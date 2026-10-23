"""知识库在线问答 HTTP 接口。"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from time import perf_counter
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.v1.schemas import (
    KnowledgeAnswerRequest,
    KnowledgeAnswerResponse,
    QAEventRead,
    QARecordDetail,
    QARecordListResponse,
    QARecordRead,
    QASourceRead,
)
from knowledge_service.api.v1.search import _result_to_schema
from knowledge_service.application.documents.rendering import has_images, render_images
from knowledge_service.application.faq.service import FaqModelUnavailable, match_faq
from knowledge_service.application.qa.service import (
    QASessionKnowledgeBaseMismatch,
    add_qa_event,
    finish_qa_failure,
    finish_qa_no_results,
    finish_qa_success,
    start_qa_record,
)
from knowledge_service.application.search.service import (
    KnowledgeSearchDocumentsInvalid,
    KnowledgeSearchModelUnavailable,
    KnowledgeSearchNotFound,
    search_knowledge_base,
)
from knowledge_service.core.config import get_settings
from knowledge_service.infrastructure.chroma.vector_store import ChromaSearchError
from knowledge_service.infrastructure.model_providers.chat import (
    ChatProviderError,
    generate_answer,
    generate_answer_stream,
    normalize_answer,
)
from knowledge_service.infrastructure.model_providers.embeddings import EmbeddingProviderError
from knowledge_service.infrastructure.model_providers.rerank import RerankProviderError
from knowledge_service.infrastructure.persistence.models import (
    KnowledgeBase,
    ModelProfile,
    QAEvent,
    QARecord,
    QASource,
)
from knowledge_service.infrastructure.persistence.session import get_db_session, get_session_factory

router = APIRouter(tags=["知识库在线问答"])
logger = logging.getLogger(__name__)


def _candidate_models(results: list) -> list[str]:
    """提取本次命中资料中的去重型号，并保持稳定排序。"""
    models: set[str] = set()
    for result in results:
        if result.model_name:
            models.add(str(result.model_name).strip())
        models.update(
            str(name).strip() for name in (result.model_names or []) if str(name).strip()
        )
    return sorted(models)


def _mentioned_models(query: str, models: list[str]) -> list[str]:
    """返回用户问题中明确出现的已知型号。"""
    normalized = query.casefold()
    return [model for model in models if model.casefold() in normalized]


def _needs_full_section(query: str) -> bool:
    """判断是否在询问一整段操作说明，而不是单个细节。"""
    phrases = (
        "正确使用方法",
        "使用方法",
        "操作步骤",
        "测量步骤",
        "怎么使用",
        "如何使用",
        "使用说明",
        "操作说明",
    )
    return any(phrase in query for phrase in phrases)


def _duration_ms(started_at: float) -> int:
    """将高精度计时转换为非负毫秒数。"""
    return max(0, int((perf_counter() - started_at) * 1000))


def _error_code(error: Exception) -> str:
    """从内部异常中提取不超过 100 字符的稳定错误代码。"""
    return str(error).split(":", 1)[0][:100] or error.__class__.__name__


def _qa_http_error(error: Exception) -> tuple[int, str]:
    """从内部异常中提取对外稳定的 HTTP 状态码与提示文案。"""
    if isinstance(error, KnowledgeSearchNotFound):
        return 404, "知识库不存在或已归档"
    if isinstance(error, QASessionKnowledgeBaseMismatch):
        return 422, "session_id 不属于当前知识库"
    if isinstance(error, KnowledgeSearchDocumentsInvalid):
        return 422, "document_ids 中存在不属于该知识库的文档"
    if isinstance(error, KnowledgeSearchModelUnavailable):
        return 503, f"知识库绑定的{error}模型不可用"
    if isinstance(error, FaqModelUnavailable):
        return 503, str(error)
    if isinstance(error, EmbeddingProviderError):
        return 503, str(error)
    if isinstance(error, ChromaSearchError):
        return 503, "Chroma 向量检索暂不可用"
    if isinstance(error, RerankProviderError):
        return 502, str(error)
    if isinstance(error, ChatProviderError):
        return 502, str(error)
    return 500, "在线问答处理失败，请稍后重试"


def _raise_qa_http_error(error: Exception) -> None:
    """将检索和模型异常转换为对外稳定的 HTTP 错误。"""
    error_status, detail = _qa_http_error(error)
    raise HTTPException(status_code=error_status, detail=detail) from error


@router.post(
    "/knowledge-bases/{knowledge_base_id}/ask",
    response_model=KnowledgeAnswerResponse,
    summary="检索并生成知识库答案",
    description="先执行知识库检索，再把检索片段交给聊天模型整理为中文答案，同时返回原始来源。",
)
async def ask_knowledge_base(
    knowledge_base_id: Annotated[UUID, Path(description="待问答的知识库 ID。")],
    company_id: Annotated[UUID, Query(description="公司租户 ID；必须与子库归属一致。")],
    payload: KnowledgeAnswerRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> KnowledgeAnswerResponse:
    """执行检索、组织上下文并调用聊天模型生成答案。"""
    settings = get_settings()
    if payload.top_k > settings.search_max_top_k:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"top_k 不能大于服务配置上限 {settings.search_max_top_k}",
        )
    knowledge_base = await session.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.status == "active",
            KnowledgeBase.company_id == company_id,
        )
    )
    if knowledge_base is None:
        raise HTTPException(status_code=404, detail="知识库不存在或已归档")
    try:
        record = await start_qa_record(
            session,
            knowledge_base_id=knowledge_base_id,
            company_id=company_id,
            session_id=payload.session_id,
            query=payload.query,
            retrieval_mode=payload.retrieval_mode,
            top_k=payload.top_k,
            # 整体方法类问题必须展开正文上下文，即使调用方关闭了普通邻块扩展。
            include_context=payload.include_context or _needs_full_section(payload.query),
            chat_provider=settings.chat_provider,
            chat_model_name=settings.chat_model_name,
        )
    except QASessionKnowledgeBaseMismatch as exc:
        raise HTTPException(status_code=422, detail="session_id 不属于当前知识库") from exc

    request_started_at = perf_counter()
    retrieval_started_at = perf_counter()
    retrieval_duration_ms: int | None = None
    generation_duration_ms: int | None = None
    generation_started_at: float | None = None
    results = []
    try:
        # FAQ 前置匹配：命中库级或当前版本文档级问答对时原样直出，跳过检索与生成。
        # 文档级 FAQ 按产品/型号权限过滤，见 match_faq。
        faq_entry = await match_faq(
            session,
            kb=knowledge_base,
            query=payload.query,
            product_name=payload.product_name,
            model_name=payload.model_name,
        )
        if faq_entry is not None:
            answer = faq_entry.answer
            await finish_qa_success(
                session,
                record,
                answer=answer,
                results=[],
                retrieval_duration_ms=0,
                generation_duration_ms=0,
                total_duration_ms=_duration_ms(request_started_at),
                badcase_rerank_threshold=settings.qa_badcase_rerank_threshold,
                faq_matched=True,
            )
            logger.info(
                "Knowledge QA answered by FAQ: request_id=%s faq_id=%s",
                record.request_id,
                faq_entry.id,
            )
            return KnowledgeAnswerResponse(
                company_id=company_id,
                knowledge_base_id=knowledge_base_id,
                qa_record_id=record.id,
                request_id=record.request_id,
                session_id=payload.session_id,
                query=payload.query,
                answer=answer,
                faq_matched=True,
                retrieval_mode=payload.retrieval_mode,
                sources=[],
            )
        results = await search_knowledge_base(
            session,
            knowledge_base_id=knowledge_base_id,
            company_id=company_id,
            query=payload.query,
            top_k=payload.top_k,
            document_ids=payload.document_ids,
            retrieval_mode=payload.retrieval_mode,
            include_context=payload.include_context,
            expand_section=_needs_full_section(payload.query),
            section_query=payload.query,
            product_name=payload.product_name,
            model_name=payload.model_name,
        )
        retrieval_duration_ms = _duration_ms(retrieval_started_at)
        if not results:
            await finish_qa_no_results(
                session,
                record,
                retrieval_duration_ms=retrieval_duration_ms,
                total_duration_ms=_duration_ms(request_started_at),
            )
            logger.info(
                "Knowledge QA completed without results: request_id=%s knowledge_base_id=%s",
                record.request_id,
                knowledge_base_id,
            )
            product_label = (payload.model_name or payload.product_name or "").strip()
            answer = (
                f"知识库中没有检索到与「{product_label}」相关的内容。"
                if product_label
                else "知识库中没有检索到与问题相关的内容。"
            )
            return KnowledgeAnswerResponse(
                company_id=company_id,
                knowledge_base_id=knowledge_base_id,
                qa_record_id=record.id,
                request_id=record.request_id,
                session_id=payload.session_id,
                query=payload.query,
                answer=answer,
                retrieval_mode=payload.retrieval_mode,
                sources=[],
            )
        model_options = _candidate_models(results)
        if len(model_options) > 1:
            mentioned_models = _mentioned_models(payload.query, model_options)
            if mentioned_models:
                results = [result for result in results if result.model_name in mentioned_models]
            else:
                question = "请问您使用的是以下哪个型号？"
                answer = question + "\n\n" + "\n".join(
                    f"- {model}" for model in model_options
                )
                generation_duration_ms = 0
                await finish_qa_success(
                    session,
                    record,
                    answer=answer,
                    results=results,
                    retrieval_duration_ms=retrieval_duration_ms,
                    generation_duration_ms=generation_duration_ms,
                    total_duration_ms=_duration_ms(request_started_at),
                    badcase_rerank_threshold=settings.qa_badcase_rerank_threshold,
                )
                return KnowledgeAnswerResponse(
                    company_id=company_id,
                    knowledge_base_id=knowledge_base_id,
                    qa_record_id=record.id,
                    request_id=record.request_id,
                    session_id=payload.session_id,
                    query=payload.query,
                    answer=answer,
                    clarification_required=True,
                    clarification_question=question,
                    clarification_options=model_options,
                    retrieval_mode=payload.retrieval_mode,
                    sources=[
                        _result_to_schema(index, result)
                        for index, result in enumerate(results, start=1)
                    ],
                )
        add_qa_event(
            session,
            record,
            stage="retrieval",
            status="success",
            duration_ms=retrieval_duration_ms,
            details={"result_count": len(results)},
        )
        # 只有整段操作说明类问题才需要图片原位保留；事实型问题交模型总结，
        # 否则"公司地址"这类提问会把带图片的整章原文原样回吐，答案被无关内容淹没。
        exact_image_answer = (
            _exact_answer_with_images(results) if _needs_full_section(payload.query) else ""
        )
        if exact_image_answer:
            # 图片位置和原文结构必须保持一致时，直接返回解析原文，避免模型总结或移动图片。
            answer = exact_image_answer
            generation_duration_ms = 0
            await finish_qa_success(
                session,
                record,
                answer=answer,
                results=results,
                retrieval_duration_ms=retrieval_duration_ms,
                generation_duration_ms=generation_duration_ms,
                total_duration_ms=_duration_ms(request_started_at),
                badcase_rerank_threshold=settings.qa_badcase_rerank_threshold,
            )
            return KnowledgeAnswerResponse(
                company_id=company_id,
                knowledge_base_id=knowledge_base_id,
                qa_record_id=record.id,
                request_id=record.request_id,
                session_id=payload.session_id,
                query=payload.query,
                answer=answer,
                retrieval_mode=payload.retrieval_mode,
                sources=[
                    _result_to_schema(index, result)
                    for index, result in enumerate(results, start=1)
                ],
            )
        context = "\n\n".join(
            _answer_context(index, result)
            for index, result in enumerate(results, start=1)
        )
        generation_started_at = perf_counter()
        usage: dict[str, int] = {}
        # 优先使用管理页配置的默认生成模型档案；未配置时回落到 .env 全局聊天配置。
        chat_profile = await session.scalar(
            select(ModelProfile).where(
                ModelProfile.kind == "chat",
                ModelProfile.is_enabled.is_(True),
                ModelProfile.is_default.is_(True),
            )
        )
        answer = await generate_answer(
            query=payload.query,
            context=context,
            profile=chat_profile,
            session=session,
            usage_sink=usage,
        )
        answer = _ensure_answer_images(answer, results)
        generation_duration_ms = _duration_ms(generation_started_at)
        await finish_qa_success(
            session,
            record,
            answer=answer,
            results=results,
            retrieval_duration_ms=retrieval_duration_ms,
            generation_duration_ms=generation_duration_ms,
            total_duration_ms=_duration_ms(request_started_at),
            badcase_rerank_threshold=settings.qa_badcase_rerank_threshold,
            token_usage=usage or None,
        )
    except Exception as exc:
        if generation_started_at is not None and generation_duration_ms is None:
            generation_duration_ms = _duration_ms(generation_started_at)
        stage = "generation" if retrieval_duration_ms is not None else "retrieval"
        await finish_qa_failure(
            session,
            record,
            stage=stage,
            error=exc,
            results=results,
            retrieval_duration_ms=retrieval_duration_ms,
            generation_duration_ms=generation_duration_ms,
            total_duration_ms=_duration_ms(request_started_at),
        )
        await session.commit()
        logger.warning(
            "Knowledge QA failed: request_id=%s knowledge_base_id=%s code=%s",
            record.request_id,
            knowledge_base_id,
            _error_code(exc),
        )
        _raise_qa_http_error(exc)

    return KnowledgeAnswerResponse(
        company_id=company_id,
        knowledge_base_id=knowledge_base_id,
        qa_record_id=record.id,
        request_id=record.request_id,
        session_id=payload.session_id,
        query=payload.query,
        answer=answer,
        retrieval_mode=payload.retrieval_mode,
        sources=[_result_to_schema(index, result) for index, result in enumerate(results, start=1)],
    )


_ASSET_MARKER_PATTERN = re.compile(r"\[\[KB_ASSET_KEY:[^\]]*\]\]")


def _answer_context(index: int, result) -> str:
    """把图片真实地址原位放回上下文，让模型原样保留；无法解析的占位符只留位置提示。"""
    content = result.context or result.content
    content = render_images(content, result.assets or [])
    content = _ASSET_MARKER_PATTERN.sub("[图片]", content)
    return f"[资料{index}]\n{content}"


def _ensure_answer_images(answer: str, results: list) -> str:
    """把模型答案中保留的图片占位符换成真实地址，剔除模型编造的假链接。

    模型没有保留的图片不再补到答案末尾：事实型答案会被无关图片（条码、二维码）
    污染，操作步骤类答案的图片位置也会丢失。
    """
    normalized = render_images(
        answer.strip(),
        [asset for result in results for asset in (result.assets or [])],
    )
    return re.sub(r"!\[[^\]]*\]\((?!https?://)[^)]*\)", "", normalized).strip()


def _exact_answer_with_images(results: list) -> str:
    """有图片的原文按章节重组，保留命中段落之间的纯文字和图片位置。"""
    grouped: dict[tuple[object, str | None], list] = {}
    image_groups: set[tuple[object, str | None]] = set()
    group_order: list[tuple[object, str | None]] = []
    for result in results:
        content = result.context or result.content
        version_id = getattr(result, "document_version_id", None)
        if version_id is None:
            version_id = getattr(result, "document_id", id(result))
        section = getattr(result, "section", None)
        section_root = str(section).split(" / ", 1)[0] if section else None
        key = (version_id, section_root)
        if key not in grouped:
            grouped[key] = []
            group_order.append(key)
        grouped[key].append(result)
        if has_images(content) or any(
            getattr(asset, "marker", None) and asset.marker in content
            for asset in result.assets or []
        ):
            image_groups.add(key)

    parts: list[str] = []
    for key in group_order:
        if key not in image_groups:
            continue
        chunk_content: dict[object, tuple[int | None, int, str]] = {}
        fallback_content: list[tuple[int | None, int, str]] = []
        assets_by_key: dict[object, object] = {}
        sequence = 0
        for result in grouped[key]:
            for asset in result.assets or []:
                marker = getattr(asset, "marker", None)
                asset_key = marker or getattr(asset, "id", id(asset))
                assets_by_key.setdefault(asset_key, asset)
            context_sources = getattr(result, "context_sources", None) or []
            if context_sources:
                for item in context_sources:
                    chunk_id = getattr(item, "chunk_id", None)
                    ordinal = getattr(item, "ordinal", None)
                    item_content = str(getattr(item, "content", ""))
                    if chunk_id is None:
                        fallback_content.append((ordinal, sequence, item_content))
                    else:
                        chunk_content.setdefault(
                            chunk_id, (ordinal, sequence, item_content)
                        )
                    sequence += 1
            else:
                fallback_content.append(
                    (
                        getattr(result, "context_ordinal", None),
                        sequence,
                        result.context or result.content,
                    )
                )
                sequence += 1

        ordered_content = sorted(
            [*chunk_content.values(), *fallback_content],
            key=lambda item: (item[0] if item[0] is not None else 10**12, item[1]),
        )
        text = "\n\n".join(content for _, _, content in ordered_content if content)
        if not text:
            continue
        rendered = render_images(text, assets_by_key.values())
        if rendered not in parts:
            parts.append(rendered)
    return "\n\n".join(parts)


def _sse_event(name: str, payload: dict) -> str:
    """把事件打包成 SSE 消息帧；data 为单行 JSON，便于前端按行解析。"""
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _persist_stream_failure(
    record_id: UUID,
    *,
    stage: str,
    error: Exception,
    results: list,
    retrieval_duration_ms: int | None,
    generation_duration_ms: int | None,
    total_duration_ms: int,
) -> None:
    """用独立会话写失败记录；客户端断连时请求会话会回滚，不能依赖它提交。"""
    async with get_session_factory()() as failure_session:
        record = await failure_session.get(QARecord, record_id)
        if record is None:
            return
        await finish_qa_failure(
            failure_session,
            record,
            stage=stage,
            error=error,
            results=results,
            retrieval_duration_ms=retrieval_duration_ms,
            generation_duration_ms=generation_duration_ms,
            total_duration_ms=total_duration_ms,
        )
        await failure_session.commit()


async def _stream_qa_events(
    *,
    session: AsyncSession,
    record: QARecord,
    knowledge_base: KnowledgeBase,
    knowledge_base_id: UUID,
    company_id: UUID,
    payload: KnowledgeAnswerRequest,
) -> AsyncIterator[str]:
    """流式问答主流程：先下发 meta，检索后按 无结果/澄清/图文直出/模型生成 分支处理。

    模型生成分支边生成边下发 delta 增量；最终答案统一做表格转列表与图片占位符还原，
    经 done 事件下发，字段与 /ask 响应一致，保证图片和表格不会因流式而丢失。
    """
    settings = get_settings()
    request_started_at = perf_counter()
    retrieval_started_at = perf_counter()
    retrieval_duration_ms: int | None = None
    generation_duration_ms: int | None = None
    generation_started_at: float | None = None
    results: list = []

    def done_event(
        answer: str,
        sources: list,
        *,
        clarification_question: str | None = None,
        clarification_options: list[str] | None = None,
        faq_matched: bool = False,
    ) -> str:
        return _sse_event(
            "done",
            KnowledgeAnswerResponse(
                company_id=company_id,
                knowledge_base_id=knowledge_base_id,
                qa_record_id=record.id,
                request_id=record.request_id,
                session_id=payload.session_id,
                query=payload.query,
                answer=answer,
                clarification_required=clarification_question is not None,
                clarification_question=clarification_question,
                clarification_options=clarification_options or [],
                faq_matched=faq_matched,
                retrieval_mode=payload.retrieval_mode,
                sources=sources,
            ).model_dump(mode="json"),
        )

    async def record_failure(error: Exception) -> None:
        nonlocal generation_duration_ms
        if generation_started_at is not None and generation_duration_ms is None:
            generation_duration_ms = _duration_ms(generation_started_at)
        stage = "generation" if retrieval_duration_ms is not None else "retrieval"
        try:
            await _persist_stream_failure(
                record.id,
                stage=stage,
                error=error,
                results=results,
                retrieval_duration_ms=retrieval_duration_ms,
                generation_duration_ms=generation_duration_ms,
                total_duration_ms=_duration_ms(request_started_at),
            )
        except Exception:
            # 落库失败不影响向客户端下发 error 事件。
            logger.warning(
                "Failed to persist QA stream failure: record_id=%s", record.id, exc_info=True
            )

    yield _sse_event(
        "meta", {"qa_record_id": str(record.id), "request_id": record.request_id}
    )
    try:
        # FAQ 前置匹配：命中时原样直出，与 /ask 保持一致；产品/型号过滤同 match_faq。
        faq_entry = await match_faq(
            session,
            kb=knowledge_base,
            query=payload.query,
            product_name=payload.product_name,
            model_name=payload.model_name,
        )
        if faq_entry is not None:
            answer = faq_entry.answer
            await finish_qa_success(
                session,
                record,
                answer=answer,
                results=[],
                retrieval_duration_ms=0,
                generation_duration_ms=0,
                total_duration_ms=_duration_ms(request_started_at),
                badcase_rerank_threshold=settings.qa_badcase_rerank_threshold,
                faq_matched=True,
            )
            logger.info(
                "Knowledge QA stream answered by FAQ: request_id=%s faq_id=%s",
                record.request_id,
                faq_entry.id,
            )
            yield done_event(answer, [], faq_matched=True)
            return
        results = await search_knowledge_base(
            session,
            knowledge_base_id=knowledge_base_id,
            company_id=company_id,
            query=payload.query,
            top_k=payload.top_k,
            document_ids=payload.document_ids,
            retrieval_mode=payload.retrieval_mode,
            include_context=payload.include_context,
            expand_section=_needs_full_section(payload.query),
            section_query=payload.query,
            product_name=payload.product_name,
            model_name=payload.model_name,
        )
        retrieval_duration_ms = _duration_ms(retrieval_started_at)
        if not results:
            await finish_qa_no_results(
                session,
                record,
                retrieval_duration_ms=retrieval_duration_ms,
                total_duration_ms=_duration_ms(request_started_at),
            )
            logger.info(
                "Knowledge QA completed without results: request_id=%s knowledge_base_id=%s",
                record.request_id,
                knowledge_base_id,
            )
            product_label = (payload.model_name or payload.product_name or "").strip()
            answer = (
                f"知识库中没有检索到与「{product_label}」相关的内容。"
                if product_label
                else "知识库中没有检索到与问题相关的内容。"
            )
            yield done_event(answer, [])
            return
        model_options = _candidate_models(results)
        if len(model_options) > 1:
            mentioned_models = _mentioned_models(payload.query, model_options)
            if mentioned_models:
                results = [result for result in results if result.model_name in mentioned_models]
            else:
                question = "请问您使用的是以下哪个型号？"
                answer = question + "\n\n" + "\n".join(
                    f"- {model}" for model in model_options
                )
                generation_duration_ms = 0
                await finish_qa_success(
                    session,
                    record,
                    answer=answer,
                    results=results,
                    retrieval_duration_ms=retrieval_duration_ms,
                    generation_duration_ms=generation_duration_ms,
                    total_duration_ms=_duration_ms(request_started_at),
                    badcase_rerank_threshold=settings.qa_badcase_rerank_threshold,
                )
                yield done_event(
                    answer,
                    [
                        _result_to_schema(index, result)
                        for index, result in enumerate(results, start=1)
                    ],
                    clarification_question=question,
                    clarification_options=model_options,
                )
                return
        add_qa_event(
            session,
            record,
            stage="retrieval",
            status="success",
            duration_ms=retrieval_duration_ms,
            details={"result_count": len(results)},
        )
        exact_image_answer = (
            _exact_answer_with_images(results) if _needs_full_section(payload.query) else ""
        )
        if exact_image_answer:
            answer = exact_image_answer
            generation_duration_ms = 0
            await finish_qa_success(
                session,
                record,
                answer=answer,
                results=results,
                retrieval_duration_ms=retrieval_duration_ms,
                generation_duration_ms=generation_duration_ms,
                total_duration_ms=_duration_ms(request_started_at),
                badcase_rerank_threshold=settings.qa_badcase_rerank_threshold,
            )
            yield done_event(
                answer,
                [
                    _result_to_schema(index, result)
                    for index, result in enumerate(results, start=1)
                ],
            )
            return
        context = "\n\n".join(
            _answer_context(index, result)
            for index, result in enumerate(results, start=1)
        )
        generation_started_at = perf_counter()
        usage: dict[str, int] = {}
        # 优先使用管理页配置的默认生成模型档案；未配置时回落到 .env 全局聊天配置。
        chat_profile = await session.scalar(
            select(ModelProfile).where(
                ModelProfile.kind == "chat",
                ModelProfile.is_enabled.is_(True),
                ModelProfile.is_default.is_(True),
            )
        )
        parts: list[str] = []
        async for delta in generate_answer_stream(
            query=payload.query,
            context=context,
            profile=chat_profile,
            session=session,
            usage_sink=usage,
        ):
            parts.append(delta)
            yield _sse_event("delta", {"text": delta})
        answer = _ensure_answer_images(normalize_answer("".join(parts)), results)
        generation_duration_ms = _duration_ms(generation_started_at)
        await finish_qa_success(
            session,
            record,
            answer=answer,
            results=results,
            retrieval_duration_ms=retrieval_duration_ms,
            generation_duration_ms=generation_duration_ms,
            total_duration_ms=_duration_ms(request_started_at),
            badcase_rerank_threshold=settings.qa_badcase_rerank_threshold,
            token_usage=usage or None,
        )
        yield done_event(
            answer,
            [
                _result_to_schema(index, result)
                for index, result in enumerate(results, start=1)
            ],
        )
    except asyncio.CancelledError:
        # 客户端断连会连带取消请求会话，改用独立后台任务落库，避免记录永久 running。
        failure_task = asyncio.create_task(
            record_failure(RuntimeError("CLIENT_DISCONNECTED: 客户端已断开连接"))
        )
        try:
            await asyncio.shield(failure_task)
        except asyncio.CancelledError:
            pass
        raise
    except Exception as exc:
        await record_failure(exc)
        logger.warning(
            "Knowledge QA stream failed: request_id=%s knowledge_base_id=%s code=%s",
            record.request_id,
            knowledge_base_id,
            _error_code(exc),
        )
        _, detail = _qa_http_error(exc)
        yield _sse_event("error", {"code": _error_code(exc), "detail": detail})


@router.post(
    "/knowledge-bases/{knowledge_base_id}/ask/stream",
    response_class=StreamingResponse,
    summary="流式检索并生成知识库答案",
    description="与 /ask 相同的检索与生成流程，答案经 SSE 边生成边下发：meta、delta、done，失败时下发 error。",
)
async def ask_knowledge_base_stream(
    knowledge_base_id: Annotated[UUID, Path(description="待问答的知识库 ID。")],
    company_id: Annotated[UUID, Query(description="公司租户 ID；必须与子库归属一致。")],
    payload: KnowledgeAnswerRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> StreamingResponse:
    """先完成与 /ask 一致的快速校验与问答记录创建，再返回 SSE 响应流。"""
    settings = get_settings()
    if payload.top_k > settings.search_max_top_k:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"top_k 不能大于服务配置上限 {settings.search_max_top_k}",
        )
    knowledge_base = await session.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.status == "active",
            KnowledgeBase.company_id == company_id,
        )
    )
    if knowledge_base is None:
        raise HTTPException(status_code=404, detail="知识库不存在或已归档")
    try:
        record = await start_qa_record(
            session,
            knowledge_base_id=knowledge_base_id,
            company_id=company_id,
            session_id=payload.session_id,
            query=payload.query,
            retrieval_mode=payload.retrieval_mode,
            top_k=payload.top_k,
            # 整体方法类问题必须展开正文上下文，即使调用方关闭了普通邻块扩展。
            include_context=payload.include_context or _needs_full_section(payload.query),
            chat_provider=settings.chat_provider,
            chat_model_name=settings.chat_model_name,
        )
    except QASessionKnowledgeBaseMismatch as exc:
        raise HTTPException(status_code=422, detail="session_id 不属于当前知识库") from exc
    # 先提交记录：客户端中途断连时请求会话会回滚，独立会话仍能把它标记为失败。
    await session.commit()
    return StreamingResponse(
        _stream_qa_events(
            session=session,
            record=record,
            knowledge_base=knowledge_base,
            knowledge_base_id=knowledge_base_id,
            company_id=company_id,
            payload=payload,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/knowledge-bases/{knowledge_base_id}/qa-records",
    response_model=QARecordListResponse,
    summary="查询知识库问答记录",
    description="按时间倒序分页查询知识库的在线问答主记录。",
)
async def list_qa_records(
    knowledge_base_id: Annotated[UUID, Path(description="知识库 ID。")],
    company_id: Annotated[UUID, Query(description="公司租户 ID；必须与知识库归属一致。")],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始。")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=100, description="每页数量，最多 100 条。")
    ] = 20,
) -> QARecordListResponse:
    """分页返回问答记录摘要，便于后台查看和排查。"""
    exists = await session.scalar(
        select(KnowledgeBase.id).where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.company_id == company_id,
        )
    )
    if exists is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    condition = and_(
        QARecord.knowledge_base_id == knowledge_base_id,
        QARecord.company_id == company_id,
    )
    total = int(await session.scalar(select(func.count(QARecord.id)).where(condition)) or 0)
    records = list(
        await session.scalars(
            select(QARecord)
            .where(condition)
            .order_by(QARecord.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return QARecordListResponse(
        page=page,
        page_size=page_size,
        total=total,
        items=[QARecordRead.model_validate(record) for record in records],
    )


@router.get(
    "/qa-records/{qa_record_id}",
    response_model=QARecordDetail,
    summary="查询问答记录详情",
    description="返回单次问答的答案、来源快照和检索/生成阶段日志。",
)
async def get_qa_record(
    qa_record_id: Annotated[UUID, Path(description="问答记录 ID。")],
    company_id: Annotated[UUID, Query(description="公司租户 ID；必须与记录归属一致。")],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> QARecordDetail:
    """返回问答主记录、引用来源和阶段日志。"""
    record = await session.scalar(
        select(QARecord).where(
            QARecord.id == qa_record_id,
            QARecord.company_id == company_id,
        )
    )
    if record is None:
        raise HTTPException(status_code=404, detail="问答记录不存在")
    sources = list(
        await session.scalars(
            select(QASource)
            .where(
                QASource.qa_record_id == qa_record_id,
                QASource.company_id == company_id,
            )
            .order_by(QASource.rank)
        )
    )
    events = list(
        await session.scalars(
            select(QAEvent)
            .where(
                QAEvent.qa_record_id == qa_record_id,
                QAEvent.company_id == company_id,
            )
            .order_by(QAEvent.created_at)
        )
    )
    base = QARecordRead.model_validate(record).model_dump()
    return QARecordDetail(
        **base,
        sources=[QASourceRead.model_validate(source) for source in sources],
        events=[QAEventRead.model_validate(event) for event in events],
    )
