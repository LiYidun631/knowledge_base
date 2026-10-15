import hashlib
import logging
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi import Path as ApiPath
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.v1.schemas import (
    DocumentRead,
    DocumentVersionRead,
    IngestionJobRead,
)
from knowledge_service.application.documents import service
from knowledge_service.application.documents.upload import (
    UploadLimitExceeded,
    normalize_filename,
    read_limited_upload,
)
from knowledge_service.core.config import get_settings
from knowledge_service.domain.errors import KnowledgeBaseNotFound
from knowledge_service.infrastructure.object_storage.factory import get_object_storage
from knowledge_service.infrastructure.persistence.locks import ingestion_lock_name
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentVersion,
    IngestionJob,
    KnowledgeBase,
    OutboxEvent,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_db_session
from knowledge_service.ports.object_store import ObjectStorage

logger = logging.getLogger(__name__)
router = APIRouter(tags=["文档与离线入库"])


@router.post(
    "/knowledge-bases/{knowledge_base_id}/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传文档并创建入库任务",
    description=(
        "以原始二进制请求体上传文件。接口接受后返回文档信息，后台异步解析、分块，"
        "再生成向量并写入本地持久化 Chroma 数据库。"
    ),
)
async def upload_document(
    knowledge_base_id: Annotated[UUID, ApiPath(description="目标知识库 ID。")],
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    filename: Annotated[
        str, Query(min_length=1, max_length=1000, description="上传文件名，需包含受支持的扩展名。")
    ],
    content_type: Annotated[
        str | None,
        Header(alias="Content-Type", description="文件 MIME 类型，例如 application/pdf。"),
    ] = None,
    document_key: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=500,
            description="调用方提供的稳定文档标识；同一标识再次上传会创建新版本。",
        ),
    ] = None,
) -> DocumentRead:
    settings = get_settings()
    try:
        safe_filename = normalize_filename(filename)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    extension = Path(safe_filename).suffix.lower()
    allowed_extensions = {item.lower() for item in settings.allowed_document_extensions}
    if extension not in allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"File extension {extension or '(none)'} is not allowed",
        )

    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            declared_size = int(declared_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length header",
            ) from exc
        if declared_size > settings.max_upload_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Uploaded file exceeds the configured size limit",
            )

    try:
        content = await read_limited_upload(request.stream(), settings.max_upload_bytes)
    except UploadLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Uploaded file exceeds the configured size limit",
        ) from exc
    if not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty",
        )

    object_key = f"{knowledge_base_id}/{uuid4().hex}{extension}"
    storage = get_object_storage(settings)
    try:
        await storage.put(object_key, content)
        document = await service.register_document(
            session,
            knowledge_base_id=knowledge_base_id,
            document_key=document_key,
            filename=safe_filename,
            media_type=content_type,
            size_bytes=len(content),
            content_sha256=hashlib.sha256(content).hexdigest(),
            object_key=object_key,
        )
        # 上传接口主动提交事务，以便提交失败时回收刚写入的原始文件。
        await session.commit()
    except KnowledgeBaseNotFound as exc:
        await session.rollback()
        await _remove_failed_upload(storage, object_key)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge base not found"
        ) from exc
    except ValueError as exc:
        await session.rollback()
        await _remove_failed_upload(storage, object_key)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except Exception:
        await session.rollback()
        await _remove_failed_upload(storage, object_key)
        raise

    return DocumentRead.model_validate(document)


