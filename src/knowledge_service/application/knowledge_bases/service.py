from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.domain.errors import ConfigurationNotFound, KnowledgeBaseNotFound
from knowledge_service.domain.policies import ensure_model_binding_mutable
from knowledge_service.infrastructure.persistence.models import KnowledgeBase, ModelProfile, utc_now


class _DefaultRerank(Enum):
    """区分未填写 Rerank 和显式关闭 Rerank。"""

    USE_DEFAULT = "use_default"


_USE_DEFAULT_RERANK = _DefaultRerank.USE_DEFAULT


async def _validate_models(
    session: AsyncSession,
    *,
    embedding_model_id: UUID,
    rerank_model_id: UUID | None,
) -> None:
    """确认所选模型已登记、启用且类型正确。"""
    embedding = await session.scalar(
        select(ModelProfile).where(
            ModelProfile.id == embedding_model_id,
            ModelProfile.kind == "embedding",
            ModelProfile.is_enabled.is_(True),
        )
    )
    if embedding is None:
        raise ConfigurationNotFound("Embedding model is unavailable")

    if rerank_model_id is not None:
        reranker = await session.scalar(
            select(ModelProfile).where(
                ModelProfile.id == rerank_model_id,
                ModelProfile.kind == "rerank",
                ModelProfile.is_enabled.is_(True),
            )
        )
        if reranker is None:
            raise ConfigurationNotFound("Rerank model is unavailable")


async def create_knowledge_base(
    session: AsyncSession,
    *,
    name: str,
    description: str | None = None,
    embedding_model_id: UUID | None = None,
    rerank_model_id: UUID | None | _DefaultRerank = _USE_DEFAULT_RERANK,
) -> KnowledgeBase:
    """使用默认模型或指定模型创建知识库；创建后模型绑定即锁定。"""
    if embedding_model_id is None:
        embedding_model_id = await session.scalar(
            select(ModelProfile.id).where(
                ModelProfile.kind == "embedding",
                ModelProfile.is_enabled.is_(True),
                ModelProfile.is_default.is_(True),
            )
        )
        if embedding_model_id is None:
            raise ConfigurationNotFound("No default embedding model is configured")

    if rerank_model_id is _USE_DEFAULT_RERANK:
        rerank_model_id = await session.scalar(
            select(ModelProfile.id).where(
                ModelProfile.kind == "rerank",
                ModelProfile.is_enabled.is_(True),
                ModelProfile.is_default.is_(True),
            )
        )

    await _validate_models(
        session,
        embedding_model_id=embedding_model_id,
        rerank_model_id=rerank_model_id,
    )
    knowledge_base = KnowledgeBase(
        name=name,
        description=description,
        embedding_model_id=embedding_model_id,
        rerank_model_id=rerank_model_id,
        models_locked_at=utc_now(),
    )
    session.add(knowledge_base)
    await session.flush()
    return knowledge_base


async def list_knowledge_bases(
    session: AsyncSession,
    *,
    offset: int,
    limit: int,
) -> list[KnowledgeBase]:
    """分页列出未归档知识库。"""
    result = await session.scalars(
        select(KnowledgeBase)
        .where(KnowledgeBase.status == "active")
        .order_by(KnowledgeBase.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result)


async def get_knowledge_base(
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    lock_row: bool = False,
) -> KnowledgeBase:
    """读取一个有效知识库，可选行锁用于并发写入保护。"""
    statement = select(KnowledgeBase).where(
        KnowledgeBase.id == knowledge_base_id,
        KnowledgeBase.status == "active",
    )
    if lock_row:
        statement = statement.with_for_update()
    knowledge_base = await session.scalar(statement)
    if knowledge_base is None:
        raise KnowledgeBaseNotFound("Knowledge base not found")
    return knowledge_base


async def update_knowledge_base(
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    values: dict[str, Any],
) -> KnowledgeBase:
    """仅修改知识库名称与描述，不开放模型绑定变更。"""
    knowledge_base = await get_knowledge_base(
        session,
        knowledge_base_id=knowledge_base_id,
        lock_row=True,
    )
    # 应用内部调用同样执行不可变规则，防止绕过 HTTP 请求校验。
    ensure_model_binding_mutable(
        models_locked_at=knowledge_base.models_locked_at,
        current_values={
            "embedding_model_id": knowledge_base.embedding_model_id,
            "rerank_model_id": knowledge_base.rerank_model_id,
        },
        requested_values=values,
    )
    if set(values) - {"name", "description"}:
        raise ValueError("只允许修改知识库名称和描述")
    if "name" in values and values["name"] is None:
        raise ValueError("知识库名称不能为空")
    for field, value in values.items():
        setattr(knowledge_base, field, value)
    await session.flush()
    return knowledge_base


async def archive_knowledge_base(session: AsyncSession, *, knowledge_base_id: UUID) -> None:
    """归档知识库，保留其文档和任务历史。"""
    knowledge_base = await get_knowledge_base(
        session,
        knowledge_base_id=knowledge_base_id,
        lock_row=True,
    )
    knowledge_base.status = "archived"
    knowledge_base.archived_at = utc_now()
    await session.flush()
