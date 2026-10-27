"""FAQ 管理端接口：库级与文档级的增删改查、AI 改写与文档 FAQ 生成。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.auth import require_admin_service_auth
from knowledge_service.api.v1.schemas import (
    FaqBatchGenerateResponse,
    FaqCreate,
    FaqGenerateResponse,
    FaqListResponse,
    FaqRead,
    FaqRewriteRequest,
    FaqUpdate,
)
from knowledge_service.application.faq.service import (
    FaqEntryNotFound,
    FaqModelUnavailable,
    FaqNotFound,
    create_faq,
    enqueue_missing_faq_generation,
    generate_for_document_version,
    random_faqs,
    rewrite_faq,
    update_faq,
)
from knowledge_service.infrastructure.model_providers.chat import ChatProviderError
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentVersion,
    FaqEntry,
    KnowledgeBase,
)
from knowledge_service.infrastructure.persistence.session import get_db_session

router = APIRouter(
    prefix="/admin",
    tags=["FAQ 管理"],
    dependencies=[Depends(require_admin_service_auth)],
)


async def _require_kb(session: AsyncSession, kb_id: UUID) -> KnowledgeBase:
    kb = await session.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")
    return kb


async def _require_faq(session: AsyncSession, faq_id: UUID) -> FaqEntry:
    entry = await session.get(FaqEntry, faq_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="FAQ 不存在")
    return entry


def _faq_http_error(error: Exception) -> HTTPException:
    if isinstance(error, FaqEntryNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="FAQ 不存在")
    if isinstance(error, FaqModelUnavailable):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error))
    if isinstance(error, ChatProviderError):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="FAQ 处理失败，请稍后重试",
    )


@router.get("/knowledge-bases/{knowledge_base_id}/faqs", response_model=FaqListResponse)
async def list_faqs(
    knowledge_base_id: Annotated[UUID, Path(description="知识库 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    scope: Annotated[
        Literal["all", "kb", "document"],
        Query(description="范围：all 全部、kb 仅库级、document 指定文档级。"),
    ] = "all",
    document_id: Annotated[
        UUID | None,
        Query(description="按文档过滤；scope=document 时必填。"),
    ] = None,
    source: Annotated[
        Literal["auto", "manual"] | None,
        Query(description="按来源过滤：auto 自动生成、manual 手动添加。"),
    ] = None,
    q: Annotated[
        str | None,
        Query(max_length=200, description="对问题与答案做不区分大小写的模糊匹配。"),
    ] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始。")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=100, description="每页条数，最多 100 条。")
    ] = 20,
) -> FaqListResponse:
    await _require_kb(session, knowledge_base_id)
    if scope == "document" and document_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="scope=document 时必须提供 document_id",
        )
    conditions = [FaqEntry.knowledge_base_id == knowledge_base_id]
    if scope == "kb":
        conditions.append(FaqEntry.document_id.is_(None))
    elif document_id is not None:
        conditions.append(FaqEntry.document_id == document_id)
    if source is not None:
        conditions.append(FaqEntry.source == source)
    if q and q.strip():
        keyword = f"%{q.strip().lower()}%"
        conditions.append(
            or_(
                func.lower(FaqEntry.question).like(keyword),
                func.lower(FaqEntry.answer).like(keyword),
            )
        )
    total = int(
        await session.scalar(select(func.count(FaqEntry.id)).where(*conditions)) or 0
    )
    rows = list(
        await session.scalars(
            select(FaqEntry)
            .where(*conditions)
            .order_by(FaqEntry.created_at.desc(), FaqEntry.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return FaqListResponse(
        page=page,
        page_size=page_size,
        total=total,
        items=[FaqRead.model_validate(entry) for entry in rows],
    )


@router.get(
    "/knowledge-bases/{knowledge_base_id}/faqs/random",
    response_model=list[FaqRead],
    summary="随机抽取 FAQ 问题",
    description=(
        "问答测试页的推荐问题卡片：库级 FAQ 始终参与，文档级 FAQ 按产品/型号过滤，"
        "与在线问答的 FAQ 匹配池语义一致。"
    ),
)
async def list_random_faqs(
    knowledge_base_id: Annotated[UUID, Path(description="知识库 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    limit: Annotated[int, Query(ge=1, le=20, description="返回条数，默认 6。")] = 6,
    product_name: Annotated[
        str | None, Query(max_length=200, description="产品名称过滤；不传只返回库级 FAQ。")
    ] = None,
    model_name: Annotated[
        str | None, Query(max_length=200, description="产品型号过滤；不传只返回库级 FAQ。")
    ] = None,
) -> list[FaqEntry]:
    kb = await _require_kb(session, knowledge_base_id)
    return await random_faqs(
        session,
        kb=kb,
        product_name=product_name,
        model_name=model_name,
        limit=limit,
    )


@router.post("/knowledge-bases/{knowledge_base_id}/faqs", response_model=FaqRead)
async def add_faq(
    knowledge_base_id: Annotated[UUID, Path(description="知识库 ID。")],
    payload: FaqCreate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> FaqEntry:
    kb = await _require_kb(session, knowledge_base_id)
    document_id = payload.document_id
    document_version_id = None
    if document_id is not None:
        document = await session.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.knowledge_base_id == knowledge_base_id,
            )
        )
        if document is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="文档不属于该知识库",
            )
        version = await session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.is_current.is_(True),
            )
        )
        if version is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="文档还没有已完成的当前版本，无法绑定文档级 FAQ",
            )
        document_version_id = version.id
    try:
        return await create_faq(
            session,
            kb=kb,
            question=payload.question,
            answer=payload.answer,
            document_id=document_id,
            document_version_id=document_version_id,
            source="manual",
        )
    except (FaqModelUnavailable, ChatProviderError) as error:
        raise _faq_http_error(error) from error


@router.patch("/faqs/{faq_id}", response_model=FaqRead)
async def patch_faq(
    faq_id: Annotated[UUID, Path(description="FAQ ID。")],
    payload: FaqUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> FaqEntry:
    entry = await _require_faq(session, faq_id)
    if payload.question is None and payload.answer is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="question 与 answer 至少提供一个",
        )
    kb = await _require_kb(session, entry.knowledge_base_id)
    try:
        return await update_faq(
            session, kb=kb, entry=entry, question=payload.question, answer=payload.answer
        )
    except (FaqModelUnavailable, ChatProviderError) as error:
        raise _faq_http_error(error) from error


@router.delete("/faqs/{faq_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_faq(
    faq_id: Annotated[UUID, Path(description="FAQ ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> None:
    entry = await _require_faq(session, faq_id)
    await session.delete(entry)


@router.post("/faqs/{faq_id}/rewrite", response_model=FaqRead)
async def rewrite_faq_endpoint(
    faq_id: Annotated[UUID, Path(description="FAQ ID。")],
    payload: Annotated[FaqRewriteRequest | None, Body()] = None,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")] = None,
) -> FaqEntry:
    entry = await _require_faq(session, faq_id)
    try:
        return await rewrite_faq(
            session, entry=entry, instruction=payload.instruction if payload else None
        )
    except (FaqModelUnavailable, ChatProviderError) as error:
        raise _faq_http_error(error) from error


@router.post(
    "/knowledge-bases/{knowledge_base_id}/faqs/generate",
    response_model=FaqBatchGenerateResponse,
    summary="批量补齐文档 FAQ",
    description=(
        "为当前版本还没有自动 FAQ 的文档排队生成；已有自动条目或已在排队的文档跳过。"
        "归档知识库不允许生成。"
    ),
)
async def generate_knowledge_base_faqs(
    knowledge_base_id: Annotated[UUID, Path(description="知识库 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> FaqBatchGenerateResponse:
    kb = await _require_kb(session, knowledge_base_id)
    if kb.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="知识库已归档，不能生成 FAQ"
        )
    queued, skipped = await enqueue_missing_faq_generation(session, kb=kb)
    return FaqBatchGenerateResponse(queued=queued, skipped=skipped)


@router.post(
    "/documents/{document_id}/faqs/generate",
    response_model=FaqGenerateResponse,
    summary="为文档生成 FAQ",
    description=(
        "从文档当前版本的父块内容提炼 FAQ 问答对（自动生成条目会替换同版本旧条目）；"
        "入库完成后的自动生成失败时，可在此手动重试。"
    ),
)
async def generate_document_faqs(
    document_id: Annotated[UUID, Path(description="文档 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> FaqGenerateResponse:
    document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
    version = await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.is_current.is_(True),
        )
    )
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="文档还没有已完成的当前版本，无法生成 FAQ",
        )
    try:
        entries = await generate_for_document_version(
            session, document_id=document_id, document_version_id=version.id
        )
    except FaqNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except (FaqModelUnavailable, ChatProviderError) as error:
        raise _faq_http_error(error) from error
    return FaqGenerateResponse(entries=entries)
