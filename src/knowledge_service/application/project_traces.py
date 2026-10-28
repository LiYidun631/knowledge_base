"""项目问答轨迹：检索时记录召回快照，项目回答完成后回填答案。

轨迹写入失败只记录日志，绝不影响检索与回答主链路。
"""

import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.infrastructure.persistence.models import (
    AppProject,
    KnowledgeBase,
    ProjectQaSource,
    ProjectQaTrace,
    utc_now,
)

logger = logging.getLogger(__name__)

PROJECT_KEY_HEADER = "X-Project-Key"
TRACE_ID_HEADER = "X-Trace-Id"
SESSION_ID_HEADER = "X-Session-Id"
USER_REF_HEADER = "X-User-Ref"


class ProjectTraceNotFound(LookupError):
    """项目未登记、已停用，或轨迹不存在。"""


class ProjectTraceScopeInvalid(LookupError):
    """知识库不属于请求公司。"""


def _best_score(results: Sequence[Any], field: str) -> float | None:
    values = [value for result in results if (value := getattr(result, field, None)) is not None]
    return max(values) if values else None


def _attempt_top_score(results: Sequence[Any]) -> float | None:
    """尝试摘要优先展示重排分，未启用重排时退回 RRF 分数。"""
    rerank_score = _best_score(results, "rerank_score")
    return rerank_score if rerank_score is not None else _best_score(results, "rrf_score")


async def _purge_expired(session: AsyncSession, project: AppProject) -> None:
    """每个项目每天最多清理一次过期轨迹，避免独立定时任务的额外部署。"""
    now = utc_now()
    if project.last_purged_at is not None:
        elapsed = now - project.last_purged_at
        if elapsed < timedelta(hours=24):
            return
    cutoff = now - timedelta(days=max(project.retention_days, 1))
    await session.execute(
        delete(ProjectQaTrace).where(
            ProjectQaTrace.project_id == project.id,
            ProjectQaTrace.created_at < cutoff,
        )
    )
    project.last_purged_at = now


async def _find_project(session: AsyncSession, project_key: str) -> AppProject | None:
    return await session.scalar(
        select(AppProject).where(
            AppProject.project_key == project_key,
            AppProject.is_enabled.is_(True),
        )
    )


