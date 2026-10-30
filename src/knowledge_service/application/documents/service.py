from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.application.knowledge_bases.service import get_knowledge_base
from knowledge_service.domain.errors import (
    DocumentNotFound,
    KnowledgeBaseArchived,
    KnowledgeBaseNotFound,
)
from knowledge_service.domain.policies import lock_model_binding
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentFolder,
    DocumentVersion,
    IngestionJob,
    KnowledgeBase,
    OutboxEvent,
)


async def register_document(
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    company_id: UUID | None = None,
    document_key: str | None = None,
    filename: str,
    media_type: str | None,
    size_bytes: int,
    content_sha256: str,
    object_key: str,
    source_metadata: dict[str, Any] | None = None,
    folder_id: UUID | None = None,
) -> Document:
    """登记已保存的原始文件，在同一事务内创建文档、任务与 Outbox 事件。"""
    knowledge_base = await get_knowledge_base(
        session,
        knowledge_base_id=knowledge_base_id,
        lock_row=True,
    )
    # 公司 ID 由调用方显式传入时必须与子库归属一致，防止跨公司写入。
    if company_id is not None and knowledge_base.company_id != company_id:
        raise ValueError("知识库不属于指定公司")
    # 目录必须属于同一子库；目录只是组织信息，不影响入库流程。
    if folder_id is not None:
        folder = await session.get(DocumentFolder, folder_id)
        if folder is None or folder.knowledge_base_id != knowledge_base.id:
            raise ValueError("目录不属于该知识库")
    if knowledge_base.models_locked_at is None:
        knowledge_base.models_locked_at = lock_model_binding()

    # 新公共接口使用后台文件 ID 作为逻辑键；旧调用仍兼容用文件名作为后备键。
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
    normalized_metadata = {
        str(key): value
        for key, value in (source_metadata or {}).items()
        if value is not None
    }
    normalized_metadata.setdefault("source_file_id", key)
    if knowledge_base.company_id is not None:
        normalized_metadata.setdefault("company_id", str(knowledge_base.company_id))
    if document is None:
        document = Document(
            company_id=knowledge_base.company_id,
            knowledge_base_id=knowledge_base.id,
            folder_id=folder_id,
            document_key=key,
            filename=filename,
            latest_version_number=0,
            extra_metadata=normalized_metadata,
        )
        session.add(document)
        await session.flush()
    elif document.status == "deleting":
        # 删除过程中不允许新增文档版本，避免清理完成后又留下孤儿文件和向量。
        raise ValueError("该文档正在删除，请稍后再上传")
    elif document.company_id is None and knowledge_base.company_id is not None:
        # 历史文档首次更新时补齐公司归属，兼容迁移前创建的记录。
        document.company_id = knowledge_base.company_id
    # 新版本跟随本次上传的目录选择；不传目录时保持原有归属。
    if folder_id is not None:
        document.folder_id = folder_id
    document.extra_metadata = {
        **(document.extra_metadata or {}),
        **normalized_metadata,
    }

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
                "company_id": str(knowledge_base.company_id)
                if knowledge_base.company_id is not None
                else None,
                "vector_library_id": str(knowledge_base.vector_library_id)
                if knowledge_base.vector_library_id is not None
                else None,
                "document_id": str(document.id),
                "document_version_id": str(version.id),
                "job_id": str(job.id),
                "source_file_id": key,
            },
        )
    )
    await session.flush()
    return document


async def update_product_labels(
    session: AsyncSession,
    *,
    document_ids: list[UUID],
    product_names: list[str],
    model_names: list[str],
) -> list[Document]:
    """覆盖文档的产品名称与型号标签；空列表表示清空，按通用资料参与检索。

    标签只存在 documents.extra_metadata，检索过滤、型号澄清与 FAQ 匹配都在
    查询期实时读取，因此改标签不需要重新解析或重建向量。
    """
    documents = list(
        (
            await session.scalars(
                select(Document).where(Document.id.in_(document_ids)).with_for_update()
            )
        ).all()
    )
    found_ids = {document.id for document in documents}
    if any(document_id not in found_ids for document_id in document_ids):
        raise DocumentNotFound

    knowledge_base_ids = {document.knowledge_base_id for document in documents}
    status_rows = (
        await session.execute(
            select(KnowledgeBase.id, KnowledgeBase.status).where(
                KnowledgeBase.id.in_(knowledge_base_ids)
            )
        )
    ).all()
    statuses = {knowledge_base_id: status for knowledge_base_id, status in status_rows}
    if len(statuses) != len(knowledge_base_ids):
        raise KnowledgeBaseNotFound
    # 归档库整体只读，标签同样不能改。
    if any(status != "active" for status in statuses.values()):
        raise KnowledgeBaseArchived

    for document in documents:
        metadata = dict(document.extra_metadata or {})
        # 读取时列表与旧版单值键会被合并，不删掉旧键等于旧标签仍然生效。
        metadata.pop("product_name", None)
        metadata.pop("model_name", None)
        metadata["product_names"] = list(product_names)
        metadata["model_names"] = list(model_names)
        document.extra_metadata = metadata
    await session.flush()
    return documents
