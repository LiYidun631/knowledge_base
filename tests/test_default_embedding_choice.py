import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.api.v1.schemas import KnowledgeBaseCreate, VectorLibraryCreate
from knowledge_service.application import companies as company_service
from knowledge_service.application.knowledge_bases import service as knowledge_base_service
from knowledge_service.cli import provision_catalog as catalog_cli
from knowledge_service.cli.provision_catalog import CatalogDefinition, ModelDefinition
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    KnowledgeBase,
    ModelProfile,
)


def test_default_catalog_uses_dashscope_embedding_and_rerank(monkeypatch) -> None:
    """默认目录固定使用百炼 Embedding 和 Rerank 预设。"""
    settings = SimpleNamespace(
        default_embedding_profile_name="aliyun-dashscope-embedding-v4",
        default_rerank_profile_name="aliyun-dashscope-qwen3.7-text-rerank",
        default_rerank_provider="dashscope",
        default_rerank_model_name="qwen3.7-text-rerank",
        default_rerank_base_url="https://dashscope.aliyuncs.com/compatible-api/v1",
        default_rerank_path="/reranks",
        default_rerank_secret_ref="dashscope",
        embedding_model_presets=[
            {
                "name": "aliyun-dashscope-embedding-v4",
                "secret_ref": "dashscope",
                "provider": "dashscope",
                "base_url": "https://dashscope.aliyuncs.com/compatible-api/v1",
                "model_name": "text-embedding-v4",
                "dimensions": 1024,
                "embedding_path": "/embeddings",
                "batch_size": 10,
                "enabled": False,
            }
        ],
    )
    monkeypatch.setattr(catalog_cli, "get_settings", lambda: settings)

    profile = catalog_cli.default_catalog().models[0]

    assert (profile.name, profile.provider, profile.model_name, profile.dimensions) == (
        "aliyun-dashscope-embedding-v4",
        "dashscope",
        "text-embedding-v4",
        1024,
    )
    assert profile.secret_ref == "dashscope"
    assert profile.is_default is True
    assert catalog_cli.default_catalog().models[1].is_default is True


def test_provision_catalog_switches_default_without_mutating_profiles(monkeypatch) -> None:
    """切换默认档案时保留旧档案及其设置，已有知识库仍可继续使用。"""

    async def run() -> list[ModelProfile]:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(ModelProfile.__table__.create)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(catalog_cli, "get_session_factory", lambda: session_factory)

        previous_embedding = ModelDefinition(
            name="previous-embedding",
            kind="embedding",
            provider="openai_compatible",
            model_name="previous-embedding-model",
            dimensions=1024,
            settings={"base_url": "https://previous.example/v1"},
            secret_ref="previous_embedding_key",
            is_default=True,
        )
        dashscope = ModelDefinition(
            name="aliyun-dashscope-embedding-v4",
            kind="embedding",
            provider="dashscope",
            model_name="text-embedding-v4",
            dimensions=1024,
            settings={
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "embedding_path": "/embeddings",
                "batch_size": 10,
            },
            secret_ref="dashscope",
            is_default=True,
        )
        old_rerank = ModelDefinition(
            name="old-rerank",
            kind="rerank",
            provider="openai_compatible",
            model_name="old-rerank-model",
            settings={"base_url": "https://old.example/v1"},
            is_default=True,
        )
        dashscope_rerank = ModelDefinition(
            name="aliyun-dashscope-qwen3.7-text-rerank",
            kind="rerank",
            provider="dashscope",
            model_name="qwen3.7-text-rerank",
            settings={
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "rerank_path": "/reranks",
            },
            secret_ref="dashscope",
            is_default=True,
        )
        await catalog_cli.provision_catalog(
            CatalogDefinition(models=[previous_embedding, old_rerank])
        )
        await catalog_cli.provision_catalog(CatalogDefinition(models=[dashscope, dashscope_rerank]))

        async with session_factory() as session:
            profiles = list(
                await session.scalars(
                    select(ModelProfile).order_by(ModelProfile.kind, ModelProfile.name)
                )
            )
        await engine.dispose()
        return profiles

    profiles = asyncio.run(run())

    assert [(profile.name, profile.is_default) for profile in profiles] == [
        ("aliyun-dashscope-embedding-v4", True),
        ("previous-embedding", False),
        ("aliyun-dashscope-qwen3.7-text-rerank", True),
        ("old-rerank", False),
    ]
    assert next(
        profile for profile in profiles if profile.name == "previous-embedding"
    ).model_name == ("previous-embedding-model")


