import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi import Path as ApiPath
from sqlalchemy import delete, func, or_, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.v1.schemas import (
    BatchDeleteResponse,
    BatchDocumentDeleteRequest,
    BatchFileVectorDeleteRequest,
    ChunkPreviewAsset,
    ChunkPreviewParent,
    ChunkPreviewResponse,
    DocumentFolderCreate,
    DocumentFolderPatch,
    DocumentFolderRead,
    DocumentLinkIngestionRequest,
    DocumentRead,
    DocumentUpdate,
    DocumentVersionRead,
    IngestionJobRead,
    ProductOptionEntry,
    ProductOptionsRead,
)
from knowledge_service.application.documents import folders as folder_service
from knowledge_service.application.documents import service
from knowledge_service.application.documents.ingestion import (
    discard_ingestion_process_lock,
    ingestion_process_lock,
)
from knowledge_service.application.documents.rendering import render_images
from knowledge_service.application.documents.upload import (
    SourceDownloadError,
    UploadLimitExceeded,
    download_limited_source,
    normalize_filename,
    read_limited_upload,
)
from knowledge_service.application.search.service import document_product_labels
from knowledge_service.core.config import get_settings
from knowledge_service.domain.errors import (
    FolderNameConflict,
    FolderNotEmpty,
    FolderNotFound,
    KnowledgeBaseArchived,
    KnowledgeBaseNotFound,
)
from knowledge_service.infrastructure.chroma.vector_store import delete_document_versions
from knowledge_service.infrastructure.object_storage.factory import get_object_storage
from knowledge_service.infrastructure.persistence.locks import ingestion_lock_name
from knowledge_service.infrastructure.persistence.models import (
    Company,
    Document,
    DocumentAsset,
    DocumentChunk,
    DocumentChunkTerm,
    DocumentVersion,
    IngestionJob,
    KnowledgeBase,
    ModelProfile,
    OutboxEvent,
    ProductCatalog,
    VectorLibrary,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_db_session, get_engine
from knowledge_service.ports.object_store import ObjectStorage

logger = logging.getLogger(__name__)
router = APIRouter(tags=["文档与离线入库"])


async def _validate_ingestion_scope(
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    company_id: UUID,
    require_active: bool = True,
) -> tuple[KnowledgeBase, VectorLibrary]:
    """校验公司、公司级大库和子库关系，所有新入库入口共用此边界。"""
    knowledge_base_filters = [
        KnowledgeBase.id == knowledge_base_id,
        KnowledgeBase.company_id == company_id,
    ]
    if require_active:
        knowledge_base_filters.append(KnowledgeBase.status == "active")
    knowledge_base = await session.scalar(select(KnowledgeBase).where(*knowledge_base_filters))
    company = await session.scalar(
        select(Company).where(Company.id == company_id, Company.status == "active")
    )
    library = None
    if knowledge_base is not None and knowledge_base.vector_library_id is not None:
        library_filters = [
            VectorLibrary.id == knowledge_base.vector_library_id,
            VectorLibrary.company_id == company_id,
        ]
        if require_active:
            library_filters.append(VectorLibrary.status == "active")
        library = await session.scalar(select(VectorLibrary).where(*library_filters))
    if company is None or knowledge_base is None or library is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="公司或知识库不存在")
    return knowledge_base, library


def _normalize_product_lists(
    product_name: str | None,
    model_name: str | None,
    product_names: list[str] | None,
    model_names: list[str] | None,
) -> tuple[list[str], list[str]]:
    """合并旧版单值参数与新版列表参数，去重去空并校验单项长度。"""
    merged_products = [
        *(product_names or []),
        *([product_name] if product_name and product_name.strip() else []),
    ]
    merged_models = [
        *(model_names or []),
        *([model_name] if model_name and model_name.strip() else []),
    ]
    products = list(
        dict.fromkeys(value.strip() for value in merged_products if value.strip())
    )
    models = list(dict.fromkeys(value.strip() for value in merged_models if value.strip()))
    if any(len(value) > 200 for value in [*products, *models]):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="产品名称或型号长度不能超过 200 字符",
        )
    return products, models


async def _validate_catalog_labels(
    session: AsyncSession, products: list[str], models: list[str]
) -> None:
    """上传标签必须已在全局产品目录登记；未传标签的旧式上传不受限。"""
    if not products and not models:
        return
    rows = await session.execute(
        select(ProductCatalog.product_name, ProductCatalog.model_name)
    )
    catalog_products: set[str] = set()
    catalog_models: set[str] = set()
    for product_name, model_name in rows:
        catalog_products.add(product_name)
        catalog_models.add(model_name)
    unknown_products = sorted(
        (value for value in products if value not in catalog_products), key=str.casefold
    )
    unknown_models = sorted(
        (value for value in models if value not in catalog_models), key=str.casefold
    )
    if unknown_products or unknown_models:
        parts: list[str] = []
        if unknown_products:
            parts.append(f"产品名称：{'、'.join(unknown_products)}")
        if unknown_models:
            parts.append(f"产品型号：{'、'.join(unknown_models)}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"以下标签未在产品目录中登记，请先在目录中添加：{'；'.join(parts)}",
        )


