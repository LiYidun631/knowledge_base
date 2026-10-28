"""管理端检索评测集接口：评测集、用例与运行记录。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.auth import require_admin_service_auth
from knowledge_service.api.v1.schemas import (
    EvaluationBaselineRead,
    EvaluationCaseBulkCreate,
    EvaluationCaseCreate,
    EvaluationCaseListResponse,
    EvaluationCaseRead,
    EvaluationCaseResultRead,
    EvaluationCaseUpdate,
    EvaluationMetricsRead,
    EvaluationRunDetail,
    EvaluationRunListResponse,
    EvaluationRunRead,
    EvaluationSetCreate,
    EvaluationSetListResponse,
    EvaluationSetRead,
    EvaluationSetUpdate,
)
from knowledge_service.application.evaluation import (
    STALE_RUN_AFTER,
    EvaluationKnowledgeBaseUnavailable,
    EvaluationNoCases,
    EvaluationRunConflict,
    EvaluationSetNotFound,
    recover_stale_runs,
    schedule_run,
    start_run,
)
from knowledge_service.infrastructure.persistence.models import (
    Company,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationRun,
    EvaluationSet,
    KnowledgeBase,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_db_session

router = APIRouter(
    prefix="/admin",
    tags=["管理端评测"],
    dependencies=[Depends(require_admin_service_auth)],
)


def _run_schema(
    run: EvaluationRun,
    *,
    knowledge_base_name: str | None = None,
    set_name: str | None = None,
) -> EvaluationRunRead:
    """把运行记录转换为响应；名称由调用方按关联表填入。"""
    read = EvaluationRunRead.model_validate(run)
    return read.model_copy(
        update={
            "knowledge_base_name": knowledge_base_name,
            "set_name": set_name,
        }
    )


def _set_schema(
    evaluation_set: EvaluationSet,
    knowledge_base: KnowledgeBase,
    company_name: str | None,
    *,
    case_count: int,
    enabled_case_count: int,
    last_run: EvaluationRun | None,
) -> EvaluationSetRead:
    return EvaluationSetRead(
        id=evaluation_set.id,
        knowledge_base_id=evaluation_set.knowledge_base_id,
        company_id=knowledge_base.company_id,
        company_name=company_name,
        knowledge_base_name=knowledge_base.name,
        name=evaluation_set.name,
        description=evaluation_set.description,
        case_count=case_count,
        enabled_case_count=enabled_case_count,
        last_run=(
            _run_schema(
                last_run,
                knowledge_base_name=knowledge_base.name,
                set_name=evaluation_set.name,
            )
            if last_run is not None
            else None
        ),
        created_at=evaluation_set.created_at,
        updated_at=evaluation_set.updated_at,
    )


async def _require_set(
    session: AsyncSession, set_id: UUID
) -> tuple[EvaluationSet, KnowledgeBase, str | None]:
    row = (
        await session.execute(
            select(EvaluationSet, KnowledgeBase, Company.name)
            .join(KnowledgeBase, KnowledgeBase.id == EvaluationSet.knowledge_base_id)
            .join(Company, Company.id == KnowledgeBase.company_id, isouter=True)
            .where(EvaluationSet.id == set_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="评测集不存在")
    return row


async def _case_counts(session: AsyncSession, set_ids: list[UUID]) -> dict[UUID, tuple[int, int]]:
    """批量统计用例总数与启用数，避免评测集列表逐条查询。"""
    if not set_ids:
        return {}
    rows = await session.execute(
        select(
            EvaluationCase.set_id,
            func.count(EvaluationCase.id),
            func.sum(case((EvaluationCase.is_enabled.is_(True), 1), else_=0)),
        )
        .where(EvaluationCase.set_id.in_(set_ids))
        .group_by(EvaluationCase.set_id)
    )
    return {set_id: (int(total or 0), int(enabled or 0)) for set_id, total, enabled in rows}


async def _last_runs(session: AsyncSession, set_ids: list[UUID]) -> dict[UUID, EvaluationRun]:
    """逐库取最近一次运行；评测集列表规模有限，避免引入窗口函数。"""
    latest: dict[UUID, EvaluationRun] = {}
    for set_id in set_ids:
        run = await session.scalar(
            select(EvaluationRun)
            .where(EvaluationRun.set_id == set_id)
            .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
            .limit(1)
        )
        if run is not None:
            latest[set_id] = run
    return latest


@router.get(
    "/evaluation-sets",
    response_model=EvaluationSetListResponse,
    summary="列出检索评测集",
)
async def list_evaluation_sets(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    company_id: Annotated[UUID | None, Query(description="按公司过滤。")] = None,
    knowledge_base_id: Annotated[UUID | None, Query(description="按知识库过滤。")] = None,
    q: Annotated[str | None, Query(description="按评测集名称模糊匹配。")] = None,
) -> EvaluationSetListResponse:
    """返回评测集摘要、用例数量和最近一次运行。"""
    conditions = []
    if company_id is not None:
        conditions.append(KnowledgeBase.company_id == company_id)
    if knowledge_base_id is not None:
        conditions.append(EvaluationSet.knowledge_base_id == knowledge_base_id)
    if q and q.strip():
        conditions.append(func.lower(EvaluationSet.name).like(f"%{q.strip().lower()}%"))
    rows = list(
        await session.execute(
            select(EvaluationSet, KnowledgeBase, Company.name)
            .join(KnowledgeBase, KnowledgeBase.id == EvaluationSet.knowledge_base_id)
            .join(Company, Company.id == KnowledgeBase.company_id, isouter=True)
            .where(*conditions)
            .order_by(EvaluationSet.updated_at.desc(), EvaluationSet.created_at.desc())
            .limit(500)
        )
    )
    set_ids = [evaluation_set.id for evaluation_set, _, _ in rows]
    counts = await _case_counts(session, set_ids)
    last_runs = await _last_runs(session, set_ids)
    return EvaluationSetListResponse(
        items=[
            _set_schema(
                evaluation_set,
                knowledge_base,
                company_name,
                case_count=counts.get(evaluation_set.id, (0, 0))[0],
                enabled_case_count=counts.get(evaluation_set.id, (0, 0))[1],
                last_run=last_runs.get(evaluation_set.id),
            )
            for evaluation_set, knowledge_base, company_name in rows
        ]
    )


@router.post(
    "/evaluation-sets",
    response_model=EvaluationSetRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建检索评测集",
)
async def create_evaluation_set(
    payload: EvaluationSetCreate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> EvaluationSetRead:
    """同一知识库内评测集名称唯一。"""
    row = (
        await session.execute(
            select(KnowledgeBase, Company.name)
            .join(Company, Company.id == KnowledgeBase.company_id, isouter=True)
            .where(KnowledgeBase.id == payload.knowledge_base_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    knowledge_base, company_name = row
    evaluation_set = EvaluationSet(
        knowledge_base_id=payload.knowledge_base_id,
        name=payload.name,
        description=payload.description,
    )
    session.add(evaluation_set)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="该知识库已有同名评测集") from exc
    await session.commit()
    return _set_schema(
        evaluation_set,
        knowledge_base,
        company_name,
        case_count=0,
        enabled_case_count=0,
        last_run=None,
    )


@router.get(
    "/evaluation-sets/{set_id}",
    response_model=EvaluationCaseListResponse,
    summary="查看评测集与用例",
)
async def get_evaluation_set(
    set_id: Annotated[UUID, Path(description="评测集 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> EvaluationCaseListResponse:
    """返回评测集摘要和全部用例，便于直接编辑。"""
    evaluation_set, knowledge_base, company_name = await _require_set(session, set_id)
    cases = list(
        await session.scalars(
            select(EvaluationCase)
            .where(EvaluationCase.set_id == set_id)
            .order_by(EvaluationCase.created_at, EvaluationCase.id)
        )
    )
    last_run = await session.scalar(
        select(EvaluationRun)
        .where(EvaluationRun.set_id == set_id)
        .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
        .limit(1)
    )
    return EvaluationCaseListResponse(
        set=_set_schema(
            evaluation_set,
            knowledge_base,
            company_name,
            case_count=len(cases),
            enabled_case_count=sum(1 for case in cases if case.is_enabled),
            last_run=last_run,
        ),
        items=[EvaluationCaseRead.model_validate(case) for case in cases],
    )


@router.patch(
    "/evaluation-sets/{set_id}",
    response_model=EvaluationSetRead,
    summary="修改评测集",
)
async def update_evaluation_set(
    set_id: Annotated[UUID, Path(description="评测集 ID。")],
    payload: EvaluationSetUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> EvaluationSetRead:
    evaluation_set, knowledge_base, company_name = await _require_set(session, set_id)
    if payload.name is not None:
        evaluation_set.name = payload.name.strip()
    if payload.description is not None:
        evaluation_set.description = payload.description.strip() or None
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="该知识库已有同名评测集") from exc
    await session.commit()
    counts = await _case_counts(session, [set_id])
    last_run = await session.scalar(
        select(EvaluationRun)
        .where(EvaluationRun.set_id == set_id)
        .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
        .limit(1)
    )
    return _set_schema(
        evaluation_set,
        knowledge_base,
        company_name,
        case_count=counts.get(set_id, (0, 0))[0],
        enabled_case_count=counts.get(set_id, (0, 0))[1],
        last_run=last_run,
    )


@router.delete(
    "/evaluation-sets/{set_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除评测集",
)
async def delete_evaluation_set(
    set_id: Annotated[UUID, Path(description="评测集 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    """删除评测集，同时级联删除用例和运行记录。"""
    evaluation_set = await session.get(EvaluationSet, set_id)
    if evaluation_set is None:
        raise HTTPException(status_code=404, detail="评测集不存在")
    await session.delete(evaluation_set)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/evaluation-sets/{set_id}/cases",
    response_model=EvaluationCaseRead,
    status_code=status.HTTP_201_CREATED,
    summary="新增评测用例",
)
async def create_evaluation_case(
    set_id: Annotated[UUID, Path(description="评测集 ID。")],
    payload: EvaluationCaseCreate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> EvaluationCaseRead:
    await _require_set(session, set_id)
    evaluation_case = EvaluationCase(
        set_id=set_id,
        query=payload.query,
        expected_document_ids=[str(value) for value in payload.expected_document_ids],
        expected_keywords=payload.expected_keywords,
        retrieval_mode=payload.retrieval_mode,
        top_k=payload.top_k,
        is_enabled=payload.is_enabled,
        notes=payload.notes,
    )
    session.add(evaluation_case)
    await session.flush()
    await session.commit()
    return EvaluationCaseRead.model_validate(evaluation_case)


@router.post(
    "/evaluation-sets/{set_id}/cases/bulk",
    response_model=list[EvaluationCaseRead],
    status_code=status.HTTP_201_CREATED,
    summary="批量导入评测用例",
)
async def bulk_create_evaluation_cases(
    set_id: Annotated[UUID, Path(description="评测集 ID。")],
    payload: EvaluationCaseBulkCreate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> list[EvaluationCaseRead]:
    """一次性写入最多 500 条用例，适合脚本或其他系统导入。"""
    await _require_set(session, set_id)
    cases = [
        EvaluationCase(
            set_id=set_id,
            query=item.query,
            expected_document_ids=[str(value) for value in item.expected_document_ids],
            expected_keywords=item.expected_keywords,
            retrieval_mode=item.retrieval_mode,
            top_k=item.top_k,
            is_enabled=item.is_enabled,
            notes=item.notes,
        )
        for item in payload.items
    ]
    session.add_all(cases)
    await session.flush()
    await session.commit()
    return [EvaluationCaseRead.model_validate(case) for case in cases]


@router.patch(
    "/evaluation-cases/{case_id}",
    response_model=EvaluationCaseRead,
    summary="修改评测用例",
)
async def update_evaluation_case(
    case_id: Annotated[UUID, Path(description="评测用例 ID。")],
    payload: EvaluationCaseUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> EvaluationCaseRead:
    evaluation_case = await session.get(EvaluationCase, case_id)
    if evaluation_case is None:
        raise HTTPException(status_code=404, detail="评测用例不存在")
    if payload.query is not None:
        evaluation_case.query = payload.query
    if payload.expected_document_ids is not None:
        evaluation_case.expected_document_ids = [
            str(value) for value in payload.expected_document_ids
        ]
    if payload.expected_keywords is not None:
        evaluation_case.expected_keywords = payload.expected_keywords
    if payload.retrieval_mode is not None:
        evaluation_case.retrieval_mode = payload.retrieval_mode
    if payload.top_k is not None:
        evaluation_case.top_k = payload.top_k
    if payload.is_enabled is not None:
        evaluation_case.is_enabled = payload.is_enabled
    if payload.notes is not None:
        evaluation_case.notes = payload.notes or None
    if not evaluation_case.expected_document_ids and not evaluation_case.expected_keywords:
        raise HTTPException(
            status_code=422,
            detail="expected_document_ids 与 expected_keywords 至少保留一项",
        )
    await session.commit()
    return EvaluationCaseRead.model_validate(evaluation_case)


@router.delete(
    "/evaluation-cases/{case_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除评测用例",
)
async def delete_evaluation_case(
    case_id: Annotated[UUID, Path(description="评测用例 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    evaluation_case = await session.get(EvaluationCase, case_id)
    if evaluation_case is None:
        raise HTTPException(status_code=404, detail="评测用例不存在")
    await session.delete(evaluation_case)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/evaluation-sets/{set_id}/run",
    response_model=EvaluationRunRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="启动检索评测",
)
async def start_evaluation_run(
    set_id: Annotated[UUID, Path(description="评测集 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> EvaluationRunRead:
    """在服务进程内异步执行检索；同一知识库同时只允许一个评测。"""
    try:
        run = await start_run(session, set_id)
    except EvaluationSetNotFound as exc:
        raise HTTPException(status_code=404, detail="评测集不存在") from exc
    except EvaluationKnowledgeBaseUnavailable as exc:
        raise HTTPException(status_code=409, detail="知识库不存在或已归档，无法评测") from exc
    except EvaluationRunConflict as exc:
        raise HTTPException(status_code=409, detail="该知识库已有评测在运行，请等待完成") from exc
    except EvaluationNoCases as exc:
        raise HTTPException(status_code=422, detail="评测集没有启用中的用例") from exc
    # 先提交运行记录，后台任务才能读到它；再启动进程内任务。
    await session.commit()
    evaluation_set, knowledge_base, _ = await _require_set(session, set_id)
    schedule_run(run.id)
    return _run_schema(
        run,
        knowledge_base_name=knowledge_base.name,
        set_name=evaluation_set.name,
    )


@router.get(
    "/evaluation-runs",
    response_model=EvaluationRunListResponse,
    summary="查询评测运行记录",
)
async def list_evaluation_runs(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    set_id: Annotated[UUID | None, Query(description="按评测集过滤。")] = None,
    knowledge_base_id: Annotated[UUID | None, Query(description="按知识库过滤。")] = None,
    status_filter: Annotated[
        Literal["running", "success", "failed"] | None,
        Query(alias="status", description="按运行状态过滤。"),
    ] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始。")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页数量，最多 100 条。")] = 20,
) -> EvaluationRunListResponse:
    conditions = []
    if set_id is not None:
        conditions.append(EvaluationRun.set_id == set_id)
    if knowledge_base_id is not None:
        conditions.append(EvaluationRun.knowledge_base_id == knowledge_base_id)
    if status_filter is not None:
        conditions.append(EvaluationRun.status == status_filter)
    total = int(await session.scalar(select(func.count(EvaluationRun.id)).where(*conditions)) or 0)
    rows = list(
        await session.execute(
            select(EvaluationRun, KnowledgeBase.name, EvaluationSet.name)
            .join(KnowledgeBase, KnowledgeBase.id == EvaluationRun.knowledge_base_id)
            .join(EvaluationSet, EvaluationSet.id == EvaluationRun.set_id)
            .where(*conditions)
            .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return EvaluationRunListResponse(
        page=page,
        page_size=page_size,
        total=total,
        items=[
            _run_schema(run, knowledge_base_name=knowledge_base_name, set_name=set_name)
            for run, knowledge_base_name, set_name in rows
        ],
    )


@router.get(
    "/evaluation-runs/{run_id}",
    response_model=EvaluationRunDetail,
    summary="查看评测运行详情",
)
async def get_evaluation_run(
    run_id: Annotated[UUID, Path(description="运行 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> EvaluationRunDetail:
    """返回逐条用例结果，并与同评测集上一次成功运行对比。"""
    run = await session.get(EvaluationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="评测运行不存在")
    if run.status == "running" and run.started_at < utc_now() - STALE_RUN_AFTER:
        await recover_stale_runs(session)
        await session.commit()
        await session.refresh(run)
    row = (
        await session.execute(
            select(EvaluationRun, KnowledgeBase.name, EvaluationSet.name)
            .join(KnowledgeBase, KnowledgeBase.id == EvaluationRun.knowledge_base_id)
            .join(EvaluationSet, EvaluationSet.id == EvaluationRun.set_id)
            .where(EvaluationRun.id == run_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="评测运行不存在")
    run, knowledge_base_name, set_name = row
    results = list(
        await session.scalars(
            select(EvaluationCaseResult)
            .where(EvaluationCaseResult.run_id == run_id)
            .order_by(EvaluationCaseResult.created_at, EvaluationCaseResult.id)
        )
    )
    baseline_run = await session.scalar(
        select(EvaluationRun)
        .where(
            EvaluationRun.set_id == run.set_id,
            EvaluationRun.status == "success",
            EvaluationRun.id != run.id,
            EvaluationRun.created_at < run.created_at,
        )
        .order_by(EvaluationRun.created_at.desc())
        .limit(1)
    )
    read = _run_schema(run, knowledge_base_name=knowledge_base_name, set_name=set_name)
    return EvaluationRunDetail(
        **read.model_dump(),
        results=[EvaluationCaseResultRead.model_validate(item) for item in results],
        baseline=(
            EvaluationBaselineRead(
                id=baseline_run.id,
                finished_at=baseline_run.finished_at,
                metrics=EvaluationMetricsRead.model_validate(baseline_run.metrics or {}),
            )
            if baseline_run is not None
            else None
        ),
    )


@router.delete(
    "/evaluation-runs/{run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除评测运行记录",
)
async def delete_evaluation_run(
    run_id: Annotated[UUID, Path(description="运行 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    run = await session.get(EvaluationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="评测运行不存在")
    if run.status == "running":
        raise HTTPException(status_code=409, detail="运行中的评测不能删除")
    await session.delete(run)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
