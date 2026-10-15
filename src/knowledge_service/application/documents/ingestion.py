"""离线入库用例：解析、切分、向量化并持久化任务结果。"""

import asyncio
import hashlib
import logging
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_service.application.documents.chunking import Chunk, build_chunks
from knowledge_service.application.documents.parsing import DocumentParseError, parse_document
from knowledge_service.core.config import get_settings
from knowledge_service.infrastructure.chroma.vector_store import (
    ChromaWriteError,
    upsert_chunks,
)
from knowledge_service.infrastructure.model_providers.embeddings import (
    EmbeddingProviderError,
    embed_texts,
)
from knowledge_service.infrastructure.object_storage.factory import get_object_storage
from knowledge_service.infrastructure.persistence.locks import ingestion_lock_name
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    IngestionJob,
    KnowledgeBase,
    ModelProfile,
    OutboxEvent,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_engine, get_session_factory

logger = logging.getLogger(__name__)


async def process_ingestion_job(job_id: UUID) -> str:
    """处理一个入库任务；重复投递会安全跳过或覆盖同一批稳定向量。"""
    # 入库协程由 FastAPI 生命周期托管，与 API 共用应用级异步连接池。
    engine = get_engine()
    factory = get_session_factory()
    lock_name = ingestion_lock_name(job_id)
    if engine.dialect.name != "mysql":
        return await run_ingestion(job_id, factory)
    async with engine.connect() as lock_connection:
        acquired = await lock_connection.scalar(
            text("SELECT GET_LOCK(:lock_name, 0)"), {"lock_name": lock_name}
        )
        await lock_connection.commit()
        if acquired != 1:
            return "already_running"
        try:
            return await run_ingestion(job_id, factory)
        finally:
            await lock_connection.scalar(
                text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": lock_name}
            )
            await lock_connection.commit()


async def run_ingestion(job_id: UUID, factory: async_sessionmaker[AsyncSession]) -> str:
    """执行入库用例；调用方必须持有任务锁，允许测试替换基础设施适配器。"""
    settings = get_settings()
    claimed = False
    try:
        async with factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(IngestionJob).where(IngestionJob.id == job_id).with_for_update()
                )
                if job is None or job.status in {"completed", "failed", "cancelled"}:
                    return "skipped"
                if job.status == "running" and job.started_at is not None:
                    age = (utc_now() - job.started_at).total_seconds()
                    if age < settings.stale_job_timeout_seconds:
                        return "already_running"

                document = await session.scalar(
                    select(Document).where(Document.id == job.document_id).with_for_update()
                )
                version = await session.get(
                    DocumentVersion, job.document_version_id, with_for_update=True
                )
                if document is None:
                    job.status = "failed"
                    job.error_details = {"code": "DOCUMENT_NOT_FOUND", "retryable": False}
                    job.finished_at = utc_now()
                    return "failed"
                if version is None or version.document_id != document.id:
                    job.status = "failed"
                    job.error_details = {"code": "DOCUMENT_VERSION_NOT_FOUND", "retryable": False}
                    job.finished_at = utc_now()
                    return "failed"
                knowledge_base = await session.scalar(
                    select(KnowledgeBase).where(KnowledgeBase.id == document.knowledge_base_id)
                )
                if knowledge_base is None or knowledge_base.status != "active":
                    job.status = "failed"
                    job.error_details = {"code": "KNOWLEDGE_BASE_UNAVAILABLE", "retryable": False}
                    job.finished_at = utc_now()
                    version.status = "failed"
                    version.error_message = "知识库不可用"
                    if document.latest_version_number == version.version_number:
                        document.status = "failed"
                        document.error_message = version.error_message
                    return "failed"
                embedding = await session.get(ModelProfile, knowledge_base.embedding_model_id)
                if embedding is None or not embedding.is_enabled:
                    job.status = "failed"
                    job.error_details = {"code": "MODEL_PROFILE_UNAVAILABLE", "retryable": False}
                    job.finished_at = utc_now()
                    version.status = "failed"
                    version.error_message = "知识库绑定的 Embedding 模型不可用"
                    if document.latest_version_number == version.version_number:
                        document.status = "failed"
                        document.error_message = version.error_message
                    return "failed"
                job.status = "running"
                job.attempts += 1
                job.stage = "load"
                job.started_at = utc_now()
                job.finished_at = None
                job.error_details = {}
                version.status = "processing"
                version.error_message = None
                if document.latest_version_number == version.version_number:
                    document.status = "processing"
                    document.error_message = None
                document_info = (
                    document.id,
                    version.id,
                    version.version_number,
                    version.filename,
                    version.object_key,
                    version.content_sha256,
                )
                kb_id = knowledge_base.id
                # 所需模型档案在本次事务内读取，知识库创建后绑定不可更换。
                profile_info = embedding
        claimed = True

        doc_id, version_id, version_number, filename, object_key, expected_sha256 = document_info
        embedding = profile_info
        content = await get_object_storage(settings).get(object_key)
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise IngestionFailure("OBJECT_CHECKSUM_MISMATCH", "原始文件校验失败")

        await _set_stage(job_id, "parse", factory)
        blocks = await asyncio.to_thread(parse_document, filename, content)
        await _set_stage(job_id, "chunk", factory)
        # 每个版本使用独立稳定 ID，保留旧版向量时不会互相覆盖。
        chunks = build_chunks(version_id, blocks)
        child_chunks = [chunk for chunk in chunks if chunk.metadata.get("level") == "child"]
        if not child_chunks:
            raise IngestionFailure("NO_INDEXABLE_CHUNKS", "文档未生成可索引分块")

        await _set_stage(job_id, "embed", factory)
        vectors: list[list[float]] = []
        batch_size = min(
            settings.embedding_batch_size,
            int(embedding.settings.get("batch_size", settings.embedding_batch_size)),
        )
        for start in range(0, len(child_chunks), batch_size):
            batch = child_chunks[start : start + batch_size]
            vectors.extend(await embed_texts(embedding, [chunk.content for chunk in batch]))

        await _set_stage(job_id, "index", factory)
        await asyncio.to_thread(
            upsert_chunks,
            embedding=embedding,
            knowledge_base_id=kb_id,
            document_id=doc_id,
            document_version_id=version_id,
            chunks=[_chunk_record(chunk) for chunk in child_chunks],
            vectors=vectors,
        )

        async with factory() as session:
            async with session.begin():
                job = await session.scalar(
                    select(IngestionJob).where(IngestionJob.id == job_id).with_for_update()
                )
                document = await session.scalar(
                    select(Document).where(Document.id == doc_id).with_for_update()
                )
                version = await session.scalar(
                    select(DocumentVersion)
                    .where(DocumentVersion.id == version_id)
                    .with_for_update()
                )
                if job is None or document is None or version is None or job.status != "running":
                    return "superseded"
                await session.execute(
                    delete(DocumentChunk).where(DocumentChunk.document_version_id == version_id)
                )
                session.add_all(
                    [
                        DocumentChunk(
                            id=chunk.id,
                            document_id=doc_id,
                            document_version_id=version_id,
                            parent_chunk_id=(
                                UUID(str(chunk.metadata["parent_chunk_id"]))
                                if chunk.metadata.get("parent_chunk_id")
                                else None
                            ),
                            ordinal=chunk.ordinal,
                            level=str(chunk.metadata["level"]),
                            content=chunk.content,
                            content_sha256=hashlib.sha256(
                                chunk.content.encode("utf-8")
                            ).hexdigest(),
                            page_number=chunk.page_number,
                            extra_metadata=chunk.metadata,
                        )
                        for chunk in chunks
                    ]
                )
                current_version_number = await session.scalar(
                    select(DocumentVersion.version_number).where(
                        DocumentVersion.document_id == doc_id,
                        DocumentVersion.is_current.is_(True),
                    )
                )
                version.status = "completed"
                version.error_message = None
                if current_version_number is None or version_number > current_version_number:
                    await session.execute(
                        update(DocumentVersion)
                        .where(
                            DocumentVersion.document_id == doc_id,
                            DocumentVersion.is_current.is_(True),
                        )
                        .values(is_current=False)
                    )
                    version.is_current = True
                if document.latest_version_number == version_number:
                    document.status = "completed"
                    document.error_message = None
                job.status = "completed"
                job.stage = "complete"
                job.finished_at = utc_now()
        return "completed"
    except Exception as exc:
        if not claimed:
            # 领取事务失败尚无任务状态可修复，交由队列重新投递。
            raise
        code, message = _failure_details(exc)
        logger.exception("Offline ingestion failed: job_id=%s code=%s", job_id, code)
        await _record_failure(job_id, factory, code=code, message=message)
        return "failed"