@router.get(
    "/knowledge-bases/{knowledge_base_id}/documents",
    response_model=list[DocumentRead],
    summary="分页查询知识库文档",
    description="列出指定知识库中的逻辑文档，按创建时间倒序分页返回。",
)
async def list_documents(
    knowledge_base_id: Annotated[UUID, ApiPath(description="目标知识库 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    offset: Annotated[int, Query(ge=0, description="跳过的记录数。")] = 0,
    limit: Annotated[
        int, Query(ge=1, le=200, description="本页最多返回的记录数，最大为 200。")
    ] = 50,
) -> list[DocumentRead]:
    knowledge_base = await session.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.status == "active",
        )
    )
    if knowledge_base is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge base not found"
        )

    documents = await session.scalars(
        select(Document)
        .where(Document.knowledge_base_id == knowledge_base_id)
        .order_by(Document.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return [DocumentRead.model_validate(document) for document in documents]


@router.get(
    "/documents/{document_id}",
    response_model=DocumentRead,
    summary="查询文档详情",
    description="按文档 ID 查询文档元数据及最新版本状态。",
)
async def get_document(
    document_id: Annotated[UUID, ApiPath(description="逻辑文档 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DocumentRead:
    document = await session.scalar(
        select(Document)
        .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
        .where(
            Document.id == document_id,
        )
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return DocumentRead.model_validate(document)


@router.get(
    "/documents/{document_id}/versions",
    response_model=list[DocumentVersionRead],
    summary="查询文档版本历史",
    description="按版本号倒序返回文档的所有历史版本，并标明当前可检索版本。",
)
async def list_document_versions(
    document_id: Annotated[UUID, ApiPath(description="逻辑文档 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[DocumentVersionRead]:
    """列出逻辑文档的全部历史版本及当前可检索版本。"""
    document = await session.scalar(
        select(Document)
        .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
        .where(
            Document.id == document_id,
        )
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    versions = await session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.desc())
    )
    return [DocumentVersionRead.model_validate(version) for version in versions]


@router.get(
    "/documents/{document_id}/ingestion",
    response_model=IngestionJobRead,
    summary="查询最近一次离线入库任务",
    description="返回该文档最新版本对应的入库状态、处理阶段、重试次数和安全错误摘要。",
)
async def get_ingestion_status(
    document_id: Annotated[UUID, ApiPath(description="逻辑文档 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> IngestionJobRead:
    """返回当前项目文档最近一次离线入库任务。"""
    job = await session.scalar(
        select(IngestionJob)
        .join(Document, Document.id == IngestionJob.document_id)
        .join(DocumentVersion, DocumentVersion.id == IngestionJob.document_version_id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
        .where(
            Document.id == document_id,
        )
        .order_by(DocumentVersion.version_number.desc(), IngestionJob.created_at.desc())
        .limit(1)
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingestion job not found")
    return IngestionJobRead.model_validate(job)


@router.post(
    "/documents/{document_id}/retry",
    response_model=IngestionJobRead,
    summary="重试失败的离线入库任务",
    description="仅可重试最新版本状态为失败的任务；任务重新排队后由应用进程内后台任务继续处理。",
)
async def retry_ingestion(
    document_id: Annotated[UUID, ApiPath(description="逻辑文档 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> IngestionJobRead:
    """重新排队失败文档，并在同一事务内写入 Outbox 事件。"""
    document = await session.scalar(
        select(Document)
        .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
        .where(
            Document.id == document_id,
        )
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    job = await session.scalar(
        select(IngestionJob)
        .where(IngestionJob.document_id == document.id)
        .join(DocumentVersion, DocumentVersion.id == IngestionJob.document_version_id)
        .order_by(DocumentVersion.version_number.desc(), IngestionJob.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if job is None or job.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only failed ingestion jobs can be retried",
        )
    # 与入库协程共用 MySQL 命名锁，再按任务→文档顺序加行锁，避免重试竞态。
    lock_name = ingestion_lock_name(job.id)
    acquired = await session.scalar(
        sql_text("SELECT GET_LOCK(:lock_name, 0)"),
        {"lock_name": lock_name},
    )
    if acquired != 1:
        raise HTTPException(status_code=409, detail="Ingestion job is still running")
    try:
        document = await session.get(Document, document.id, with_for_update=True)
        job.status = "queued"
        job.stage = None
        job.attempts = 0
        job.error_details = {}
        job.started_at = None
        job.finished_at = None
        version = await session.get(DocumentVersion, job.document_version_id, with_for_update=True)
        if version is None:
            raise HTTPException(status_code=409, detail="Document version not found")
        version.status = "queued"
        version.error_message = None
        if document.latest_version_number == version.version_number:
            document.status = "queued"
            document.error_message = None
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
                available_at=utc_now(),
            )
        )
        await session.flush()
        return IngestionJobRead.model_validate(job)
    finally:
        await session.scalar(sql_text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": lock_name})


async def _remove_failed_upload(storage: ObjectStorage, object_key: str) -> None:
    try:
        await storage.delete(object_key)
    except Exception:
        logger.exception("Failed to remove an unregistered uploaded file")