async def record_retrieval(
    session: AsyncSession,
    *,
    company_id: UUID,
    knowledge_base_id: UUID,
    project_key: str,
    external_request_id: str,
    session_ref: str | None,
    user_ref: str | None,
    query: str,
    retrieval_mode: str,
    top_k: int,
    include_context: bool,
    product_name: str | None,
    model_name: str | None,
    results: Sequence[Any],
    duration_ms: int,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """记录一次检索尝试；同一外部请求多次检索时追加到已有轨迹。"""
    try:
        async with session.begin_nested():
            project = await _find_project(session, project_key)
            if project is None:
                logger.warning("项目轨迹未记录：项目 %s 未登记或已停用", project_key)
                return
            await _purge_expired(session, project)
            trace = await session.scalar(
                select(ProjectQaTrace).where(
                    ProjectQaTrace.company_id == company_id,
                    ProjectQaTrace.project_id == project.id,
                    ProjectQaTrace.external_request_id == external_request_id,
                )
            )
            if trace is None:
                trace = ProjectQaTrace(
                    company_id=company_id,
                    project_id=project.id,
                    knowledge_base_id=knowledge_base_id,
                    external_request_id=external_request_id,
                    session_ref=session_ref,
                    user_ref=user_ref,
                    query=query,
                    status="running",
                    retrieval_mode=retrieval_mode,
                    top_k=top_k,
                    include_context=include_context,
                    product_name=product_name,
                    model_name=model_name,
                    retrieval_attempts=[],
                )
                session.add(trace)
                await session.flush()
                attempt_index = 0
            else:
                attempt_index = len(trace.retrieval_attempts or [])
                if session_ref and not trace.session_ref:
                    trace.session_ref = session_ref
                if user_ref and not trace.user_ref:
                    trace.user_ref = user_ref

            attempt: dict[str, Any] = {
                "index": attempt_index,
                "query": query,
                "retrieval_mode": retrieval_mode,
                "top_k": top_k,
                "product_name": product_name,
                "model_name": model_name,
                "result_count": len(results),
                "top_score": _attempt_top_score(results),
                "duration_ms": duration_ms,
            }
            if error_code:
                attempt["error_code"] = error_code
                attempt["error_message"] = error_message
            trace.retrieval_attempts = [*(trace.retrieval_attempts or []), attempt]
            trace.retrieval_duration_ms = (trace.retrieval_duration_ms or 0) + duration_ms
            trace.result_count = (trace.result_count or 0) + len(results)
            trace.source_count = (trace.source_count or 0) + len(results)
            attempt_rerank = _best_score(results, "rerank_score")
            if attempt_rerank is not None:
                trace.top_rerank_score = (
                    attempt_rerank
                    if trace.top_rerank_score is None
                    else max(trace.top_rerank_score, attempt_rerank)
                )

            for rank, result in enumerate(results, start=1):
                session.add(
                    ProjectQaSource(
                        company_id=company_id,
                        trace_id=trace.id,
                        attempt_index=attempt_index,
                        rank=rank,
                        document_id=result.document_id,
                        document_version_id=result.document_version_id,
                        chunk_id=result.chunk_id,
                        filename=result.filename,
                        version_number=result.version_number,
                        page_number=result.page_number,
                        section=result.section,
                        content_snapshot=result.content,
                        context_snapshot=result.context,
                        cosine_distance=result.cosine_distance,
                        bm25_score=result.bm25_score,
                        rrf_score=result.rrf_score,
                        rerank_score=result.rerank_score,
                        asset_ids=[str(asset.id) for asset in (result.assets or [])],
                    )
                )

            if error_code:
                trace.status = "failed"
                trace.error_code = error_code
                trace.error_message = error_message
            elif trace.source_count == 0:
                trace.status = "no_results"
            elif trace.status in {"no_results", "failed"}:
                # 后续检索重新命中时恢复为进行中，等待项目回填最终答案。
                trace.status = "running"
    except Exception:  # noqa: BLE001 - 轨迹失败不能影响检索主链路
        logger.warning("项目轨迹写入失败，检索结果不受影响", exc_info=True)


async def complete_trace(
    session: AsyncSession,
    *,
    company_id: UUID,
    knowledge_base_id: UUID,
    project_key: str,
    external_request_id: str,
    query: str,
    answer: str | None,
    status: str,
    answer_mode: str | None,
    intent: dict[str, Any] | None,
    chat_provider: str | None,
    chat_model_name: str | None,
    generation_duration_ms: int | None,
    total_duration_ms: int | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    error_code: str | None,
    error_message: str | None,
    extra_metadata: dict[str, Any] | None,
) -> ProjectQaTrace:
    """回填项目回答结果；检索轨迹缺失时补建一条仅含问答的轨迹。"""
    project = await _find_project(session, project_key)
    if project is None:
        raise ProjectTraceNotFound
    knowledge_base = await session.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.company_id == company_id,
        )
    )
    if knowledge_base is None:
        raise ProjectTraceScopeInvalid

    trace = await session.scalar(
        select(ProjectQaTrace).where(
            ProjectQaTrace.company_id == company_id,
            ProjectQaTrace.project_id == project.id,
            ProjectQaTrace.external_request_id == external_request_id,
        )
    )
    if trace is None:
        # 检索记录缺失时仍保留问答，便于排查项目侧问题。
        trace = ProjectQaTrace(
            company_id=company_id,
            project_id=project.id,
            knowledge_base_id=knowledge_base_id,
            external_request_id=external_request_id,
            query=query,
            status="running",
            include_context=False,
            result_count=0,
            source_count=0,
            retrieval_attempts=[],
        )
        session.add(trace)

    trace.answer = answer
    trace.status = status
    trace.answer_mode = answer_mode
    if intent is not None:
        trace.intent = intent
    trace.chat_provider = chat_provider
    trace.chat_model_name = chat_model_name
    trace.generation_duration_ms = generation_duration_ms
    trace.total_duration_ms = total_duration_ms
    trace.prompt_tokens = prompt_tokens
    trace.completion_tokens = completion_tokens
    trace.total_tokens = total_tokens
    trace.error_code = error_code
    trace.error_message = error_message
    if extra_metadata is not None:
        trace.extra_metadata = extra_metadata
    trace.finished_at = utc_now()
    await session.flush()
    return trace
