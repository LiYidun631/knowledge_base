"""入库后修改文档产品标签：覆盖写入、目录校验、归档只读与检索范围立即生效。"""

import asyncio
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.application.search import service as search_service
from knowledge_service.application.search.bm25 import BM25Hit
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    KnowledgeBase,
    ModelProfile,
    ProductCatalog,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def _app(monkeypatch, settings):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


async def _build_library(session, *, with_catalog: bool = True):
    """两篇已入库文档：同产品不同型号，用于验证改标签后检索范围的变化。"""
    embedding = ModelProfile(
        name="labels-embedding",
        kind="embedding",
        provider="openai_compatible",
        model_name="labels-embedding",
        dimensions=2,
        is_enabled=True,
    )
    session.add(embedding)
    await session.flush()
    kb = KnowledgeBase(
        name="标签修改测试库",
        embedding_model_id=embedding.id,
        models_locked_at=utc_now(),
    )
    session.add(kb)
    await session.flush()
    if with_catalog:
        session.add_all(
            [
                ProductCatalog(product_name="体温计", model_name="YK-IRT1"),
                ProductCatalog(product_name="体温计", model_name="YK-IRT2"),
                ProductCatalog(product_name="体温计", model_name="YK-IRT3"),
            ]
        )
    documents: dict[str, UUID] = {}
    child_chunks: dict[str, UUID] = {}
    for key, model in (("irt1", "YK-IRT1"), ("irt2", "YK-IRT2")):
        document = Document(
            knowledge_base_id=kb.id,
            document_key=key,
            filename=f"{key}.pdf",
            latest_version_number=1,
            status="completed",
            extra_metadata={
                "source_file_id": key,
                "product_name": "体温计",
                "model_name": model,
                "product_names": ["体温计"],
                "model_names": [model],
            },
        )
        session.add(document)
        await session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            filename=document.filename,
            size_bytes=1,
            content_sha256="a" * 64,
            object_key=f"{key}.pdf",
            status="completed",
            is_current=True,
        )
        session.add(version)
        await session.flush()
        parent = DocumentChunk(
            document_id=document.id,
            document_version_id=version.id,
            ordinal=1,
            level="parent",
            content=f"{key} 说明书内容",
            content_sha256="b" * 64,
            extra_metadata={"section": "使用说明"},
        )
        session.add(parent)
        await session.flush()
        child = DocumentChunk(
            document_id=document.id,
            document_version_id=version.id,
            parent_chunk_id=parent.id,
            ordinal=2,
            level="child",
            content=f"{key} 使用方法",
            content_sha256="c" * 64,
            token_count=3,
        )
        session.add(child)
        await session.flush()
        documents[key] = document.id
        child_chunks[key] = child.id
    await session.commit()
    return kb.id, documents, child_chunks


