import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.application import companies as company_service
from knowledge_service.application.documents.chunking import (
    ChunkParams,
    build_chunks,
    resolve_chunk_params,
)
from knowledge_service.application.documents.parsing import ParsedBlock
from knowledge_service.application.knowledge_bases import service as kb_service
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.domain.errors import ModelConfigurationLocked
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    KnowledgeBase,
    ModelProfile,
    VectorLibrary,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def test_resolve_chunk_params_merges_with_global_defaults(monkeypatch) -> None:
    assert resolve_chunk_params(parent=None, child=None, overlap=None) is None
    monkeypatch.setattr(
        "knowledge_service.application.documents.chunking.get_settings",
        lambda: Settings(
            _env_file=None,
            parent_chunk_chars=1800,
            child_chunk_chars=600,
            child_chunk_overlap_chars=80,
        ),
    )
    params = resolve_chunk_params(parent=None, child=300, overlap=None)
    assert params == ChunkParams(parent_chars=1800, child_chars=300, overlap_chars=80)
    params = resolve_chunk_params(parent=2000, child=None, overlap=120)
    assert params == ChunkParams(parent_chars=2000, child_chars=600, overlap_chars=120)
    with pytest.raises(ValueError, match="重叠"):
        resolve_chunk_params(parent=None, child=100, overlap=150)
    with pytest.raises(ValueError, match="父块"):
        resolve_chunk_params(parent=200, child=600, overlap=None)


def test_build_chunks_respects_per_kb_params() -> None:
    content = "知识" * 400
    chunks = build_chunks(
        uuid4(),
        [ParsedBlock(kind="text", content=content, page_number=1)],
        ChunkParams(parent_chars=200, child_chars=60, overlap_chars=10),
    )
    parents = [chunk for chunk in chunks if chunk.metadata["level"] == "parent"]
    children = [chunk for chunk in chunks if chunk.metadata["level"] == "child"]
    assert parents and children
    assert max(len(chunk.content) for chunk in parents) <= 200
    assert max(len(chunk.content) for chunk in children) <= 60
    # 子块应比全局默认配置切得更细。
    default_chunks = build_chunks(
        uuid4(), [ParsedBlock(kind="text", content=content, page_number=1)]
    )
    default_children = [chunk for chunk in default_chunks if chunk.metadata["level"] == "child"]
    assert len(children) > len(default_children)


def test_chunk_params_validation_matches_written_contract() -> None:
    with pytest.raises(ValueError):
        ChunkParams(parent_chars=100, child_chars=200, overlap_chars=0)
    with pytest.raises(ValueError):
        ChunkParams(parent_chars=300, child_chars=200, overlap_chars=200)


