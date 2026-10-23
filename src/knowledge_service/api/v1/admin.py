"""管理端配置接口：模型厂商预设、模型档案与问答记录管理；全部仅限管理员。"""

import time
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.auth import require_admin_service_auth
from knowledge_service.api.v1.schemas import (
    AdminCredentialCreate,
    AdminCredentialIssued,
    AdminCredentialPatch,
    AdminCredentialRead,
    AdminDailyTrendRead,
    AdminPromptRead,
    AdminPromptWrite,
    AdminStatisticsRead,
    AdminTokenUsageRead,
    ChunkDefaultsRead,
    ModelApiKeyWrite,
    ModelProfilePatch,
    ModelProfileRead,
    ModelProfileTestRead,
    ModelProfileWrite,
    ModelProviderPreset,
    QAEventRead,
    QARecordAdminListResponse,
    QARecordAdminRead,
    QARecordDetail,
    QARecordRead,
    QARecordReviewPatch,
    QASourceRead,
)
from knowledge_service.application import admin_credentials
from knowledge_service.application import model_profiles as service
from knowledge_service.application.documents.rendering import render_images
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.domain.errors import (
    AdminCredentialNotFound,
    CredentialEncryptionError,
    ModelProfileInUse,
    ModelProfileNotFound,
)
from knowledge_service.infrastructure.model_providers.chat import (
    FAQ_GENERATION_PROMPT_NAME,
    FAQ_REWRITE_PROMPT_NAME,
    SYSTEM_PROMPT_NAME,
    ChatProviderError,
    generate_answer,
    resolve_prompt,
)
from knowledge_service.infrastructure.model_providers.embeddings import (
    EmbeddingProviderError,
    embed_texts,
)
from knowledge_service.infrastructure.model_providers.rerank import (
    RerankProviderError,
    rerank_documents,
)
from knowledge_service.infrastructure.persistence.models import (
    AdminPromptOverride,
    Company,
    Document,
    DocumentAsset,
    KnowledgeBase,
    ModelProfile,
    QAEvent,
    QARecord,
    QASource,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_db_session

router = APIRouter(
    prefix="/admin",
    tags=["管理端配置"],
    dependencies=[Depends(require_admin_service_auth)],
)

# 管理页可编辑的提示词注册表；name 与生成链路共用，label 仅用于展示。
PROMPT_REGISTRY: dict[str, str] = {
    SYSTEM_PROMPT_NAME: "问答生成系统提示词",
    FAQ_GENERATION_PROMPT_NAME: "FAQ 自动生成提示词",
    FAQ_REWRITE_PROMPT_NAME: "FAQ AI 改写提示词",
}


def _read(profile: ModelProfile) -> ModelProfileRead:
    """拼装档案视图；has_api_key 只表示是否已配置，不携带任何密钥内容。"""
    return ModelProfileRead(
        id=profile.id,
        name=profile.name,
        kind=profile.kind,
        provider=profile.provider,
        model_name=profile.model_name,
        dimensions=profile.dimensions,
        settings=profile.settings,
        secret_ref=profile.secret_ref,
        is_enabled=profile.is_enabled,
        is_default=profile.is_default,
        has_api_key=profile.api_key_cipher is not None,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.get("/model-presets", response_model=list[ModelProviderPreset], summary="模型厂商预设")
async def list_model_presets() -> list[ModelProviderPreset]:
    """代码内置的厂商预设；界面据此生成服务商下拉选项。"""
    return [ModelProviderPreset(**preset) for preset in service.VENDOR_PRESETS]


@router.get("/chunk-defaults", response_model=ChunkDefaultsRead, summary="全局分块参数默认值")
async def get_chunk_defaults(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ChunkDefaultsRead:
    """建库向导用于预填表单；子库不传分块参数时即跟随这些默认值。"""
    return ChunkDefaultsRead(
        parent_chunk_chars=settings.parent_chunk_chars,
        child_chunk_chars=settings.child_chunk_chars,
        child_chunk_overlap_chars=settings.child_chunk_overlap_chars,
    )


@router.get("/model-profiles", response_model=list[ModelProfileRead], summary="模型档案列表")
async def list_model_profiles(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    kind: Annotated[
        Literal["embedding", "rerank", "chat"] | None,
        Query(description="按模型类型过滤；不传返回全部。"),
    ] = None,
) -> list[ModelProfileRead]:
    items = await service.list_model_profiles(session, kind=kind)
    return [_read(item) for item in items]


@router.post(
    "/model-profiles",
    response_model=ModelProfileRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建模型档案",
)
async def create_model_profile(
    payload: ModelProfileWrite,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ModelProfileRead:
    try:
        profile = await service.create_model_profile(session, payload)
    except ModelProfileInUse as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _read(profile)


@router.get(
    "/model-profiles/{model_profile_id}", response_model=ModelProfileRead, summary="模型档案详情"
)
async def get_model_profile(
    model_profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ModelProfileRead:
    try:
        profile = await service.get_model_profile(session, model_profile_id=model_profile_id)
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    return _read(profile)


@router.patch(
    "/model-profiles/{model_profile_id}", response_model=ModelProfileRead, summary="修改模型档案"
)
async def update_model_profile(
    model_profile_id: UUID,
    payload: ModelProfilePatch,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ModelProfileRead:
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="至少提供一个要修改的字段")
    try:
        current = await service.get_model_profile(session, model_profile_id=model_profile_id)
        # 用当前值与补丁合并后整体校验，避免部分修改组合出无效档案。
        base = {field: getattr(current, field) for field in service.ModelProfileSpec.model_fields}
        merged = service.ModelProfileSpec(**{**base, **changes})
        profile = await service.update_model_profile(
            session, model_profile_id=model_profile_id, spec=merged
        )
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    except ModelProfileInUse as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _read(profile)


@router.delete(
    "/model-profiles/{model_profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除模型档案",
    description="仍被大库或子库引用的档案拒绝删除，需先删除或重建相关库。",
)
async def delete_model_profile(
    model_profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    try:
        await service.delete_model_profile(session, model_profile_id=model_profile_id)
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    except ModelProfileInUse as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="模型档案仍被引用，无法删除") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/model-profiles/{model_profile_id}/default",
    response_model=ModelProfileRead,
    summary="设为同类默认模型",
)
async def set_default_model_profile(
    model_profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ModelProfileRead:
    try:
        profile = await service.set_default_model_profile(
            session, model_profile_id=model_profile_id
        )
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    return _read(profile)


@router.delete(
    "/model-profiles/{model_profile_id}/default",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="取消默认标记",
)
async def clear_default_model_profile(
    model_profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    try:
        await service.clear_default_model_profile(session, model_profile_id=model_profile_id)
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/model-profiles/{model_profile_id}/api-key",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="写入模型 API Key",
    description="密钥经 Fernet 加密后入库，只写不回读；读取接口仅返回是否已配置。",
)
async def set_model_api_key(
    model_profile_id: UUID,
    payload: ModelApiKeyWrite,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    try:
        await service.set_model_api_key(
            session,
            model_profile_id=model_profile_id,
            plaintext=payload.api_key.get_secret_value(),
        )
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    except CredentialEncryptionError as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.delete(
    "/model-profiles/{model_profile_id}/api-key",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="清除模型 API Key",
)
async def clear_model_api_key(
    model_profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    try:
        await service.clear_model_api_key(session, model_profile_id=model_profile_id)
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/model-profiles/{model_profile_id}/test",
    response_model=ModelProfileTestRead,
    summary="测试模型档案连通性",
    description=(
        "按档案类型发起一次最小真实调用：Embedding 校验返回维度，Rerank 校验排序结果，"
        "Chat 校验能否生成回复。失败仍返回 200，由 ok=false 和 error_code 说明原因。"
    ),
)
async def test_model_profile(
    model_profile_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ModelProfileTestRead:
    try:
        profile = await service.get_model_profile(session, model_profile_id=model_profile_id)
    except ModelProfileNotFound as exc:
        raise HTTPException(status_code=404, detail="模型档案不存在") from exc

    started = time.perf_counter()
    try:
        if profile.kind == "embedding":
            vectors = await embed_texts(profile, ["知识库连通性测试"])
            message = f"Embedding 调用成功，返回 {len(vectors[0])} 维向量"
        elif profile.kind == "rerank":
            ranked = await rerank_documents(
                profile,
                query="知识库连通性测试",
                documents=["测试文档一", "测试文档二"],
            )
            message = f"Rerank 调用成功，返回 {len(ranked)} 条排序结果"
        else:
            answer = await generate_answer(
                context="这是一次模型连通性测试，没有参考资料。",
                query="请只回复“正常”两个字。",
                profile=profile,
            )
            message = f"Chat 调用成功，模型回复：{answer[:60]}"
    except (EmbeddingProviderError, RerankProviderError, ChatProviderError) as exc:
        # 适配器错误统一为“错误码: 说明”，拆出错误码供界面区分配置错误与网络故障。
        code, _, detail = str(exc).partition(":")
        return ModelProfileTestRead(
            ok=False,
            kind=profile.kind,
            latency_ms=int((time.perf_counter() - started) * 1000),
            message=detail.strip() or str(exc),
            error_code=code.strip() or None,
        )
    except CredentialEncryptionError as exc:
        return ModelProfileTestRead(
            ok=False,
            kind=profile.kind,
            latency_ms=int((time.perf_counter() - started) * 1000),
            message=str(exc),
        )

    return ModelProfileTestRead(
        ok=True,
        kind=profile.kind,
        latency_ms=int((time.perf_counter() - started) * 1000),
        message=message,
    )


@router.get(
    "/credentials", response_model=list[AdminCredentialRead], summary="管理员凭据列表"
)
async def list_admin_credentials(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> list[AdminCredentialRead]:
    """列出库内管理员凭据；不返回哈希或明文，.env 引导 Token 不在此列。"""
    return [
        AdminCredentialRead.model_validate(item)
        for item in await admin_credentials.list_admin_credentials(session)
    ]


@router.post(
    "/credentials",
    response_model=AdminCredentialIssued,
    status_code=status.HTTP_201_CREATED,
    summary="创建管理员凭据",
    description="明文 Token 仅本次返回；只保存哈希。",
)
async def create_admin_credential(
    payload: AdminCredentialCreate,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> AdminCredentialIssued:
    credential, token = await admin_credentials.create_admin_credential(
        session, name=payload.name
    )
    # 明文仅返回一次，必须先提交成功，不能依赖响应发送后的会话清理提交。
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return AdminCredentialIssued(admin_credential_id=credential.id, token=token)


@router.post(
    "/credentials/{admin_credential_id}/reset",
    response_model=AdminCredentialIssued,
    summary="重置管理员凭据",
    description="替换已有凭据并启用；旧 Token 对新请求立即失效，新明文仅本次返回。",
)
async def reset_admin_credential(
    admin_credential_id: UUID,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> AdminCredentialIssued:
    try:
        token = await admin_credentials.reset_admin_credential(
            session, admin_credential_id=admin_credential_id
        )
        await session.commit()
    except AdminCredentialNotFound as exc:
        raise HTTPException(status_code=404, detail="管理员凭据不存在") from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return AdminCredentialIssued(admin_credential_id=admin_credential_id, token=token)


@router.patch(
    "/credentials/{admin_credential_id}",
    response_model=AdminCredentialRead,
    summary="启停管理员凭据",
)
async def patch_admin_credential(
    admin_credential_id: UUID,
    payload: AdminCredentialPatch,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> AdminCredentialRead:
    try:
        await admin_credentials.set_admin_credential_enabled(
            session, admin_credential_id=admin_credential_id, is_enabled=payload.is_enabled
        )
        credential = await admin_credentials.get_admin_credential(
            session, admin_credential_id=admin_credential_id
        )
    except AdminCredentialNotFound as exc:
        raise HTTPException(status_code=404, detail="管理员凭据不存在") from exc
    return AdminCredentialRead.model_validate(credential)


@router.delete(
    "/credentials/{admin_credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除管理员凭据",
)
async def delete_admin_credential(
    admin_credential_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> None:
    try:
        await admin_credentials.delete_admin_credential(
            session, admin_credential_id=admin_credential_id
        )
    except AdminCredentialNotFound as exc:
        raise HTTPException(status_code=404, detail="管理员凭据不存在") from exc


@router.get(
    "/qa-records",
    response_model=QARecordAdminListResponse,
    summary="管理员查询问答记录",
    description=(
        "跨公司与知识库分页查询在线问答记录；可按状态、问题关键词和创建时间范围过滤，"
        "供管理端排查回答质量与检索效果。"
    ),
)
async def list_admin_qa_records(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    company_id: Annotated[
        UUID | None, Query(description="按公司过滤；不传返回全部公司。")
    ] = None,
    knowledge_base_id: Annotated[
        UUID | None, Query(description="按知识库过滤；不传返回全部知识库。")
    ] = None,
    status: Annotated[
        Literal["running", "success", "no_results", "failed"] | None,
        Query(description="按问答状态过滤；不传返回全部状态。"),
    ] = None,
    review_status: Annotated[
        Literal["none", "pending", "resolved"] | None,
        Query(description="按复核状态过滤；pending 即待补资料列表。"),
    ] = None,
    faq_matched: Annotated[
        bool | None, Query(description="按是否 FAQ 直接命中过滤；不传返回全部。")
    ] = None,
    q: Annotated[
        str | None, Query(description="对问题文本做不区分大小写的模糊匹配。")
    ] = None,
    start_time: Annotated[
        datetime | None, Query(description="创建时间下限（含）。")
    ] = None,
    end_time: Annotated[
        datetime | None, Query(description="创建时间上限（含）。")
    ] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始。")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=100, description="每页数量，最多 100 条。")
    ] = 20,
) -> QARecordAdminListResponse:
    """管理员视角的问答记录列表；不校验公司与知识库的归属关系。"""
    conditions = []
    if company_id is not None:
        conditions.append(QARecord.company_id == company_id)
    if knowledge_base_id is not None:
        conditions.append(QARecord.knowledge_base_id == knowledge_base_id)
    if status is not None:
        conditions.append(QARecord.status == status)
    if review_status is not None:
        conditions.append(QARecord.review_status == review_status)
    if faq_matched is not None:
        conditions.append(QARecord.faq_matched.is_(faq_matched))
    if q and q.strip():
        keyword = f"%{q.strip().lower()}%"
        conditions.append(func.lower(QARecord.query).like(keyword))
    if start_time is not None:
        conditions.append(QARecord.created_at >= start_time)
    if end_time is not None:
        conditions.append(QARecord.created_at <= end_time)
    total = int(
        await session.scalar(
            select(func.count(QARecord.id)).where(*conditions)
        )
        or 0
    )
    rows = list(
        await session.execute(
            select(QARecord, Company.name, KnowledgeBase.name)
            .join(Company, Company.id == QARecord.company_id, isouter=True)
            .join(KnowledgeBase, KnowledgeBase.id == QARecord.knowledge_base_id)
            .where(*conditions)
            .order_by(QARecord.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return QARecordAdminListResponse(
        page=page,
        page_size=page_size,
        total=total,
        items=[
            QARecordAdminRead(
                **QARecordRead.model_validate(record).model_dump(),
                company_name=company_name,
                knowledge_base_name=knowledge_base_name,
            )
            for record, company_name, knowledge_base_name in rows
        ],
    )


@router.get(
    "/qa-records/{qa_record_id}",
    response_model=QARecordDetail,
    summary="管理员查询问答记录详情",
    description="返回单次问答的答案、来源快照和检索/生成阶段日志。",
)
async def get_admin_qa_record(
    qa_record_id: Annotated[UUID, Path(description="问答记录 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> QARecordDetail:
    """管理员视角的问答详情；不做公司范围校验。"""
    record = await session.scalar(
        select(QARecord).where(QARecord.id == qa_record_id)
    )
    if record is None:
        raise HTTPException(status_code=404, detail="问答记录不存在")
    sources = list(
        await session.scalars(
            select(QASource)
            .where(QASource.qa_record_id == qa_record_id)
            .order_by(QASource.rank)
        )
    )
    events = list(
        await session.scalars(
            select(QAEvent)
            .where(QAEvent.qa_record_id == qa_record_id)
            .order_by(QAEvent.created_at)
        )
    )
    # 快照中的 [[KB_ASSET_KEY:...]] 占位符在输出时替换成图片 URL，正文不落库部署域名。
    assets_by_id = await _load_snapshot_assets(session, sources)
    source_schemas: list[QASourceRead] = []
    for source in sources:
        read = QASourceRead.model_validate(source)
        assets = [
            assets_by_id[asset_id]
            for asset_id in _source_asset_ids(source)
            if asset_id in assets_by_id
        ]
        if assets:
            read.content_snapshot = render_images(read.content_snapshot, assets)
            if read.context_snapshot:
                read.context_snapshot = render_images(read.context_snapshot, assets)
        source_schemas.append(read)
    base = QARecordRead.model_validate(record).model_dump()
    return QARecordDetail(
        **base,
        sources=source_schemas,
        events=[QAEventRead.model_validate(event) for event in events],
    )


def _source_asset_ids(source: QASource) -> list[UUID]:
    """解析快照资产 ID 列表，忽略历史数据中的异常值。"""
    ids: list[UUID] = []
    for raw in source.asset_ids or []:
        try:
            ids.append(UUID(str(raw)))
        except (ValueError, TypeError, AttributeError):
            continue
    return ids


async def _load_snapshot_assets(
    session: AsyncSession, sources: list[QASource]
) -> dict[UUID, DocumentAsset]:
    """按快照 asset_ids 批量加载资产，用于占位符替换。"""
    ids = {asset_id for source in sources for asset_id in _source_asset_ids(source)}
    if not ids:
        return {}
    rows = await session.scalars(select(DocumentAsset).where(DocumentAsset.id.in_(ids)))
    return {asset.id: asset for asset in rows}


@router.patch(
    "/qa-records/{qa_record_id}/review",
    response_model=QARecordAdminRead,
    summary="修改问答记录复核状态",
    description="管理员标记问答已补充资料（resolved）或重新打开复核（none）。",
)
async def review_admin_qa_record(
    qa_record_id: Annotated[UUID, Path(description="问答记录 ID。")],
    payload: QARecordReviewPatch,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> QARecordAdminRead:
    """更新复核状态并返回带公司与知识库名称的最新记录。"""
    row = (
        await session.execute(
            select(QARecord, Company.name, KnowledgeBase.name)
            .join(Company, Company.id == QARecord.company_id, isouter=True)
            .join(KnowledgeBase, KnowledgeBase.id == QARecord.knowledge_base_id)
            .where(QARecord.id == qa_record_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="问答记录不存在")
    record, company_name, knowledge_base_name = row
    record.review_status = payload.review_status
    await session.commit()
    return QARecordAdminRead(
        **QARecordRead.model_validate(record).model_dump(),
        company_name=company_name,
        knowledge_base_name=knowledge_base_name,
    )


@router.get(
    "/statistics",
    response_model=AdminStatisticsRead,
    summary="统计仪表盘聚合数据",
    description="返回公司、知识库、文档与问答的汇总数字，以及近 14 天每日问答趋势。",
)
async def get_admin_statistics(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> AdminStatisticsRead:
    """聚合统计仪表盘所需数据；趋势按 UTC 日期对齐，缺失日期补零。"""
    companies_total = await session.scalar(
        select(func.count()).select_from(Company).where(Company.status == "active")
    )
    knowledge_bases_total = await session.scalar(
        select(func.count())
        .select_from(KnowledgeBase)
        .where(KnowledgeBase.status == "active")
    )
    document_rows = list(
        await session.execute(
            select(Document.status, func.count()).group_by(Document.status)
        )
    )
    documents_by_status = {status: count for status, count in document_rows}
    documents_total = sum(documents_by_status.values())

    qa_rows = list(
        await session.execute(
            select(QARecord.status, func.count()).group_by(QARecord.status)
        )
    )
    qa_by_status = {status: count for status, count in qa_rows}
    qa_total = sum(qa_by_status.values())
    review_pending = await session.scalar(
        select(func.count())
        .select_from(QARecord)
        .where(QARecord.review_status == "pending")
    )
    # token 字段为空的记录（FAQ 直出、未命中、改造前历史数据）不计入用量汇总。
    (
        prompt_tokens_total,
        completion_tokens_total,
        total_tokens_total,
        measured_qa_count,
    ) = (
        await session.execute(
            select(
                func.coalesce(func.sum(QARecord.prompt_tokens), 0),
                func.coalesce(func.sum(QARecord.completion_tokens), 0),
                func.coalesce(func.sum(QARecord.total_tokens), 0),
                func.count(QARecord.total_tokens),
            )
        )
    ).one()

    # 近 14 天按 UTC 日期聚合；MySQL/SQLite 都支持 DATE() 分组。
    first_day = utc_now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=13
    )
    trend_rows = list(
        await session.execute(
            select(
                func.date(QARecord.created_at),
                func.count(),
                func.sum(case((QARecord.status == "success", 1), else_=0)),
                func.avg(QARecord.total_duration_ms),
                func.sum(QARecord.prompt_tokens),
                func.sum(QARecord.completion_tokens),
                func.sum(QARecord.total_tokens),
            )
            .where(QARecord.created_at >= first_day)
            .group_by(func.date(QARecord.created_at))
        )
    )
    trend_map = {
        str(day): {
            "qa_total": total,
            "qa_success": success or 0,
            "avg_total_duration_ms": (
                round(avg_ms) if avg_ms is not None else None
            ),
            "prompt_tokens": prompt_tokens or 0,
            "completion_tokens": completion_tokens or 0,
            "total_tokens": tokens or 0,
        }
        for (
            day,
            total,
            success,
            avg_ms,
            prompt_tokens,
            completion_tokens,
            tokens,
        ) in trend_rows
    }
    daily_trend = []
    for offset in range(14):
        day = (first_day + timedelta(days=offset)).date().isoformat()
        item = trend_map.get(day)
        daily_trend.append(
            AdminDailyTrendRead(
                date=day,
                qa_total=item["qa_total"] if item else 0,
                qa_success=item["qa_success"] if item else 0,
                avg_total_duration_ms=(
                    item["avg_total_duration_ms"] if item else None
                ),
                total_tokens=item["total_tokens"] if item else 0,
            )
        )
    return AdminStatisticsRead(
        companies_total=companies_total or 0,
        knowledge_bases_total=knowledge_bases_total or 0,
        documents_total=documents_total,
        documents_by_status=documents_by_status,
        qa_total=qa_total,
        qa_by_status=qa_by_status,
        review_pending=review_pending or 0,
        token_usage=AdminTokenUsageRead(
            prompt_tokens=prompt_tokens_total,
            completion_tokens=completion_tokens_total,
            total_tokens=total_tokens_total,
            recent_prompt_tokens=sum(item["prompt_tokens"] for item in trend_map.values()),
            recent_completion_tokens=sum(
                item["completion_tokens"] for item in trend_map.values()
            ),
            recent_total_tokens=sum(item["total_tokens"] for item in trend_map.values()),
            measured_qa_count=measured_qa_count or 0,
        ),
        daily_trend=daily_trend,
    )


@router.get(
    "/prompts",
    response_model=list[AdminPromptRead],
    summary="提示词列表",
    description="返回管理页可编辑的系统提示词及其当前生效文本与来源。",
)
async def list_admin_prompts(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> list[AdminPromptRead]:
    """提示词文本与生成链路同源解析，管理页看到的就是线上生效内容。"""
    items = []
    for name, label in PROMPT_REGISTRY.items():
        content, source, updated_at = await resolve_prompt(session, name=name)
        items.append(
            AdminPromptRead(
                name=name, label=label, source=source, content=content, updated_at=updated_at
            )
        )
    return items


@router.put(
    "/prompts/{name}",
    response_model=AdminPromptRead,
    summary="保存提示词覆写",
    description="写入数据库覆写，即时对在线问答生成生效；重启服务后仍然保留。",
)
async def upsert_admin_prompt(
    name: Annotated[str, Path(description="提示词名称标识。")],
    payload: AdminPromptWrite,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> AdminPromptRead:
    """保存覆写内容；空正文视为无效，不允许清空提示词。"""
    if name not in PROMPT_REGISTRY:
        raise HTTPException(status_code=404, detail="提示词不存在")
    content = payload.content.strip()
    if not content:
        raise HTTPException(
            status_code=422, detail="提示词正文不能为空"
        )
    override = await session.get(AdminPromptOverride, name)
    if override is None:
        override = AdminPromptOverride(name=name, content=content)
        session.add(override)
    else:
        override.content = content
    await session.commit()
    return AdminPromptRead(
        name=name,
        label=PROMPT_REGISTRY[name],
        source="override",
        content=content,
        updated_at=override.updated_at,
    )


@router.delete(
    "/prompts/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除提示词覆写",
    description="移除数据库覆写，提示词回落到提示词文件或内置默认。",
)
async def delete_admin_prompt(
    name: Annotated[str, Path(description="提示词名称标识。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    """不存在覆写时幂等返回 204。"""
    if name not in PROMPT_REGISTRY:
        raise HTTPException(status_code=404, detail="提示词不存在")
    override = await session.get(AdminPromptOverride, name)
    if override is not None:
        await session.delete(override)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