class IngestionFailure(RuntimeError):
    """携带可安全持久化的稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _chunk_record(chunk: Chunk) -> dict[str, object]:
    return {
        "id": chunk.id,
        "ordinal": chunk.ordinal,
        "content": chunk.content,
        "parent_chunk_id": chunk.metadata.get("parent_chunk_id"),
    }


def _failure_details(exc: Exception) -> tuple[str, str]:
    """只保存受控错误信息，避免把供应商响应或凭据写进任务记录。"""
    if isinstance(exc, (DocumentParseError, EmbeddingProviderError, ChromaWriteError)):
        code, separator, message = str(exc).partition(": ")
        return code, message if separator else "离线入库失败"
    if isinstance(exc, IngestionFailure):
        return exc.code, str(exc)
    return "INGESTION_INTERNAL_ERROR", "入库处理失败，请检查服务端日志"


async def _set_stage(job_id: UUID, stage: str, factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        async with session.begin():
            job = await session.scalar(
                select(IngestionJob).where(IngestionJob.id == job_id).with_for_update()
            )
            if job is not None and job.status == "running":
                job.stage = stage


async def _record_failure(
    job_id: UUID, factory: async_sessionmaker[AsyncSession], *, code: str, message: str
) -> None:
    settings = get_settings()
    async with factory() as session:
        async with session.begin():
            job = await session.scalar(
                select(IngestionJob).where(IngestionJob.id == job_id).with_for_update()
            )
            if job is None or job.status != "running":
                return
            document = await session.scalar(
                select(Document).where(Document.id == job.document_id).with_for_update()
            )
            version = await session.get(
                DocumentVersion, job.document_version_id, with_for_update=True
            )
            # 格式、凭据或 schema 配置错误需要人工处理，瞬时网络失败才自动重试。
            transient = code in {
                "CHROMA_WRITE_ERROR",
                "EMBEDDING_TIMEOUT",
                "EMBEDDING_PROVIDER_ERROR",
                "INGESTION_INTERNAL_ERROR",
            } or code in {"EMBEDDING_PROVIDER_HTTP_429", "EMBEDDING_PROVIDER_HTTP_503"}
            retryable = transient and job.attempts < settings.max_ingestion_attempts
            job.error_details = {"code": code, "message": message, "retryable": retryable}
            if retryable:
                delay = settings.ingestion_retry_base_seconds * (2 ** (job.attempts - 1))
                job.status = "queued"
                job.finished_at = None
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
                        available_at=utc_now() + timedelta(seconds=delay),
                    )
                )
            else:
                job.status = "failed"
                job.finished_at = utc_now()
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