def test_update_product_labels_overwrites_and_affects_search(tmp_path, monkeypatch) -> None:
    """PATCH 覆盖标签后立即改变检索过滤结果，且不需要重建向量。"""

    settings = Settings(_env_file=None, auth_admin_token="admin-test-token")

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'patch.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                kb_id, documents, child_chunks = await _build_library(session)

                async def db_session():
                    yield session

                app = _app(monkeypatch, settings)
                app.dependency_overrides[get_db_session] = db_session
                admin = {"Authorization": "Bearer admin-test-token"}

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.patch(
                        f"/api/v1/documents/{documents['irt1']}/product-labels",
                        headers=admin,
                        json={"product_names": ["体温计"], "model_names": ["YK-IRT3"]},
                    )
                    assert response.status_code == 200, response.text
                    body = response.json()
                    assert body["product_names"] == ["体温计"]
                    assert body["model_names"] == ["YK-IRT3"]

                    # 旧版单值键必须清掉，否则读取时会与列表合并回旧标签。
                    stored = (
                        await session.execute(
                            select(Document.extra_metadata).where(
                                Document.id == documents["irt1"]
                            )
                        )
                    ).scalar_one()
                    assert "product_name" not in stored
                    assert "model_name" not in stored
                    assert stored["product_names"] == ["体温计"]
                    assert stored["model_names"] == ["YK-IRT3"]
                    assert stored["source_file_id"] == "irt1"

                    # 列表接口同步返回新标签。
                    response = await client.get(
                        f"/api/v1/knowledge-bases/{kb_id}/documents", headers=admin
                    )
                    rows = {row["document_key"]: row for row in response.json()}
                    assert rows["irt1"]["model_names"] == ["YK-IRT3"]
                    assert rows["irt2"]["model_names"] == ["YK-IRT2"]

                    # 检索侧：按新型号可召回，按旧型号不再召回，全程没有重新入库。
                    # 提交后才能被搜索用的新会话读到，顺带验证标签真的落库。
                    await session.commit()

                    async def lexical(
                        _session,
                        *,
                        knowledge_base_id,
                        company_id,
                        document_version_ids,
                        document_ids,
                        query,
                        limit,
                    ):
                        # 真实 BM25 会按 document_ids 下推过滤，这里同样只返回允许的文档。
                        allowed = set(document_ids) if document_ids else set(documents.values())
                        return [
                            BM25Hit(child_chunks[key], float(index + 1))
                            for index, key in enumerate(("irt1", "irt2"))
                            if documents[key] in allowed
                        ]

                    monkeypatch.setattr(search_service, "get_settings", lambda: Settings())
                    monkeypatch.setattr(search_service, "search_bm25", lexical)

                    async def search(**kwargs):
                        async with factory() as search_session:
                            return await search_service.search_knowledge_base(
                                search_session,
                                knowledge_base_id=kb_id,
                                document_ids=None,
                                query="使用说明",
                                top_k=10,
                                retrieval_mode="bm25",
                                **kwargs,
                            )

                    results = await search(model_name="YK-IRT3")
                    assert [item.filename for item in results] == ["irt1.pdf"]
                    assert results[0].model_names == ["YK-IRT3"]

                    # 改标签后库里已没有 YK-IRT1 文档，旧型号直接零命中。
                    results = await search(model_name="YK-IRT1")
                    assert results == []

                    results = await search(model_name="YK-IRT2")
                    assert [item.filename for item in results] == ["irt2.pdf"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_update_product_labels_guards_catalog_archive_and_missing(tmp_path, monkeypatch) -> None:
    """未登记标签 422、归档库 409、文档不存在 404，且失败时标签保持原样。"""

    settings = Settings(_env_file=None, auth_admin_token="admin-test-token")

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                kb_id, documents, _ = await _build_library(session)

                async def db_session():
                    yield session

                app = _app(monkeypatch, settings)
                app.dependency_overrides[get_db_session] = db_session
                admin = {"Authorization": "Bearer admin-test-token"}
                path = f"/api/v1/documents/{documents['irt1']}/product-labels"

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.patch(
                        path, headers=admin, json={"model_names": ["不存在的型号"]}
                    )
                    assert response.status_code == 422, response.text
                    assert "产品目录" in response.json()["detail"]

                    response = await client.patch(
                        path, headers=admin, json={"model_names": ["YK-IRT3"]}
                    )
                    assert response.status_code == 200, response.text

                    # 归档后整体只读，标签不能再改。
                    knowledge_base = await session.get(KnowledgeBase, kb_id)
                    knowledge_base.status = "archived"
                    await session.commit()
                    response = await client.patch(
                        path, headers=admin, json={"model_names": ["YK-IRT2"]}
                    )
                    assert response.status_code == 409, response.text

                    knowledge_base.status = "active"
                    await session.commit()
                    response = await client.patch(
                        f"/api/v1/documents/{UUID(int=1)}/product-labels",
                        headers=admin,
                        json={"model_names": ["YK-IRT2"]},
                    )
                    assert response.status_code == 404, response.text

                    stored = (
                        await session.execute(
                            select(Document.extra_metadata).where(
                                Document.id == documents["irt1"]
                            )
                        )
                    ).scalar_one()
                    assert stored["model_names"] == ["YK-IRT3"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_batch_update_product_labels_updates_all_or_nothing(tmp_path, monkeypatch) -> None:
    """批量改标签一次覆盖多篇；含不存在的文档时整批失败且不落库。"""

    settings = Settings(_env_file=None, auth_admin_token="admin-test-token")

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'batch.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                _, documents, _ = await _build_library(session)

                async def db_session():
                    yield session

                app = _app(monkeypatch, settings)
                app.dependency_overrides[get_db_session] = db_session
                admin = {"Authorization": "Bearer admin-test-token"}

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/api/v1/documents/product-labels/batch",
                        headers=admin,
                        json={
                            "document_ids": [str(documents["irt1"]), str(documents["irt2"])],
                            "product_names": ["体温计"],
                            "model_names": ["YK-IRT3"],
                        },
                    )
                    assert response.status_code == 200, response.text
                    assert set(response.json()["updated_ids"]) == {
                        str(documents["irt1"]),
                        str(documents["irt2"]),
                    }

                    response = await client.post(
                        "/api/v1/documents/product-labels/batch",
                        headers=admin,
                        json={
                            "document_ids": [str(documents["irt1"]), str(UUID(int=2))],
                            "model_names": ["YK-IRT1"],
                        },
                    )
                    assert response.status_code == 404, response.text

                    # 失败批次不产生部分写入。
                    stored = (
                        await session.execute(
                            select(Document.extra_metadata).order_by(Document.document_key)
                        )
                    ).scalars().all()
                    assert [item["model_names"] for item in stored] == [
                        ["YK-IRT3"],
                        ["YK-IRT3"],
                    ]
        finally:
            await engine.dispose()

    asyncio.run(scenario())
