"""公司、大库和子库的创建用例。"""

from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.application.documents.chunking import resolve_chunk_params
from knowledge_service.application.knowledge_bases.service import _validate_models
from knowledge_service.domain.errors import ConfigurationNotFound, KnowledgeBaseNotFound
from knowledge_service.infrastructure.persistence.models import (
    Company,
    KnowledgeBase,
    ModelProfile,
    VectorLibrary,
    utc_now,
)


class _DefaultModel(Enum):
    """区分未填写模型参数和显式关闭 Rerank。"""

    USE_DEFAULT = "use_default"


_USE_DEFAULT_MODEL = _DefaultModel.USE_DEFAULT


async def create_company(session: AsyncSession, *, name: str, description: str | None) -> Company:
    """创建 active 公司租户，并生成内部唯一公司编码。"""
    company = Company(code=f"company-{uuid4().hex}", name=name, description=description)
    session.add(company)
    await session.flush()
    return company


async def list_companies(session: AsyncSession) -> list[Company]:
    """列出全部公司（含已归档），供管理端公司选择器和公司管理页使用。"""
    return list(await session.scalars(select(Company).order_by(Company.created_at.desc())))


async def get_company(session: AsyncSession, *, company_id: UUID) -> Company:
    """读取 active 公司，不允许归档公司继续创建或写入数据。"""
    company = await session.scalar(
        select(Company).where(Company.id == company_id, Company.status == "active")
    )
    if company is None:
        raise KnowledgeBaseNotFound("Company not found")
    return company


async def create_vector_library(
    session: AsyncSession,
    *,
    company_id: UUID,
    name: str,
    description: str | None = None,
    embedding_model_id: UUID | None | _DefaultModel = _USE_DEFAULT_MODEL,
    rerank_model_id: UUID | None | _DefaultModel = _USE_DEFAULT_MODEL,
) -> VectorLibrary:
    """为公司创建唯一的大库；模型在创建时选定并锁定，缺省用同类默认档案。"""
    await get_company(session, company_id=company_id)
    if embedding_model_id is _USE_DEFAULT_MODEL:
        embedding_model_id = await session.scalar(
            select(ModelProfile.id).where(
                ModelProfile.kind == "embedding",
                ModelProfile.is_enabled.is_(True),
                ModelProfile.is_default.is_(True),
            )
        )
        if embedding_model_id is None:
            raise ConfigurationNotFound("No default embedding model is configured")
    if rerank_model_id is _USE_DEFAULT_MODEL:
        rerank_model_id = await session.scalar(
            select(ModelProfile.id).where(
                ModelProfile.kind == "rerank",
                ModelProfile.is_enabled.is_(True),
                ModelProfile.is_default.is_(True),
            )
        )
        if rerank_model_id is None:
            raise ConfigurationNotFound("No default rerank model is configured")
    await _validate_models(
        session,
        embedding_model_id=embedding_model_id,
        rerank_model_id=rerank_model_id,
    )
    library = VectorLibrary(
        company_id=company_id,
        name=name,
        description=description,
        embedding_model_id=embedding_model_id,
        rerank_model_id=rerank_model_id,
        models_locked_at=utc_now(),
    )
    session.add(library)
    await session.flush()
    return library


async def get_vector_library(session: AsyncSession, *, company_id: UUID) -> VectorLibrary:
    """读取公司的 active 大库。"""
    library = await session.scalar(
        select(VectorLibrary).where(
            VectorLibrary.company_id == company_id,
            VectorLibrary.status == "active",
        )
    )
    if library is None:
        raise KnowledgeBaseNotFound("Vector library not found")
    return library


async def create_sub_library(
    session: AsyncSession,
    *,
    company_id: UUID,
    name: str,
    description: str | None = None,
    chunk_parent_chars: int | None = None,
    chunk_child_chars: int | None = None,
    chunk_child_overlap_chars: int | None = None,
) -> KnowledgeBase:
    """在公司大库下创建子库，并继承大库的模型绑定；分块参数可空跟随全局。"""
    library = await get_vector_library(session, company_id=company_id)
    # 创建时校验参数组合，避免入库阶段才发现无效配置。
    resolve_chunk_params(
        parent=chunk_parent_chars,
        child=chunk_child_chars,
        overlap=chunk_child_overlap_chars,
    )
    sub_library = KnowledgeBase(
        company_id=company_id,
        vector_library_id=library.id,
        name=name,
        description=description,
        embedding_model_id=library.embedding_model_id,
        rerank_model_id=library.rerank_model_id,
        models_locked_at=library.models_locked_at,
        chunk_parent_chars=chunk_parent_chars,
        chunk_child_chars=chunk_child_chars,
        chunk_child_overlap_chars=chunk_child_overlap_chars,
    )
    session.add(sub_library)
    await session.flush()
    return sub_library
