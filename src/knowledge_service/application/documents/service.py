from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.application.knowledge_bases.service import get_knowledge_base
from knowledge_service.domain.policies import lock_model_binding
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentVersion,
    IngestionJob,
    OutboxEvent,
)


async def register_document(
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    document_key: str | None = None,
    filename: str,
    media_type: str | None,
    size_bytes: int,
    content_sha256: str,
    object_key: str,
) -> Document:
    """登记已保存的原始文件，在同一事务内创建文档、任务与 Outbox 事件。"""
    knowledge_base = await get_knowledge_base(
        session,
        knowledge_base_id=knowledge_base_id,
        lock_row=True,
    )
    if knowledge_base.models_locked_at is None:
        knowledge_base.models_locked_at = lock_model_binding()

    # 缺省用文件名作为逻辑键；调用方可传稳定 ID 区分同名文件。
    key = (document_key or filename).strip()
    if not key or len(key) > 500 or "\x00" in key:
        raise ValueError("Invalid document key")

    document = await session.scalar(
        select(Document)
        .where(
            Document.knowledge_base_id == knowledge_base.id,
            Document.document_key == key,
        )
        .with_for_update()
    )
    if document is None:
        document = Document(
            knowledge_base_id=knowledge_base.id,
            document_key=key,
            filename=filename,
            latest_version_number=0,
        )
        session.add(document)
        await session.flush()

    version_number = document.latest_version_number + 1
    document.filename = filename
    document.latest_version_number = version_number
    document.status = "queued"
    document.error_message = None
    version = DocumentVersion(
        document_id=document.id,
        version_number=version_number,
        filename=filename,
        media_type=media_type,
        size_bytes=size_bytes,
        content_sha256=content_sha256,
        object_key=object_key,
    )
    session.add(version)
    await session.flush()

    job = IngestionJob(
        document_id=document.id,
        document_version_id=version.id,
        status="queued",
    )
    session.add(job)
    await session.flush()
    session.add(
        OutboxEvent(
            event_type="document.ingestion_requested",
            aggregate_type="document",
            aggregate_id=document.id,
            payload={
                "document_id": str(document.id),
                "document_version_id": str(version.id),
                "job_id": str(job.id),
            },
        )
    )
    await session.flush()
    return document
