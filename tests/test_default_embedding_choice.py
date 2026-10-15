import asyncio
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.api.v1.schemas import KnowledgeBaseCreate
from knowledge_service.application.knowledge_bases import service as knowledge_base_service
from knowledge_service.cli import provision_catalog as catalog_cli
from knowledge_service.cli.provision_catalog import CatalogDefinition, ModelDefinition
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
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


def test_knowledge_base_create_schema_preserves_omitted_vs_null() -> None:
    """请求模型保留 Rerank 字段是否由调用方显式提供。"""
    omitted = KnowledgeBaseCreate(name="默认模型")
    disabled = KnowledgeBaseCreate(name="关闭模型", rerank_model_id=None)

    assert "rerank_model_id" not in omitted.model_dump(exclude_unset=True)
    assert disabled.model_dump(exclude_unset=True)["rerank_model_id"] is None
