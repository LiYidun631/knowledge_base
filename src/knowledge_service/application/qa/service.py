"""在线问答记录、引用来源和阶段日志的持久化服务。"""

from collections.abc import Iterable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.infrastructure.persistence.models import (
    QAEvent,
    QARecord,
    QASession,
    QASource,
)


class QASessionKnowledgeBaseMismatch(ValueError):
    """调用方传入的会话不属于当前知识库。"""


async def start_qa_record(
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    company_id: UUID | None = None,
    session_id: UUID | None,
    query: str,
    retrieval_mode: str,
    top_k: int,
    include_context: bool,
    chat_provider: str,
    chat_model_name: str,
) -> QARecord:
    """创建问答主记录，并按需创建调用方指定的会话。"""
    if session_id is not None:
        qa_session = await session.get(QASession, session_id)
        if qa_session is not None and (
            qa_session.knowledge_base_id != knowledge_base_id
            or (company_id is not None and qa_session.company_id != company_id)
        ):
            raise QASessionKnowledgeBaseMismatch
        if qa_session is None:
            session.add(
                QASession(
                    id=session_id,
                    company_id=company_id,
                    knowledge_base_id=knowledge_base_id,
                )
            )
            await session.flush()

    record = QARecord(
        request_id=uuid4().hex,
        company_id=company_id,
        knowledge_base_id=knowledge_base_id,
        session_id=session_id,
        query=query,
        retrieval_mode=retrieval_mode,
        top_k=top_k,
        include_context=include_context,
        status="running",
        chat_provider=chat_provider,
        chat_model_name=chat_model_name,
    )
    session.add(record)
    await session.flush()
    add_qa_event(
        session,
        record,
        stage="request",
        status="started",
        details={"retrieval_mode": retrieval_mode, "top_k": top_k},
    )
    return record


def add_qa_event(
    session: AsyncSession,
    record: QARecord,
    *,
    stage: str,
    status: str,
    duration_ms: int | None = None,
    error_code: str | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """向当前问答追加一条阶段日志，不记录密钥和完整供应商响应。"""
    session.add(
        QAEvent(
            company_id=record.company_id,
            qa_record_id=record.id,
            request_id=record.request_id,
            stage=stage,
            status=status,
            duration_ms=duration_ms,
            error_code=error_code,
            message=(message or "")[:1000] or None,
            details=details or {},
        )
    )


def save_qa_sources(
    session: AsyncSession, record: QARecord, results: Iterable[Any]
) -> int:
    """保存本次检索结果的来源快照，保证原文删除后仍可审计历史回答。"""
    count = 0
    for rank, result in enumerate(results, start=1):
        session.add(
            QASource(
                company_id=record.company_id,
                qa_record_id=record.id,
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
        count += 1
    record.result_count = count
    return count


async def finish_qa_success(
    session: AsyncSession,
    record: QARecord,
    *,
    answer: str,
    results: list[Any],
    retrieval_duration_ms: int,
    generation_duration_ms: int,
    total_duration_ms: int,
    badcase_rerank_threshold: float = 0.6,
    token_usage: dict[str, int] | None = None,
    faq_matched: bool = False,
) -> None:
    """将问答标记为成功，并保存答案、来源和阶段耗时。

    最佳 rerank 分数低于阈值时进入待补资料列表；来源没有 rerank 分数时
    （未启用重排）无法判断质量，保持未复核。token_usage 为模型返回的用量；
    faq_matched 标记答案是否来自 FAQ 直接命中，便于管理端统计直出占比。
    """
    record.answer = answer
    record.status = "success"
    record.faq_matched = faq_matched
    record.retrieval_duration_ms = retrieval_duration_ms
    record.generation_duration_ms = generation_duration_ms
    record.total_duration_ms = total_duration_ms
    if token_usage:
        prompt_tokens = token_usage.get("prompt_tokens")
        completion_tokens = token_usage.get("completion_tokens")
        record.prompt_tokens = prompt_tokens
        record.completion_tokens = completion_tokens
        record.total_tokens = token_usage.get("total_tokens") or (
            prompt_tokens + completion_tokens
            if prompt_tokens is not None and completion_tokens is not None
            else None
        )
    save_qa_sources(session, record, results)
    rerank_scores = [result.rerank_score for result in results if result.rerank_score is not None]
    if rerank_scores and max(rerank_scores) < badcase_rerank_threshold:
        record.review_status = "pending"
    add_qa_event(
        session,
        record,
        stage="generation",
        status="success",
        duration_ms=generation_duration_ms,
        details={"result_count": len(results)},
    )
    await session.flush()


async def finish_qa_no_results(
    session: AsyncSession,
    record: QARecord,
    *,
    retrieval_duration_ms: int,
    total_duration_ms: int,
) -> None:
    """记录没有召回结果的问答，不调用聊天模型；进入待补资料列表。"""
    record.status = "no_results"
    record.review_status = "pending"
    record.answer = "知识库中没有检索到与问题相关的内容。"
    record.retrieval_duration_ms = retrieval_duration_ms
    record.total_duration_ms = total_duration_ms
    add_qa_event(
        session,
        record,
        stage="retrieval",
        status="success",
        duration_ms=retrieval_duration_ms,
        details={"result_count": 0},
    )
    await session.flush()


async def finish_qa_failure(
    session: AsyncSession,
    record: QARecord,
    *,
    stage: str,
    error: Exception,
    results: list[Any],
    retrieval_duration_ms: int | None,
    generation_duration_ms: int | None,
    total_duration_ms: int,
) -> None:
    """记录失败阶段和安全错误摘要，避免失败请求完全丢失；进入待补资料列表。"""
    error_text = str(error).strip() or error.__class__.__name__
    record.status = "failed"
    record.review_status = "pending"
    record.error_code = error_text.split(":", 1)[0][:100]
    record.error_message = error_text[:4000]
    record.retrieval_duration_ms = retrieval_duration_ms
    record.generation_duration_ms = generation_duration_ms
    record.total_duration_ms = total_duration_ms
    save_qa_sources(session, record, results)
    add_qa_event(
        session,
        record,
        stage=stage,
        status="failed",
        duration_ms=(generation_duration_ms if stage == "generation" else retrieval_duration_ms),
        error_code=record.error_code,
        message=record.error_message,
        details={"result_count": len(results)},
    )
    await session.flush()