def _validate_filename(filename: str, settings) -> str:
    """规范化文件名并校验扩展名，避免链接入口绕过上传校验。"""
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
    return safe_filename


async def _persist_ingestion_content(
    session: AsyncSession,
    *,
    company_id: UUID,
    vector_library_id: UUID,
    knowledge_base_id: UUID,
    file_id: str,
    filename: str,
    media_type: str | None,
    content: bytes,
    source_metadata: dict[str, str | None],
    folder_id: UUID | None = None,
) -> DocumentRead:
    """保存入库所需的服务端临时副本，并登记异步入库任务。"""
    settings = get_settings()
    extension = Path(filename).suffix.lower()
    object_key = f"{company_id}/{vector_library_id}/{knowledge_base_id}/{uuid4().hex}{extension}"
    storage = get_object_storage(settings)
    try:
        await storage.put(object_key, content)
        document = await service.register_document(
            session,
            knowledge_base_id=knowledge_base_id,
            company_id=company_id,
            document_key=file_id,
            filename=filename,
            media_type=media_type,
            size_bytes=len(content),
            content_sha256=hashlib.sha256(content).hexdigest(),
            object_key=object_key,
            source_metadata=source_metadata,
            folder_id=folder_id,
        )
        # 原文件业务归后台系统管理；此处只保存异步解析所需的服务端副本。
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
    company_id: Annotated[UUID, Query(description="公司租户 ID；必须与子库归属一致。")],
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    filename: Annotated[
        str, Query(min_length=1, max_length=1000, description="上传文件名，需包含受支持的扩展名。")
    ],
    file_id: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=500,
            description="后台文件 ID；公共调用方应填写，重复提交同一 ID 会创建新版本。",
        ),
    ] = None,
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
    source_directory_id: Annotated[
        str | None, Query(max_length=500, description="后台目录 ID，只保存为来源元数据。")
    ] = None,
    source_parent_id: Annotated[
        str | None, Query(max_length=500, description="后台上级目录 ID。")
    ] = None,
    source_path: Annotated[
        str | None, Query(max_length=2000, description="后台目录路径，不创建知识库层级。")
    ] = None,
    product_name: Annotated[
        str | None, Query(max_length=200, description="产品名称，可选，兼容旧调用。")
    ] = None,
    model_name: Annotated[
        str | None, Query(max_length=200, description="产品型号，可选，用于问答歧义判断。")
    ] = None,
    product_names: Annotated[
        list[str] | None,
        Query(description="产品名称列表；同一文件可属于多个产品，可作为检索过滤条件。"),
    ] = None,
    model_names: Annotated[
        list[str] | None,
        Query(description="产品型号列表；同一文件可对应多个型号，可作为检索过滤条件。"),
    ] = None,
    folder_id: Annotated[
        UUID | None,
        Query(description="放入的知识库内目录 ID；目录必须属于同一子库，不传放入根目录。"),
    ] = None,
) -> DocumentRead:
    settings = get_settings()
    _, library = await _validate_ingestion_scope(
        session, knowledge_base_id=knowledge_base_id, company_id=company_id
    )
    safe_filename = _validate_filename(filename, settings)
    products, models = _normalize_product_lists(
        product_name, model_name, product_names, model_names
    )
    await _validate_catalog_labels(session, products, models)

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

    resolved_file_id = (file_id or document_key or safe_filename).strip()
    return await _persist_ingestion_content(
        session,
        company_id=company_id,
        vector_library_id=library.id,
        knowledge_base_id=knowledge_base_id,
        file_id=resolved_file_id,
        filename=safe_filename,
        media_type=content_type,
        content=content,
        source_metadata={
            "source_file_id": resolved_file_id,
            "source_directory_id": source_directory_id,
            "source_parent_id": source_parent_id,
            "source_path": source_path,
            # 兼容旧字段（取第一个值），同时保存完整列表供检索过滤使用。
            "product_name": products[0] if products else None,
            "model_name": models[0] if models else None,
            "product_names": products,
            "model_names": models,
        },
        folder_id=folder_id,
    )