@asynccontextmanager
async def _kb_fixture(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chunks.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            embedding = ModelProfile(
                name="分块-embedding",
                kind="embedding",
                provider="openai_compatible",
                model_name="text-embedding-3-small",
                dimensions=1536,
                is_default=True,
            )
            reranker = ModelProfile(
                name="分块-rerank",
                kind="rerank",
                provider="dashscope",
                model_name="qwen3.7-text-rerank",
                is_default=True,
            )
            company = Company(code="chunk-company", name="分块公司")
            session.add_all([embedding, reranker, company])
            await session.flush()
            library = VectorLibrary(
                company_id=company.id,
                name="分块大库",
                embedding_model_id=embedding.id,
                rerank_model_id=reranker.id,
            )
            session.add(library)
            await session.flush()
            kb = KnowledgeBase(
                company_id=company.id,
                vector_library_id=library.id,
                name="分块子库",
                embedding_model_id=embedding.id,
                rerank_model_id=reranker.id,
            )
            session.add(kb)
            await session.commit()
            yield factory, company.id, kb.id
    finally:
        await engine.dispose()


def test_sub_library_creation_stores_and_validates_chunk_params(tmp_path) -> None:
    async def run() -> None:
        async with _kb_fixture(tmp_path) as (factory, company_id, _kb_id):
            async with factory() as session, session.begin():
                sub = await company_service.create_sub_library(
                    session,
                    company_id=company_id,
                    name="自定义分块子库",
                    chunk_child_chars=400,
                    chunk_child_overlap_chars=50,
                )
                assert sub.chunk_child_chars == 400
                assert sub.chunk_child_overlap_chars == 50
                assert sub.chunk_parent_chars is None
                with pytest.raises(ValueError, match="父块"):
                    await company_service.create_sub_library(
                        session,
                        company_id=company_id,
                        name="非法子库",
                        chunk_parent_chars=200,
                        chunk_child_chars=400,
                    )

    asyncio.run(run())


def test_kb_update_allows_chunk_fields_and_rejects_invalid_combos(tmp_path) -> None:
    async def run() -> None:
        async with _kb_fixture(tmp_path) as (factory, _company_id, kb_id):
            async with factory() as session, session.begin():
                updated = await kb_service.update_knowledge_base(
                    session,
                    knowledge_base_id=kb_id,
                    values={"chunk_child_chars": 300, "chunk_child_overlap_chars": 60},
                )
                assert updated.chunk_child_chars == 300
                assert updated.chunk_child_overlap_chars == 60
                with pytest.raises(ValueError, match="重叠"):
                    await kb_service.update_knowledge_base(
                        session,
                        knowledge_base_id=kb_id,
                        values={"chunk_child_chars": 100, "chunk_child_overlap_chars": 150},
                    )
                # 已锁定库的模型字段仍被不可变规则拦截。
                with pytest.raises(ModelConfigurationLocked):
                    await kb_service.update_knowledge_base(
                        session,
                        knowledge_base_id=kb_id,
                        values={"embedding_model_id": uuid4()},
                    )
                # 白名单之外的字段直接拒绝。
                with pytest.raises(ValueError, match="只允许"):
                    await kb_service.update_knowledge_base(
                        session,
                        knowledge_base_id=kb_id,
                        values={"models_locked_at": utc_now()},
                    )

    asyncio.run(run())


def test_http_kb_chunk_update_and_explicit_model_library(monkeypatch, tmp_path) -> None:
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'http.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                embedding = ModelProfile(
                    name="http-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="text-embedding-3-small",
                    dimensions=1536,
                )
                reranker = ModelProfile(
                    name="http-rerank",
                    kind="rerank",
                    provider="dashscope",
                    model_name="qwen3.7-text-rerank",
                )
                company = Company(code="http-company", name="HTTP 公司")
                session.add_all([embedding, reranker, company])
                await session.flush()
                library = VectorLibrary(
                    company_id=company.id,
                    name="HTTP 大库",
                    embedding_model_id=embedding.id,
                )
                session.add(library)
                await session.flush()
                kb = KnowledgeBase(
                    company_id=company.id,
                    vector_library_id=library.id,
                    name="HTTP 子库",
                    embedding_model_id=embedding.id,
                )
                session.add(kb)
                await session.commit()

            settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
            import main

            monkeypatch.setattr(main, "get_settings", lambda: settings)
            app = main.create_app()
            app.dependency_overrides[get_settings] = lambda: settings

            async def db_session():
                async with factory() as session:
                    yield session
                    await session.commit()

            app.dependency_overrides[get_db_session] = db_session
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test/api/v1/",
                headers={"Authorization": "Bearer admin-test-token"},
            ) as client:
                response = await client.patch(
                    f"knowledge-bases/{kb.id}",
                    json={"chunk_child_chars": 300, "chunk_child_overlap_chars": 60},
                )
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["chunk_child_chars"] == 300
                assert body["chunk_child_overlap_chars"] == 60
                assert body["chunk_parent_chars"] is None

                response = await client.patch(
                    f"knowledge-bases/{kb.id}",
                    json={"chunk_parent_chars": 200, "chunk_child_chars": 400},
                )
                assert response.status_code == 422
                assert "父块" in response.json()["detail"]

                # 新公司可以创建时显式选定模型，并继承到子库。
                response = await client.post(
                    "companies",
                    json={"name": "模型选择公司"},
                )
                new_company_id = response.json()["id"]
                response = await client.post(
                    f"companies/{new_company_id}/vector-library",
                    json={
                        "name": "选型大库",
                        "embedding_model_id": str(embedding.id),
                        "rerank_model_id": str(reranker.id),
                    },
                )
                assert response.status_code == 201, response.text
                assert response.json()["embedding_model_id"] == str(embedding.id)
                assert response.json()["rerank_model_id"] == str(reranker.id)
                response = await client.post(
                    f"companies/{new_company_id}/vector-library/knowledge-bases",
                    json={"name": "选型子库", "chunk_child_chars": 500},
                )
                assert response.status_code == 201, response.text
                body = response.json()
                assert body["embedding_model_id"] == str(embedding.id)
                assert body["chunk_child_chars"] == 500
        finally:
            await engine.dispose()

    asyncio.run(scenario())
