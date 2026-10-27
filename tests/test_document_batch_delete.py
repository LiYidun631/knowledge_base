"""验证按文档 ID 批量删除接口的范围与响应边界。"""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.api.v1 import documents as document_api
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    Document,
    KnowledgeBase,
    ModelProfile,
    VectorLibrary,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


@asynccontextmanager
async def _fixture(tmp_path):
    """构造一个公司、大库、两个子库，以及子库 A 下的两个文档和子库 B 下的一个文档。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'batch-delete.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            embedding = ModelProfile(
                name="批量删除-embedding",
                kind="embedding",
                provider="openai_compatible",
                model_name="text-embedding-3-small",
                dimensions=1536,
                is_default=True,
            )
            company = Company(code="batch-delete-company", name="批量删除公司")
            session.add_all([embedding, company])
            await session.flush()
            library = VectorLibrary(
                company_id=company.id,
                name="批量删除大库",
                embedding_model_id=embedding.id,
            )
            session.add(library)
            await session.flush()
            kb = KnowledgeBase(
                company_id=company.id,
                vector_library_id=library.id,
                name="批量删除子库",
                embedding_model_id=embedding.id,
            )
            other_kb = KnowledgeBase(
                company_id=company.id,
                vector_library_id=library.id,
                name="另一子库",
                embedding_model_id=embedding.id,
            )
            session.add_all([kb, other_kb])
            await session.flush()
            documents = [
                Document(
                    company_id=company.id,
                    knowledge_base_id=kb.id,
                    document_key=f"file-{index}",
                    filename=f"文件{index}.txt",
                    status="completed",
                )
                for index in range(2)
            ]
            other_document = Document(
                company_id=company.id,
                knowledge_base_id=other_kb.id,
                document_key="other-file",
                filename="他库文件.txt",
                status="completed",
            )
            session.add_all([*documents, other_document])
            await session.commit()
            yield factory, kb.id, [document.id for document in documents], other_document.id
    finally:
        await engine.dispose()


def _build_client(app, factory) -> httpx.AsyncClient:
    """组装 ASGI 测试客户端；db 会话在请求结束时提交。"""

    async def db_session():
        async with factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db_session] = db_session
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers={"Authorization": "Bearer admin-test-token"},
    )


def _make_app(monkeypatch):
    settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_batch_delete_documents_ok(monkeypatch, tmp_path) -> None:
    """合法请求按提交顺序去重后删除；空列表被请求校验拒绝。"""

    async def scenario() -> None:
        async with _fixture(tmp_path) as (factory, kb_id, document_ids, _other_id):
            app = _make_app(monkeypatch)
            deleted_calls: list[list] = []

            async def fake_delete_documents(_session, ids):
                deleted_calls.append(list(ids))
                return [str(document_id) for document_id in ids]

            monkeypatch.setattr(document_api, "delete_documents", fake_delete_documents)
            async with _build_client(app, factory) as client:
                response = await client.post(
                    f"knowledge-bases/{kb_id}/documents/batch-delete",
                    json={
                        "document_ids": [
                            str(document_ids[0]),
                            str(document_ids[1]),
                            str(document_ids[0]),
                        ]
                    },
                )
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["deleted_count"] == 2
                assert body["deleted_ids"] == [str(document_ids[0]), str(document_ids[1])]
                assert deleted_calls == [[document_ids[0], document_ids[1]]]

                response = await client.post(
                    f"knowledge-bases/{kb_id}/documents/batch-delete",
                    json={"document_ids": []},
                )
                assert response.status_code == 422
                assert deleted_calls == [[document_ids[0], document_ids[1]]]

    asyncio.run(scenario())


def test_batch_delete_rejects_other_kb_documents(monkeypatch, tmp_path) -> None:
    """跨子库或不存在的文档 ID 返回 404 且不触发任何删除。"""

    async def scenario() -> None:
        async with _fixture(tmp_path) as (factory, kb_id, document_ids, other_id):
            app = _make_app(monkeypatch)

            async def fake_delete_documents(_session, ids):
                raise AssertionError("跨库请求不应触发删除")

            monkeypatch.setattr(document_api, "delete_documents", fake_delete_documents)
            async with _build_client(app, factory) as client:
                response = await client.post(
                    f"knowledge-bases/{kb_id}/documents/batch-delete",
                    json={"document_ids": [str(document_ids[0]), str(other_id)]},
                )
                assert response.status_code == 404
                detail = response.json()["detail"]
                assert str(other_id) in detail["document_ids"]
                assert str(document_ids[0]) not in detail["document_ids"]

                unknown_id = uuid4()
                response = await client.post(
                    f"knowledge-bases/{kb_id}/documents/batch-delete",
                    json={"document_ids": [str(unknown_id)]},
                )
                assert response.status_code == 404
                assert str(unknown_id) in response.json()["detail"]["document_ids"]

                async with factory() as session:
                    remaining = await session.scalar(select(func.count(Document.id)))
                assert remaining == 3

    asyncio.run(scenario())