@router.post(
    "/knowledge-bases/{knowledge_base_id}/documents/from-url",
    response_model=DocumentRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="通过原文件链接创建入库任务",
    description=(
        "后台文件系统只提交文件 ID 和可访问链接时使用此接口。知识库服务下载一份临时副本，"
        "完成异步解析后用于生成向量；后台原文件仍由源系统负责管理。"
    ),
)
async def ingest_document_from_url(
    knowledge_base_id: Annotated[UUID, ApiPath(description="目标子库 ID。")],
    company_id: Annotated[UUID, Query(description="公司租户 ID；必须与子库归属一致。")],
    payload: DocumentLinkIngestionRequest,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> DocumentRead:
    """下载后台原文件链接并登记离线入库任务。"""
    settings = get_settings()
    _, library = await _validate_ingestion_scope(
        session, knowledge_base_id=knowledge_base_id, company_id=company_id
    )
    safe_filename = _validate_filename(payload.filename, settings)
    products, models = _normalize_product_lists(
        payload.product_name, payload.model_name, payload.product_names, payload.model_names
    )
    await _validate_catalog_labels(session, products, models)
    try:
        content = await download_limited_source(
            str(payload.file_url),
            max_bytes=settings.max_upload_bytes,
            timeout=settings.source_download_timeout_seconds,
        )
    except UploadLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="链接文件超过服务端大小限制",
        ) from exc
    except SourceDownloadError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return await _persist_ingestion_content(
        session,
        company_id=company_id,
        vector_library_id=library.id,
        knowledge_base_id=knowledge_base_id,
        file_id=payload.file_id.strip(),
        filename=safe_filename,
        media_type=payload.media_type,
        content=content,
        source_metadata={
            "source_file_id": payload.file_id.strip(),
            "source_directory_id": payload.source_directory_id,
            "source_parent_id": payload.source_parent_id,
            "source_path": payload.source_path,
            "product_name": products[0] if products else None,
            "model_name": models[0] if models else None,
            "product_names": products,
            "model_names": models,
        },
        folder_id=payload.folder_id,
    )


