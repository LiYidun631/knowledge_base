"""在 FastAPI 应用进程内消费 MySQL Outbox 并执行离线入库。"""

import asyncio
import logging
from datetime import timedelta
from uuid import UUID

from sqlalchemy import or_, select, text

from knowledge_service.application.documents.ingestion import (
    ingestion_job_running,
    process_ingestion_job,
)
from knowledge_service.application.faq.service import (
    FaqNotFound,
    generate_for_document_version,
)
from knowledge_service.core.config import get_settings
from knowledge_service.infrastructure.persistence.locks import ingestion_lock_name
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentVersion,
    IngestionJob,
    OutboxEvent,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_session_factory

logger = logging.getLogger(__name__)


async def dispatch_once() -> int:
    """领取并执行一条待处理事件；任务和事件状态均持久化在 MySQL。"""
    settings = get_settings()
    now = utc_now()
    lease_expired = now - timedelta(seconds=settings.stale_job_timeout_seconds)
    async with get_session_factory()() as session:
        async with session.begin():
            event = await session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.available_at <= now,
                    or_(
                        OutboxEvent.status == "pending",
                        (OutboxEvent.status == "processing")
                        & (OutboxEvent.updated_at < lease_expired),
                    ),
                )
                .order_by(OutboxEvent.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                claimed = None
            else:
                event.status = "processing"
                event.attempts += 1
                claimed = (event.id, event.event_type, dict(event.payload), event.attempts)

    if claimed is None:
        await recover_stale_jobs()
        return 0

    event_id, event_type, payload, attempts = claimed
    try:
        if event_type == "document.ingestion_requested":
            result = await process_ingestion_job(UUID(str(payload["job_id"])))
            async with get_session_factory()() as session:
                async with session.begin():
                    event = await session.get(OutboxEvent, event_id, with_for_update=True)
                    if event is not None and event.status == "processing":
                        if result == "already_running":
                            event.status = "pending"
                            event.available_at = utc_now() + timedelta(
                                seconds=settings.ingestion_poll_seconds
                            )
                        else:
                            event.status = "published"
                            event.published_at = utc_now()
        elif event_type == "document.faq_generation_requested":
            document_id = UUID(str(payload["document_id"]))
            document_version_id = UUID(str(payload["document_version_id"]))
            try:
                async with get_session_factory()() as session:
                    async with session.begin():
                        await generate_for_document_version(
                            session,
                            document_id=document_id,
                            document_version_id=document_version_id,
                        )
            except FaqNotFound:
                # 文档/版本已被删除或替换，生成目标永久消失，直接终态不再重试。
                logger.warning(
                    "FAQ 生成目标不存在，事件置为失败：document_id=%s", document_id
                )
                async with get_session_factory()() as session:
                    async with session.begin():
                        event = await session.get(
                            OutboxEvent, event_id, with_for_update=True
                        )
                        if event is not None and event.status == "processing":
                            event.status = "failed"
                await recover_stale_jobs()
                return 1
            async with get_session_factory()() as session:
                async with session.begin():
                    event = await session.get(OutboxEvent, event_id, with_for_update=True)
                    if event is not None and event.status == "processing":
                        event.status = "published"
                        event.published_at = utc_now()
        else:
            raise ValueError(f"Unsupported outbox event: {event_type}")
        await recover_stale_jobs()
        return 1
    except Exception:
        logger.exception(
            "In-process outbox dispatch failed: event_id=%s type=%s", event_id, event_type
        )
        delay = min(settings.ingestion_retry_base_seconds * (2 ** min(attempts, 4)), 300)
        async with get_session_factory()() as session:
            async with session.begin():
                event = await session.get(OutboxEvent, event_id, with_for_update=True)
                if event is not None and event.status == "processing":
                    # FAQ 生成失败只重试有限次数，超过后置为失败，避免永久空转。
                    if (
                        event_type == "document.faq_generation_requested"
                        and attempts >= settings.max_ingestion_attempts
                    ):
                        event.status = "failed"
                    else:
                        event.status = "pending"
                        event.available_at = utc_now() + timedelta(seconds=delay)
        return 0


async def recover_stale_jobs() -> int:
    """把异常退出留下的超时任务恢复为可重试状态。"""
    settings = get_settings()
    cutoff = utc_now() - timedelta(seconds=settings.stale_job_timeout_seconds)
    recovered = 0
    async with get_session_factory()() as session:
        async with session.begin():
            jobs = list(
                await session.scalars(
                    select(IngestionJob)
                    .where(IngestionJob.status == "running", IngestionJob.started_at < cutoff)
                    .limit(settings.recovery_batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            is_mysql = session.bind.dialect.name == "mysql"
            for job in jobs:
                # 本进程仍在处理的慢任务不按超时回收，否则会与当前协程并发执行。
                if ingestion_job_running(job.id):
                    continue
                lock_name = ingestion_lock_name(job.id)
                if is_mysql:
                    acquired = await session.scalar(
                        text("SELECT GET_LOCK(:lock_name, 0)"),
                        {"lock_name": lock_name},
                    )
                    if acquired != 1:
                        continue
                try:
                    document = await session.get(Document, job.document_id, with_for_update=True)
                    version = await session.get(
                        DocumentVersion, job.document_version_id, with_for_update=True
                    )
                    if job.attempts >= settings.max_ingestion_attempts:
                        job.status = "failed"
                        job.finished_at = utc_now()
                        message = "后台任务超时，已达到最大尝试次数"
                        job.error_details = {
                            "code": "INGESTION_TIMEOUT",
                            "message": message,
                            "retryable": False,
                        }
                        if version is not None:
                            version.status = "failed"
                            version.error_message = message
                        if (
                            document is not None
                            and version is not None
                            and (document.latest_version_number == version.version_number)
                        ):
                            document.status = "failed"
                            document.error_message = message
                        continue

                    job.status = "queued"
                    job.started_at = None
                    message = "后台任务超时，系统正在重试"
                    job.error_details = {
                        "code": "INGESTION_TIMEOUT",
                        "message": message,
                        "retryable": True,
                    }
                    if version is not None:
                        version.status = "queued"
                        version.error_message = message
                    if (
                        document is not None
                        and version is not None
                        and (document.latest_version_number == version.version_number)
                    ):
                        document.status = "queued"
                        document.error_message = message
                    session.add(
                        OutboxEvent(
                            event_type="document.ingestion_requested",
                            aggregate_type="document",
                            aggregate_id=job.document_id,
                            payload={
                                "document_id": str(job.document_id),
                                "document_version_id": str(job.document_version_id),
                                "job_id": str(job.id),
                            },
                        )
                    )
                    recovered += 1
                finally:
                    if is_mysql:
                        await session.scalar(
                            text("SELECT RELEASE_LOCK(:lock_name)"),
                            {"lock_name": lock_name},
                        )
    return recovered


async def run_dispatcher(stop_event: asyncio.Event | None = None) -> None:
    """由 FastAPI 生命周期启动，在应用进程内持续消费任务。"""
    settings = get_settings()
    while stop_event is None or not stop_event.is_set():
        try:
            await dispatch_once()
        except Exception:
            logger.exception("In-process ingestion loop failed; will retry")
        if stop_event is None:
            await asyncio.sleep(settings.ingestion_poll_seconds)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.ingestion_poll_seconds)
            except TimeoutError:
                pass