def test_knowledge_base_uses_default_rerank_only_when_field_is_omitted(tmp_path) -> None:
    """省略 Rerank 时绑定默认档案，显式 null 时关闭 Rerank。"""

    async def run() -> tuple[KnowledgeBase, KnowledgeBase]:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'defaults.db'}")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session, session.begin():
            embedding = ModelProfile(
                name="dashscope-embedding",
                kind="embedding",
                provider="dashscope",
                model_name="text-embedding-v4",
                dimensions=1024,
                is_default=True,
            )
            reranker = ModelProfile(
                name="dashscope-rerank",
                kind="rerank",
                provider="dashscope",
                model_name="qwen3.7-text-rerank",
                is_default=True,
            )
            session.add_all([embedding, reranker])
            await session.flush()
            automatically_bound = await knowledge_base_service.create_knowledge_base(
                session,
                name="默认 Rerank",
                description=None,
                embedding_model_id=None,
            )
            explicitly_disabled = await knowledge_base_service.create_knowledge_base(
                session,
                name="关闭 Rerank",
                description=None,
                embedding_model_id=None,
                rerank_model_id=None,
            )
            assert automatically_bound.rerank_model_id == reranker.id
            assert explicitly_disabled.rerank_model_id is None
        await engine.dispose()
        return automatically_bound, explicitly_disabled

    automatically_bound, explicitly_disabled = asyncio.run(run())
    assert automatically_bound.rerank_model_id is not None
    assert explicitly_disabled.rerank_model_id is None


def test_library_create_schema_allows_model_choice_but_legacy_create_does_not() -> None:
    """新大库支持创建时选定模型；旧兼容知识库请求仍不接受模型 ID。"""
    embedding_id, rerank_id = uuid4(), uuid4()
    assert VectorLibraryCreate(
        name="公司大库", embedding_model_id=embedding_id, rerank_model_id=rerank_id
    ).model_dump() == {
        "name": "公司大库",
        "description": None,
        "embedding_model_id": embedding_id,
        "rerank_model_id": rerank_id,
    }
    assert KnowledgeBaseCreate(name="旧版兼容库").model_dump() == {
        "name": "旧版兼容库",
        "description": None,
    }
    with pytest.raises(ValidationError):
        KnowledgeBaseCreate(name="旧版兼容库", rerank_model_id=None)
    with pytest.raises(ValidationError):
        KnowledgeBaseCreate(name="旧版兼容库", embedding_model_id=uuid4())


def test_company_library_binds_models_from_developer_defaults(tmp_path) -> None:
    """创建公司大库时自动绑定已由开发配置登记的默认 Embedding 和 Rerank。"""

    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'company-library.db'}")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with session_factory() as session, session.begin():
                company = Company(code="company-defaults", name="默认模型公司")
                embedding = ModelProfile(
                    name="configured-embedding",
                    kind="embedding",
                    provider="dashscope",
                    model_name="text-embedding-v4",
                    dimensions=1024,
                    is_default=True,
                )
                reranker = ModelProfile(
                    name="configured-rerank",
                    kind="rerank",
                    provider="dashscope",
                    model_name="qwen3.7-text-rerank",
                    is_default=True,
                )
                session.add_all([company, embedding, reranker])
                await session.flush()
                library = await company_service.create_vector_library(
                    session,
                    company_id=company.id,
                    name="自动模型大库",
                    description=None,
                )
                assert library.embedding_model_id == embedding.id
                assert library.rerank_model_id == reranker.id
        finally:
            await engine.dispose()

    asyncio.run(run())