@router.get(
    "/knowledge-bases/{knowledge_base_id}/documents",
    response_model=list[DocumentRead],
    summary="分页查询知识库文档",
    description=(
        "列出指定知识库中的逻辑文档，按创建时间倒序分页返回；支持按文件名或文档标识搜索，"
        "响应头 X-Total-Count 返回过滤后的文档总数。"
    ),
)
async def list_documents(
    knowledge_base_id: Annotated[UUID, ApiPath(description="目标知识库 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    response: Response,
    offset: Annotated[int, Query(ge=0, description="跳过的记录数。")] = 0,
    limit: Annotated[
        int, Query(ge=1, le=200, description="本页最多返回的记录数，最大为 200。")
    ] = 50,
    folder_id: Annotated[
        UUID | None,
        Query(description="只返回指定目录下的文档；不传返回全部，含根目录。"),
    ] = None,
    root_only: Annotated[
        bool,
        Query(description="仅返回根目录文档；传 folder_id 时忽略。"),
    ] = False,
    q: Annotated[
        str | None,
        Query(max_length=200, description="按文件名或文档标识模糊搜索；不传返回全部。"),
    ] = None,
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

    filters = [Document.knowledge_base_id == knowledge_base_id]
    if folder_id is not None:
        filters.append(Document.folder_id == folder_id)
    elif root_only:
        filters.append(Document.folder_id.is_(None))
    keyword = (q or "").strip().lower()
    if keyword:
        pattern = f"%{keyword}%"
        filters.append(
            or_(
                func.lower(Document.filename).like(pattern),
                func.lower(Document.document_key).like(pattern),
            )
        )
    total = await session.scalar(select(func.count(Document.id)).where(*filters)) or 0
    # 响应体保持数组不变，用响应头携带过滤后的总数供管理页分页。
    response.headers["X-Total-Count"] = str(total)
    documents = await session.scalars(
        select(Document)
        .where(*filters)
        .order_by(Document.created_at.desc(), Document.id)
        .offset(offset)
        .limit(limit)
    )
    items: list[DocumentRead] = []
    for document in documents:
        read = DocumentRead.model_validate(document)
        # 产品标签从元数据推导，供管理页「上传新版本」预填，避免重传时错标。
        read.product_names, read.model_names = document_product_labels(
            document.extra_metadata
        )
        items.append(read)
    return items


@router.get(
    "/knowledge-bases/{knowledge_base_id}/product-options",
    response_model=ProductOptionsRead,
    summary="知识库产品选项",
    description=(
        "返回知识库内文档实际标注的去重产品名称与型号，供在线问答的"
        "检索过滤与型号澄清使用；与全局产品目录无关。"
    ),
)
async def list_product_options(
    knowledge_base_id: Annotated[UUID, ApiPath(description="目标知识库 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> ProductOptionsRead:
    """按库内已完成文档的元数据聚合选项；无标注时返回空列表。"""
    rows = await session.execute(
        select(Document.extra_metadata).where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.status == "completed",
        )
    )
    product_names: set[str] = set()
    model_names: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for (metadata,) in rows:
        products, models = document_product_labels(metadata)
        product_names.update(products)
        model_names.update(models)
        # 同一文档多产品多型号按笛卡尔积组合成条目，与检索过滤的独立匹配口径一致。
        pairs.update((product, model) for product in products for model in models)
    return ProductOptionsRead(
        product_names=sorted(product_names, key=str.casefold),
        model_names=sorted(model_names, key=str.casefold),
        entries=[
            ProductOptionEntry(product_name=product_name, model_name=model_name)
            for product_name, model_name in sorted(
                pairs, key=lambda pair: (str.casefold(pair[0]), str.casefold(pair[1]))
            )
        ],
    )


async def _acquire_ingestion_process_lock(job_id: UUID) -> asyncio.Lock:
    """非阻塞占用入库进程锁；任务正在本进程处理时立即返回 409，不排队等长任务。"""
    lock = ingestion_process_lock(job_id)
    if lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document ingestion is still running; try again later",
        )
    await lock.acquire()
    return lock


def _release_ingestion_process_lock(job_id: UUID, lock: asyncio.Lock) -> None:
    lock.release()
    discard_ingestion_process_lock(job_id, lock)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除文档及其全部版本",
    description=(
        "删除文档的 MySQL 记录、服务端临时副本和 Chroma 向量。该操作不可恢复，"
        "如需保留历史记录请不要调用此接口。"
    ),
)
async def delete_document(
    document_id: Annotated[UUID, ApiPath(description="要删除的逻辑文档 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> None:
    """完整删除文档，避免数据库、文件目录和向量库残留孤儿数据。"""
    document = await session.scalar(
        select(Document)
        .where(Document.id == document_id)
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    jobs = list(await session.scalars(
        select(IngestionJob)
        .where(IngestionJob.document_id == document_id)
        .order_by(IngestionJob.id)
    ))
    acquired_locks: list[str] = []
    process_locks: dict[UUID, asyncio.Lock] = {}
    is_mysql = session.get_bind().dialect.name == "mysql"
    lock_connection = None
    try:
        # 先占用进程内入库锁；命名锁只约束多进程间的删除/重试，挡不住本进程的入库协程。
        for job in jobs:
            process_locks[job.id] = await _acquire_ingestion_process_lock(job.id)
        # 与入库任务使用同一命名锁，避免删除时后台任务同时读取文件或写入向量。
        if is_mysql:
            lock_connection = await get_engine().connect()
            for job in jobs:
                lock_name = ingestion_lock_name(job.id)
                acquired = await lock_connection.scalar(
                    sql_text("SELECT GET_LOCK(:lock_name, 0)"), {"lock_name": lock_name}
                )
                if acquired != 1:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Document ingestion is still running; try again later",
                    )
                acquired_locks.append(lock_name)
            await lock_connection.commit()

        # 先拿任务锁再锁文档行，保持与入库任务一致的加锁顺序。
        document = await session.scalar(
            select(Document).where(Document.id == document_id).with_for_update()
        )
        if document is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        jobs = list(await session.scalars(
            select(IngestionJob)
            .where(IngestionJob.document_id == document_id)
            .order_by(IngestionJob.id)
        ))
        # 上传可能在首次读取任务列表后刚好提交；补锁新任务，避免留下一条竞态窗口。
        for job in jobs:
            if job.id not in process_locks:
                process_locks[job.id] = await _acquire_ingestion_process_lock(job.id)
        if is_mysql:
            for job in jobs:
                lock_name = ingestion_lock_name(job.id)
                if lock_name in acquired_locks:
                    continue
                acquired = await lock_connection.scalar(
                    sql_text("SELECT GET_LOCK(:lock_name, 0)"), {"lock_name": lock_name}
                )
                if acquired != 1:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Document ingestion is still running; try again later",
                    )
                acquired_locks.append(lock_name)
            if is_mysql:
                await lock_connection.commit()

        versions = list(
            await session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document_id)
                .with_for_update()
            )
        )
        knowledge_base = await session.get(KnowledgeBase, document.knowledge_base_id)
        embedding = (
            await session.get(ModelProfile, knowledge_base.embedding_model_id)
            if knowledge_base is not None
            else None
        )
        if embedding is None:
            raise HTTPException(status_code=500, detail="Knowledge base embedding model not found")

        # 先持久化删除中状态。后续外部清理失败时，重复调用 DELETE 可安全重试。
        document.status = "deleting"
        for version in versions:
            version.status = "deleting"
        for job in jobs:
            job.status = "cancelled"
            job.finished_at = utc_now()
        await session.commit()

        await asyncio.to_thread(
            delete_document_versions,
            embedding=embedding,
            version_ids=[version.id for version in versions],
        )
        storage = get_object_storage(get_settings())
        for version in versions:
            await storage.delete(version.object_key)
        assets = list(
            await session.scalars(
                select(DocumentAsset).where(DocumentAsset.document_id == document_id)
            )
        )
        for asset in assets:
            await storage.delete(asset.object_key)

        # Outbox 没有直接外键，必须显式删除，防止删除后任务再次被派发。
        await session.execute(
            delete(OutboxEvent).where(OutboxEvent.aggregate_id == document_id)
        )
        await session.execute(
            delete(DocumentChunkTerm).where(DocumentChunkTerm.document_id == document_id)
        )
        await session.execute(delete(DocumentAsset).where(DocumentAsset.document_id == document_id))
        await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
        await session.execute(delete(IngestionJob).where(IngestionJob.document_id == document_id))
        await session.execute(
            delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
        )
        await session.delete(document)
        await session.commit()
    except HTTPException:
        await session.rollback()
        raise
    except Exception as exc:
        await session.rollback()
        logger.exception("Failed to delete document: document_id=%s", document_id)
        raise HTTPException(
            status_code=500,
            detail="Document cleanup failed; repeat DELETE to safely retry the cleanup",
        ) from exc
    finally:
        if lock_connection is not None:
            for lock_name in acquired_locks:
                try:
                    await lock_connection.scalar(
                        sql_text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": lock_name}
                    )
                except Exception:
                    logger.warning("Failed to release ingestion lock: %s", lock_name)
            try:
                await lock_connection.commit()
            except Exception:
                logger.warning("Failed to commit ingestion lock release")
            finally:
                await lock_connection.close()
        for job_id, process_lock in process_locks.items():
            _release_ingestion_process_lock(job_id, process_lock)


async def _ensure_documents_idle(session: AsyncSession, document_ids: list[UUID]) -> None:
    """批量删除前拒绝仍在处理的任务，避免删除与入库同时操作同一文件。"""
    if not document_ids:
        return
    running_job = await session.scalar(
        select(IngestionJob.id)
        .where(
            IngestionJob.document_id.in_(document_ids),
            IngestionJob.status.in_(["queued", "running"]),
        )
        .limit(1)
    )
    if running_job is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="存在正在处理的文件，请等待入库完成后再删除向量",
        )


async def delete_documents(session: AsyncSession, document_ids: list[UUID]) -> list[str]:
    """批量执行完整文档清理，返回已删除的文档 UUID。"""
    normalized_ids = list(dict.fromkeys(document_ids))
    await _ensure_documents_idle(session, normalized_ids)
    for document_id in normalized_ids:
        # 复用单文档清理，确保 Chroma、BM25、图片资产、任务和临时文件全部同步清理。
        await delete_document(document_id, session)
    return [str(document_id) for document_id in normalized_ids]


@router.delete(
    "/knowledge-bases/{knowledge_base_id}/file-vectors/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除单个文件向量",
    description=(
        "按后台文件 ID 删除指定子库中的全部版本、分块、BM25 词项、图片资产和 Chroma 向量。"
        "后台文件系统中的原文件不受影响。"
    ),
)
async def delete_file_vectors(
    knowledge_base_id: Annotated[UUID, ApiPath(description="目标子库 ID。")],
    file_id: Annotated[
        str, ApiPath(min_length=1, max_length=500, description="后台文件系统中的稳定文件 ID。")
    ],
    company_id: Annotated[UUID, Query(description="公司租户 ID；必须与子库归属一致。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    """删除单个后台文件在知识库侧的向量索引。"""
    await _validate_ingestion_scope(
        session,
        knowledge_base_id=knowledge_base_id,
        company_id=company_id,
        require_active=False,
    )
    normalized_file_id = file_id.strip()
    document = await session.scalar(
        select(Document).where(
            Document.knowledge_base_id == knowledge_base_id,
            Document.document_key == normalized_file_id,
        )
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件向量不存在")
    await delete_documents(session, [document.id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/knowledge-bases/{knowledge_base_id}/file-vectors/batch-delete",
    response_model=BatchDeleteResponse,
    summary="批量删除文件向量",
    description=(
        "按后台文件 ID 批量删除指定子库中的向量索引和入库数据。"
        "后台文件系统中的原文件不受影响。"
    ),
)
async def batch_delete_file_vectors(
    knowledge_base_id: Annotated[UUID, ApiPath(description="目标子库 ID。")],
    company_id: Annotated[UUID, Query(description="公司租户 ID；必须与子库归属一致。")],
    payload: BatchFileVectorDeleteRequest,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> BatchDeleteResponse:
    """批量删除后台文件在知识库侧的向量索引。"""
    await _validate_ingestion_scope(
        session,
        knowledge_base_id=knowledge_base_id,
        company_id=company_id,
        require_active=False,
    )
    documents = list(
        await session.scalars(
            select(Document).where(
                Document.knowledge_base_id == knowledge_base_id,
                Document.document_key.in_(payload.file_ids),
            )
        )
    )
    found_ids = {document.document_key for document in documents}
    missing_ids = [file_id for file_id in payload.file_ids if file_id not in found_ids]
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "部分文件向量不存在", "file_ids": missing_ids},
        )
    await delete_documents(session, [document.id for document in documents])
    return BatchDeleteResponse(
        deleted_count=len(payload.file_ids),
        deleted_ids=payload.file_ids,
    )


@router.post(
    "/knowledge-bases/{knowledge_base_id}/documents/batch-delete",
    response_model=BatchDeleteResponse,
    summary="批量删除文档",
    description=(
        "按文档 ID 批量删除指定知识库中的文档及其全部版本、切块、BM25 词项、"
        "图片资产和 Chroma 向量；操作不可恢复。"
    ),
)
async def batch_delete_documents(
    knowledge_base_id: Annotated[UUID, ApiPath(description="目标知识库 ID。")],
    payload: BatchDocumentDeleteRequest,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> BatchDeleteResponse:
    """批量完整删除文档；不属于该知识库的文档 ID 会被拒绝。"""
    documents = list(
        await session.scalars(
            select(Document).where(
                Document.knowledge_base_id == knowledge_base_id,
                Document.id.in_(payload.document_ids),
            )
        )
    )
    found_ids = {document.id for document in documents}
    missing_ids = [
        document_id for document_id in payload.document_ids if document_id not in found_ids
    ]
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "部分文档不存在或不属于该知识库",
                "document_ids": [str(document_id) for document_id in missing_ids],
            },
        )
    # 按请求提交顺序返回删除结果，避免无 ORDER BY 查询导致顺序随机。
    documents_by_id = {document.id: document for document in documents}
    ordered_documents = [
        documents_by_id[document_id] for document_id in payload.document_ids
    ]
    deleted_ids = await delete_documents(
        session, [document.id for document in ordered_documents]
    )
    return BatchDeleteResponse(deleted_count=len(deleted_ids), deleted_ids=deleted_ids)


@router.get(
    "/document-assets/{asset_id}",
    summary="读取文档图片资产",
    description="返回版面分析提取的图片；接口沿用知识库模块的鉴权依赖。",
)
async def get_document_asset(
    asset_id: Annotated[UUID, ApiPath(description="图片资产 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    """从本地目录或 RustFS 读取图片，不暴露对象存储密钥。"""
    asset = await session.get(DocumentAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="图片资产不存在")
    try:
        content = await get_object_storage(get_settings()).get(asset.object_key)
    except Exception as exc:
        logger.exception("Failed to read document asset: asset_id=%s", asset_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="图片资产读取失败"
        ) from exc
    return Response(content=content, media_type=asset.media_type)


@router.get(
    "/documents/{document_id}",
    response_model=DocumentRead,
    summary="查询文档详情",
    description="按文档 ID 查询文档元数据及最新版本状态。",
)
async def get_document(
    document_id: Annotated[UUID, ApiPath(description="逻辑文档 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
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
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
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
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
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
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> IngestionJobRead:
    """重新排队失败文档，并在同一事务内写入 Outbox 事件。"""
    row = (
        await session.execute(
            select(Document, KnowledgeBase.status)
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
            .where(Document.id == document_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    document, knowledge_base_status = row
    if knowledge_base_status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="知识库已归档，不能重试入库"
        )
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
    # 进程锁挡住本进程的入库协程，命名锁挡住其他进程的删除/重试，再按任务→文档顺序加行锁。
    process_lock = await _acquire_ingestion_process_lock(job.id)
    lock_name = ingestion_lock_name(job.id)
    is_mysql = session.get_bind().dialect.name == "mysql"
    try:
        if is_mysql:
            acquired = await session.scalar(
                sql_text("SELECT GET_LOCK(:lock_name, 0)"),
                {"lock_name": lock_name},
            )
            if acquired != 1:
                raise HTTPException(status_code=409, detail="Ingestion job is still running")
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
        if is_mysql:
            await session.scalar(
                sql_text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": lock_name}
            )
        _release_ingestion_process_lock(job.id, process_lock)


async def _remove_failed_upload(storage: ObjectStorage, object_key: str) -> None:
    try:
        await storage.delete(object_key)
    except Exception:
        logger.exception("Failed to remove an unregistered uploaded file")


@router.get(
    "/knowledge-bases/{knowledge_base_id}/folders",
    response_model=list[DocumentFolderRead],
    summary="查询知识库目录列表",
    description="返回知识库全部目录（扁平列表，含上级目录 ID）；前端自行组装成树。",
)
async def list_document_folders(
    knowledge_base_id: Annotated[UUID, ApiPath(description="目标知识库 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> list[DocumentFolderRead]:
    knowledge_base = await session.scalar(
        select(KnowledgeBase.id).where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.status == "active",
        )
    )
    if knowledge_base is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge base not found"
        )
    folders = await folder_service.list_folders(session, knowledge_base_id=knowledge_base_id)
    return [DocumentFolderRead.model_validate(folder) for folder in folders]


@router.post(
    "/knowledge-bases/{knowledge_base_id}/folders",
    response_model=DocumentFolderRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建知识库目录",
    description="在指定父目录下创建目录；同一级内不允许重名。",
)
async def create_document_folder(
    knowledge_base_id: Annotated[UUID, ApiPath(description="目标知识库 ID。")],
    payload: DocumentFolderCreate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> DocumentFolderRead:
    try:
        folder = await folder_service.create_folder(
            session,
            knowledge_base_id=knowledge_base_id,
            name=payload.name,
            parent_id=payload.parent_id,
        )
    except KnowledgeBaseNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在"
        ) from exc
    except KnowledgeBaseArchived as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="知识库已归档，不能修改目录"
        ) from exc
    except FolderNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="上级目录不存在或不属于该知识库"
        ) from exc
    except FolderNameConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="同一级目录下已存在同名目录"
        ) from exc
    return DocumentFolderRead.model_validate(folder)


@router.patch(
    "/knowledge-bases/{knowledge_base_id}/folders/{folder_id}",
    response_model=DocumentFolderRead,
    summary="重命名或移动目录",
    description="提交 name 重命名，提交 parent_id 移动（null 移到根目录），可同时提交。",
)
async def update_document_folder(
    knowledge_base_id: Annotated[UUID, ApiPath(description="目标知识库 ID。")],
    folder_id: Annotated[UUID, ApiPath(description="目录 ID。")],
    payload: DocumentFolderPatch,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> DocumentFolderRead:
    values = payload.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=422, detail="至少提供一个要修改的字段")
    try:
        folder = await folder_service.update_folder(
            session,
            knowledge_base_id=knowledge_base_id,
            folder_id=folder_id,
            name=values.get("name"),
            parent_id=values.get("parent_id"),
            move="parent_id" in values,
        )
    except FolderNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="目录不存在或不属于该知识库"
        ) from exc
    except KnowledgeBaseArchived as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="知识库已归档，不能修改目录"
        ) from exc
    except FolderNameConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="同一级目录下已存在同名目录"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return DocumentFolderRead.model_validate(folder)


@router.delete(
    "/knowledge-bases/{knowledge_base_id}/folders/{folder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除知识库目录",
    description="只能删除空目录；目录下仍有子目录或文件时返回 409，需先移走或删除内容。",
)
async def delete_document_folder(
    knowledge_base_id: Annotated[UUID, ApiPath(description="目标知识库 ID。")],
    folder_id: Annotated[UUID, ApiPath(description="目录 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    try:
        await folder_service.delete_folder(
            session, knowledge_base_id=knowledge_base_id, folder_id=folder_id
        )
    except FolderNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="目录不存在或不属于该知识库"
        ) from exc
    except KnowledgeBaseArchived as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="知识库已归档，不能修改目录"
        ) from exc
    except FolderNotEmpty as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="目录非空，请先移走或删除目录下的文件和子目录",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/documents/{document_id}",
    response_model=DocumentRead,
    summary="移动文档到目录",
    description="修改文档所在目录；目录必须属于文档所在子库，null 表示移动到根目录。",
)
async def move_document(
    document_id: Annotated[UUID, ApiPath(description="逻辑文档 ID。")],
    payload: DocumentUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> DocumentRead:
    try:
        document = await folder_service.move_document(
            session, document_id=document_id, folder_id=payload.folder_id
        )
    except FolderNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="文档或目录不存在"
        ) from exc
    except KnowledgeBaseArchived as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="知识库已归档，不能移动文档"
        ) from exc
    return DocumentRead.model_validate(document)


@router.get(
    "/documents/{document_id}/versions/{document_version_id}/chunks",
    response_model=ChunkPreviewResponse,
    summary="查询文档版本的切块预览",
    description=(
        "按父块分页返回该版本的分块文本、字符数、章节路径与块类型；"
        "返回整体统计便于管理页展示切分效果。"
    ),
)
async def list_document_chunks(
    document_id: Annotated[UUID, ApiPath(description="逻辑文档 ID。")],
    document_version_id: Annotated[UUID, ApiPath(description="要预览的文档版本 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    offset: Annotated[
        int, Query(ge=0, description="跳过的父块数量。")
    ] = 0,
    limit: Annotated[
        int, Query(ge=1, le=500, description="本页最多返回的父块数量，最大为 500。")
    ] = 100,
) -> ChunkPreviewResponse:
    version = await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.id == document_version_id,
            DocumentVersion.document_id == document_id,
        )
    )
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文档或版本不存在",
        )
    total_parents = await session.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_version_id == document_version_id,
            DocumentChunk.level == "parent",
        )
    )
    total_children = await session.scalar(
        select(func.count(DocumentChunk.id)).where(
            DocumentChunk.document_version_id == document_version_id,
            DocumentChunk.level == "child",
        )
    )
    total_chars = await session.scalar(
        select(func.coalesce(func.sum(func.char_length(DocumentChunk.content)), 0)).where(
            DocumentChunk.document_version_id == document_version_id,
            DocumentChunk.level == "parent",
        )
    )
    parents = list(
        await session.scalars(
            select(DocumentChunk)
            .where(
                DocumentChunk.document_version_id == document_version_id,
                DocumentChunk.level == "parent",
            )
            .order_by(DocumentChunk.ordinal)
            .offset(offset)
            .limit(limit)
        )
    )
    parent_ids = [parent.id for parent in parents]
    children = list(
        await session.scalars(
            select(DocumentChunk)
            .where(
                DocumentChunk.document_version_id == document_version_id,
                DocumentChunk.level == "child",
            )
            .order_by(DocumentChunk.ordinal)
        )
    )
    children_by_parent: dict[UUID, list] = {}
    for chunk in children:
        if chunk.parent_chunk_id is not None and chunk.parent_chunk_id in parent_ids:
            children_by_parent.setdefault(chunk.parent_chunk_id, []).append(chunk)
    assets = list(
        await session.scalars(
            select(DocumentAsset).where(
                DocumentAsset.document_version_id == document_version_id,
                DocumentAsset.marker.is_not(None),
            )
        )
    )
    parent_items = []
    for parent in parents:
        extra = parent.extra_metadata or {}
        parent_items.append(
            ChunkPreviewParent(
                ordinal=parent.ordinal,
                content=render_images(parent.content, assets),
                char_count=len(parent.content),
                page_number=parent.page_number,
                section=extra.get("section"),
                block_type=str(extra.get("block_type") or "text"),
                children=[
                    {
                        "ordinal": chunk.ordinal,
                        "content": render_images(chunk.content, assets),
                        "char_count": len(chunk.content),
                    }
                    for chunk in children_by_parent.get(parent.id, [])
                ],
            )
        )
    return ChunkPreviewResponse(
        total_parents=int(total_parents or 0),
        total_children=int(total_children or 0),
        total_chars=int(total_chars or 0),
        offset=offset,
        limit=limit,
        parents=parent_items,
        assets=[
            ChunkPreviewAsset(
                id=asset.id,
                marker=asset.marker,
                media_type=asset.media_type,
                description=asset.description,
                page_number=asset.page_number,
                chunk_id=asset.chunk_id,
            )
            for asset in assets
        ],
    )


@router.get(
    "/documents/{document_id}/file",
    summary="下载或预览文档最新版本的原文件",
    description=(
        "返回最新上传版本的服务端原文件副本。disposition=inline 用于浏览器预览，"
        "attachment 用于下载；不支持浏览器内渲染的格式（如 Office 文档）会触发下载。"
    ),
)
async def get_document_file(
    document_id: Annotated[UUID, ApiPath(description="逻辑文档 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    disposition: Annotated[
        str,
        Query(description="inline 在浏览器内预览，attachment 作为附件下载。"),
    ] = "attachment",
) -> Response:
    if disposition not in {"inline", "attachment"}:
        raise HTTPException(status_code=422, detail="disposition 只能是 inline 或 attachment")
    document = await session.scalar(
        select(Document).where(Document.id == document_id)
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    version = await session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_number == document.latest_version_number,
        )
    )
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="最新版本的原文件不存在"
        )
    try:
        content = await get_object_storage(get_settings()).get(version.object_key)
    except Exception as exc:
        logger.exception("Failed to read document file: document_id=%s", document_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="原文件读取失败"
        ) from exc
    quoted_name = version.filename.replace("\\", "").replace('"', "")
    fallback = (
        quoted_name.encode("ascii", "replace").decode("ascii").replace("?", "_")
    )
    content_disposition = (
        f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quote(quoted_name)}"
    )
    return Response(
        content=content,
        media_type=version.media_type or "application/octet-stream",
        headers={
            "Content-Disposition": content_disposition,
            "Cache-Control": "no-store",
        },
    )
