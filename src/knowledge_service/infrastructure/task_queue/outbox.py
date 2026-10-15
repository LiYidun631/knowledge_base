"""在 FastAPI 应用进程内消费 MySQL Outbox 并执行离线入库。"""

import asyncio
import logging
from datetime import timedelta
from uuid import UUID

from sqlalchemy import or_, select, text

from knowledge_service.application.documents.ingestion import process_ingestion_job
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
        if event_type != "document.ingestion_requested":
            raise ValueError("Unsupported outbox event")
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
        await recover_stale_jobs()
        return 1
    except Exception:
        logger.exception("In-process ingestion dispatch failed: event_id=%s", event_id)
        delay = min(settings.ingestion_retry_base_seconds * (2 ** min(attempts, 4)), 300)
        async with get_session_factory()() as session:
            async with session.begin():
                event = await session.get(OutboxEvent, event_id, with_for_update=True)
                if event is not None and event.status == "processing":
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
