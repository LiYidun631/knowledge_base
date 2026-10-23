"""知识库内目录的组织操作：多级目录与文件归属，不参与检索。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.domain.errors import (
    FolderNameConflict,
    FolderNotEmpty,
    FolderNotFound,
    KnowledgeBaseArchived,
    KnowledgeBaseNotFound,
)
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentFolder,
    KnowledgeBase,
)


async def _ensure_writable(session: AsyncSession, *, knowledge_base_id: UUID) -> None:
    """归档库保留历史数据但整体只读，避免归档后仍被改动目录结构。"""
    status = await session.scalar(
        select(KnowledgeBase.status).where(KnowledgeBase.id == knowledge_base_id)
    )
    if status is None:
        raise KnowledgeBaseNotFound
    if status != "active":
        raise KnowledgeBaseArchived


async def _get_folder(session: AsyncSession, *, folder_id: UUID) -> DocumentFolder:
    folder = await session.get(DocumentFolder, folder_id)
    if folder is None:
        raise FolderNotFound
    return folder


async def _ensure_parent(
    session: AsyncSession, *, knowledge_base_id: UUID, parent_id: UUID | None
) -> None:
    if parent_id is None:
        return
    parent = await _get_folder(session, folder_id=parent_id)
    if parent.knowledge_base_id != knowledge_base_id:
        raise FolderNotFound


async def _ensure_name_available(
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    parent_id: UUID | None,
    name: str,
    exclude_id: UUID | None = None,
) -> None:
    filters = [
        DocumentFolder.knowledge_base_id == knowledge_base_id,
        DocumentFolder.parent_id == parent_id,
        DocumentFolder.name == name,
    ]
    if exclude_id is not None:
        filters.append(DocumentFolder.id != exclude_id)
    existing = await session.scalar(select(DocumentFolder.id).where(*filters))
    if existing is not None:
        raise FolderNameConflict


async def list_folders(session: AsyncSession, *, knowledge_base_id: UUID) -> list[DocumentFolder]:
    """列出知识库全部目录，按名称排序；前端自行组装成树。"""
    return list(
        await session.scalars(
            select(DocumentFolder)
            .where(DocumentFolder.knowledge_base_id == knowledge_base_id)
            .order_by(DocumentFolder.name)
        )
    )


async def create_folder(
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    name: str,
    parent_id: UUID | None = None,
) -> DocumentFolder:
    """在指定父目录下新建目录；同级不允许重名。"""
    await _ensure_writable(session, knowledge_base_id=knowledge_base_id)
    await _ensure_parent(session, knowledge_base_id=knowledge_base_id, parent_id=parent_id)
    await _ensure_name_available(
        session, knowledge_base_id=knowledge_base_id, parent_id=parent_id, name=name
    )
    folder = DocumentFolder(knowledge_base_id=knowledge_base_id, parent_id=parent_id, name=name)
    session.add(folder)
    await session.flush()
    return folder


async def _is_descendant(session: AsyncSession, *, folder_id: UUID, ancestor_id: UUID) -> bool:
    """沿父链向上检查 folder_id 是否位于 ancestor_id 之下。"""
    current_id: UUID | None = folder_id
    seen: set[UUID] = set()
    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        current = await _get_folder(session, folder_id=current_id)
        if current.id == ancestor_id:
            return True
        current_id = current.parent_id
    return False


async def update_folder(
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    folder_id: UUID,
    name: str | None = None,
    parent_id: UUID | None = None,
    move: bool = False,
) -> DocumentFolder:
    """重命名或移动目录；移动时拒绝放入自身或自己的子目录。"""
    folder = await _get_folder(session, folder_id=folder_id)
    if folder.knowledge_base_id != knowledge_base_id:
        raise FolderNotFound
    await _ensure_writable(session, knowledge_base_id=knowledge_base_id)
    if name is not None and name != folder.name:
        await _ensure_name_available(
            session,
            knowledge_base_id=knowledge_base_id,
            parent_id=parent_id if move else folder.parent_id,
            name=name,
            exclude_id=folder_id,
        )
        folder.name = name
    if move:
        if parent_id == folder.id or (
            parent_id is not None
            and await _is_descendant(session, folder_id=parent_id, ancestor_id=folder.id)
        ):
            raise ValueError("不能把目录移动到自身或自己的子目录下")
        await _ensure_parent(session, knowledge_base_id=knowledge_base_id, parent_id=parent_id)
        if name is None:
            # 目标位置重名时同样拒绝，避免无声地破坏同级唯一性。
            await _ensure_name_available(
                session,
                knowledge_base_id=knowledge_base_id,
                parent_id=parent_id,
                name=folder.name,
                exclude_id=folder_id,
            )
        folder.parent_id = parent_id
    await session.flush()
    return folder


async def delete_folder(
    session: AsyncSession, *, knowledge_base_id: UUID, folder_id: UUID
) -> None:
    """删除空目录；目录下有子目录或文件时拒绝删除。"""
    folder = await _get_folder(session, folder_id=folder_id)
    if folder.knowledge_base_id != knowledge_base_id:
        raise FolderNotFound
    await _ensure_writable(session, knowledge_base_id=knowledge_base_id)
    child = await session.scalar(
        select(DocumentFolder.id).where(DocumentFolder.parent_id == folder_id)
    )
    document = await session.scalar(
        select(Document.id).where(Document.folder_id == folder_id)
    )
    if child is not None or document is not None:
        raise FolderNotEmpty
    await session.delete(folder)
    await session.flush()


async def move_document(
    session: AsyncSession, *, document_id: UUID, folder_id: UUID | None
) -> Document:
    """把文档移动到指定目录；目录必须属于文档所在子库。"""
    document = await session.get(Document, document_id)
    if document is None:
        raise FolderNotFound
    await _ensure_writable(session, knowledge_base_id=document.knowledge_base_id)
    if folder_id is not None:
        folder = await _get_folder(session, folder_id=folder_id)
        if folder.knowledge_base_id != document.knowledge_base_id:
            raise FolderNotFound
    document.folder_id = folder_id
    await session.flush()
    return document
