"""离线入库用例：解析、切分、向量化并持久化任务结果。"""

import asyncio
import hashlib
import logging
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid5

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_service.application.documents.chunking import (
    Chunk,
    build_chunks,
    resolve_chunk_params,
)
from knowledge_service.application.documents.parsing import DocumentParseError, parse_document
from knowledge_service.application.model_profiles import get_default_model_profile
from knowledge_service.application.search.bm25 import term_frequencies
from knowledge_service.core.config import get_settings
from knowledge_service.core.credentials import resolve_profile_api_key
from knowledge_service.core.ocr_runtime import OcrRuntimeConfig, use_ocr_runtime
from knowledge_service.domain.errors import CredentialEncryptionError
from knowledge_service.infrastructure.chroma.vector_store import (
    ChromaWriteError,
    upsert_chunks,
)
from knowledge_service.infrastructure.model_providers.embeddings import (
    EmbeddingProviderError,
    embed_texts,
)
from knowledge_service.infrastructure.object_storage.factory import get_object_storage
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentAsset,
    DocumentChunk,
    DocumentChunkTerm,
    DocumentVersion,
    IngestionJob,
    KnowledgeBase,
    ModelProfile,
    OutboxEvent,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_session_factory

logger = logging.getLogger(__name__)
_process_locks: dict[UUID, asyncio.Lock] = {}


def ingestion_process_lock(job_id: UUID) -> asyncio.Lock:
    """返回任务在进程内的执行锁，供删除/重试等写操作与入库协程互斥。"""
    return _process_locks.setdefault(job_id, asyncio.Lock())


def ingestion_job_running(job_id: UUID) -> bool:
    """只读判断本进程是否正在执行该任务，不创建新的锁对象。"""
    lock = _process_locks.get(job_id)
    return lock is not None and lock.locked()


def discard_ingestion_process_lock(job_id: UUID, lock: asyncio.Lock) -> None:
    """写操作释放后清理空闲锁，避免历史任务锁长期累积。"""
    if not lock.locked() and _process_locks.get(job_id) is lock:
        _process_locks.pop(job_id, None)


async def process_ingestion_job(job_id: UUID) -> str:
    """处理一个入库任务；重复投递会安全跳过或覆盖同一批稳定向量。"""
    # 应用进程内只有一个入库调度器，使用协程锁避免为长任务占用 MySQL 锁连接。
    factory = get_session_factory()
    lock = ingestion_process_lock(job_id)
    if lock.locked():
        return "already_running"
    try:
        async with lock:
            return await run_ingestion(job_id, factory)
    finally:
        # 任务结束后释放字典引用，避免大量历史任务长期占用内存。
        _process_locks.pop(job_id, None)


async def run_ingestion(job_id: UUID, factory: async_sessionmaker[AsyncSession]) -> str:
    """执行入库用例；调用方必须持有任务锁，允许测试替换基础设施适配器。"""
    settings = get_settings()
    claimed = False
    created_asset_keys: list[str] = []
    ocr_runtime: OcrRuntimeConfig | None = None
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
                # OCR 为全局默认档案；未配置时解析器继续回落到 .env 视觉模型。
                ocr_profile = await get_default_model_profile(session, kind="ocr")
                if ocr_profile is not None:
                    try:
                        ocr_api_key = resolve_profile_api_key(ocr_profile)
                    except CredentialEncryptionError:
                        # 仅当文档确实需要 OCR 时再报密钥错误，避免纯文本文档入库被阻断。
                        ocr_api_key = None
                    ocr_runtime = OcrRuntimeConfig(
                        base_url=str(ocr_profile.settings.get("base_url", "")),
                        chat_path=str(
                            ocr_profile.settings.get("chat_path", "/chat/completions")
                        ),
                        model_name=ocr_profile.model_name,
                        api_key=ocr_api_key,
                        mode=str(ocr_profile.settings.get("ocr_mode", "auto")),
                        profile_name=ocr_profile.name,
                        settings=dict(ocr_profile.settings),
                    )
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
                    dict(document.extra_metadata or {}),
                )
                kb_id = knowledge_base.id
                company_id = knowledge_base.company_id
                vector_library_id = knowledge_base.vector_library_id
                # 所需模型档案在本次事务内读取，知识库创建后绑定不可更换。
                profile_info = embedding
        claimed = True

        (
            doc_id,
            version_id,
            version_number,
            filename,
            object_key,
            expected_sha256,
            source_metadata,
        ) = document_info
        embedding = profile_info
        content = await get_object_storage(settings).get(object_key)
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise IngestionFailure("OBJECT_CHECKSUM_MISMATCH", "原始文件校验失败")

        await _set_stage(job_id, "parse", factory)
        with use_ocr_runtime(ocr_runtime):
            blocks = await asyncio.to_thread(parse_document, filename, content)
        # 图片先写入与原始文件相同的存储后端，解析文本只保存对象 key，避免把二进制写进 MySQL。
        storage = get_object_storage(settings)
        asset_records: list[dict[str, object]] = []
        for block_index, block in enumerate(blocks):
            asset_keys: list[str] = []
            if block.assets:
                block.metadata["asset_descriptions"] = {
                    asset.marker: asset.description or asset.name or "图片"
                    for asset in block.assets
                    if asset.marker
                }
            if block.asset_content is not None:
                asset_id = uuid5(version_id, f"{block_index}:image")
                asset_key = f"assets/{version_id}/{asset_id.hex}.png"
                media_type = block.asset_media_type or "image/png"
                await storage.put(asset_key, block.asset_content)
                created_asset_keys.append(asset_key)
                asset_keys.append(asset_key)
                marker = block.metadata.get("asset_marker")
                if marker:
                    block = replace(
                        block,
                        content=block.content.replace(str(marker), f"[[KB_ASSET_KEY:{asset_key}]]"),
                    )
                asset_records.append(
                    {
                        "object_key": asset_key,
                        "media_type": media_type,
                        "description": block.metadata.get("asset_description", ""),
                        "location": block.metadata | {"block_index": block_index},
                        "marker": (
                            f"[[KB_ASSET_KEY:{asset_key}]]"
                            if block.metadata.get("asset_marker")
                            else None
                        ),
                        "page_number": block.page_number,
                        "bbox": block.metadata.get("bbox"),
                    }
                )
            for asset_index, asset in enumerate(block.assets):
                asset_id = uuid5(version_id, f"{block_index}:{asset_index}:asset")
                asset_key = f"assets/{version_id}/{asset_id.hex}.bin"
                await storage.put(asset_key, asset.content)
                created_asset_keys.append(asset_key)
                asset_keys.append(asset_key)
                marker = asset.marker
                if marker:
                    block = replace(
                        block,
                        content=block.content.replace(marker, f"[[KB_ASSET_KEY:{asset_key}]]"),
                    )
                    if "table_header" in block.metadata:
                        block.metadata["table_header"] = str(
                            block.metadata["table_header"]
                        ).replace(marker, f"[[KB_ASSET_KEY:{asset_key}]]")
                asset_records.append(
                    {
                        "object_key": asset_key,
                        "media_type": asset.media_type,
                        "description": asset.description or asset.name or "文档图片",
                        "location": asset.metadata | {"block_index": block_index},
                        "marker": f"[[KB_ASSET_KEY:{asset_key}]]" if marker else None,
                        "page_number": block.page_number,
                        "bbox": asset.metadata.get("bbox"),
                    }
                )
            if asset_keys:
                block.metadata["asset_descriptions"] = {
                    str(asset["marker"]): str(asset["description"])
                    for asset in asset_records
                    if asset["object_key"] in asset_keys and asset.get("marker")
                }
            blocks[block_index] = block
        await _set_stage(job_id, "chunk", factory)
        # 每个版本使用独立稳定 ID，保留旧版向量时不会互相覆盖。
        # 分块参数在入库时读取子库配置，改配置只影响之后入库的新文档。
        chunks = build_chunks(
            version_id,
            blocks,
            resolve_chunk_params(
                parent=knowledge_base.chunk_parent_chars,
                child=knowledge_base.chunk_child_chars,
                overlap=knowledge_base.chunk_child_overlap_chars,
            ),
        )
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
            vectors.extend(await embed_texts(embedding, [_index_content(chunk) for chunk in batch]))

        await _set_stage(job_id, "index", factory)
        await asyncio.to_thread(
            upsert_chunks,
            embedding=embedding,
            company_id=company_id,
            vector_library_id=vector_library_id,
            source_metadata=source_metadata,
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
                    delete(DocumentChunkTerm).where(
                        DocumentChunkTerm.document_version_id == version_id
                    )
                )
                await session.execute(
                    delete(DocumentChunk).where(DocumentChunk.document_version_id == version_id)
                )
                chunk_rows = []
                term_rows = []
                chunk_by_asset_key: dict[str, UUID] = {}
                for chunk in chunks:
                    level = str(chunk.metadata["level"])
                    token_count, frequencies = (
                        term_frequencies(_index_content(chunk)) if level == "child" else (0, {})
                    )
                    chunk_rows.append(
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
                            level=level,
                            content=chunk.content,
                            token_count=token_count,
                            content_sha256=hashlib.sha256(
                                chunk.content.encode("utf-8")
                            ).hexdigest(),
                            page_number=chunk.page_number,
                            extra_metadata=chunk.metadata,
                        )
                    )
                    if level == "parent":
                        # 只把占位符实际出现的图片绑定到当前父块，避免表格切块后
                        # 把整张表的所有图片重复挂到每个父块。
                        for asset in asset_records:
                            asset_marker = asset.get("marker")
                            if asset_marker and str(asset_marker) in chunk.content:
                                chunk_by_asset_key[str(asset["object_key"])] = chunk.id
                    if level == "child":
                        term_rows.extend(
                            DocumentChunkTerm(
                                chunk_id=chunk.id,
                                term=term,
                                document_id=doc_id,
                                document_version_id=version_id,
                                frequency=frequency,
                            )
                            for term, frequency in frequencies.items()
                        )
                session.add_all(chunk_rows)
                await session.flush()
                session.add_all(term_rows)
                session.add_all(
                    DocumentAsset(
                        document_id=doc_id,
                        document_version_id=version_id,
                        chunk_id=chunk_by_asset_key.get(str(asset["object_key"])),
                        object_key=str(asset["object_key"]),
                        media_type=str(asset["media_type"]),
                        description=str(asset["description"]),
                        marker=str(asset["marker"]) if asset.get("marker") else None,
                        location=asset.get("location"),
                        page_number=asset["page_number"],
                        bbox=asset["bbox"],
                    )
                    for asset in asset_records
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
                # 入库成功后发布 FAQ 自动生成事件，由进程内调度循环异步消费，
                # 避免文档上传接口等待生成模型。
                session.add(
                    OutboxEvent(
                        event_type="document.faq_generation_requested",
                        aggregate_type="document",
                        aggregate_id=doc_id,
                        payload={
                            "document_id": str(doc_id),
                            "document_version_id": str(version_id),
                        },
                    )
                )
        return "completed"
    except Exception as exc:
        if not claimed:
            # 领取事务失败尚无任务状态可修复，交由队列重新投递。
            raise
        code, message = _failure_details(exc)
        # 数据库提交前失败时清理已上传图片，避免本地目录或 OSS 留下孤儿对象。
        if created_asset_keys:
            storage = get_object_storage(settings)
            for asset_key in created_asset_keys:
                try:
                    await storage.delete(asset_key)
                except Exception:
                    logger.warning("Failed to clean document asset: %s", asset_key)
        logger.exception("Offline ingestion failed: job_id=%s code=%s", job_id, code)
        await _record_failure(job_id, factory, code=code, message=message)
        return "failed"


class IngestionFailure(RuntimeError):
    """携带可安全持久化的稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _index_content(chunk: Chunk) -> str:
    """只在索引文本中展开图片描述，展示正文始终保留原图位置。"""
    text = chunk.content
    section = str(chunk.metadata.get("section") or "").strip()
    if section and section not in text:
        # 标题常作为独立解析块，不会自然出现在正文块的 Embedding/BM25 文本中。
        text = f"章节：{section}\n{text}"
    for marker, description in chunk.metadata.get("asset_descriptions", {}).items():
        text = text.replace(marker, description)
    return text


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
