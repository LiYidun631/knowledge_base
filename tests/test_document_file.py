"""原文件下载/预览接口：最新版本解析、内联与附件分发、错误边界。"""

import asyncio
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.api.v1 import documents as document_api
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentVersion,
    KnowledgeBase,
    ModelProfile,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def _app(monkeypatch, settings):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


class _StubStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, content: bytes) -> None:
        self.objects[key] = content

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]


def test_document_file_serves_latest_version_original(monkeypatch, tmp_path) -> None:
    settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
    storage = _StubStorage()
    monkeypatch.setattr(document_api, "get_object_storage", lambda settings: storage)

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'file.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                profile = ModelProfile(
                    name="file-embedding", kind="embedding",
                    provider="openai_compatible", model_name="test-embedding", dimensions=2,
                )
                session.add(profile)
                await session.flush()
                knowledge_base = KnowledgeBase(
                    name="文件下载知识库", embedding_model_id=profile.id
                )
                session.add(knowledge_base)
                await session.flush()
                document = Document(
                    knowledge_base_id=knowledge_base.id,
                    document_key="file-1", filename="说明书 测试.pdf",
                    latest_version_number=2, status="completed",
                )
                session.add(document)
                await session.flush()
                first_version = DocumentVersion(
                    document_id=document.id, version_number=1, filename="说明书 测试.pdf",
                    size_bytes=5, content_sha256="x" * 64, object_key="k/v1.pdf",
                    status="completed", is_current=False,
                )
                latest_version = DocumentVersion(
                    document_id=document.id, version_number=2, filename="说明书 测试.pdf",
                    size_bytes=7, content_sha256="y" * 64, object_key="k/v2.pdf",
                    media_type="application/pdf",
                    status="completed", is_current=True,
                )
                session.add_all([first_version, latest_version])
                await session.flush()
                await storage.put("k/v1.pdf", b"first")
                await storage.put("k/v2.pdf", b"second")
                await session.commit()
                document_id = document.id

                async def db_session():
                    yield session

                app = _app(monkeypatch, settings)
                app.dependency_overrides[get_db_session] = db_session
                admin = {"Authorization": "Bearer admin-test-token"}
                path = f"/api/v1/documents/{document_id}/file"
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test",
                ) as client:
                    # 默认按附件下载最新版本（v2）的原文件。
                    response = await client.get(path, headers=admin)
                    assert response.status_code == 200
                    assert response.content == b"second"
                    assert response.headers["content-type"] == "application/pdf"
                    disposition = response.headers["content-disposition"]
                    assert disposition.startswith("attachment;")
                    assert "filename*=UTF-8''" in disposition

                    # inline 用于浏览器内预览。
                    response = await client.get(
                        path, headers=admin, params={"disposition": "inline"}
                    )
                    assert response.status_code == 200
                    assert response.headers["content-disposition"].startswith("inline;")

                    # 非法 disposition 422；文档不存在 404。
                    response = await client.get(
                        path, headers=admin, params={"disposition": "weird"}
                    )
                    assert response.status_code == 422
                    response = await client.get(
                        f"/api/v1/documents/{uuid4()}/file", headers=admin
                    )
                    assert response.status_code == 404

                    # 对象存储读不到最新版本原文件时 404，不泄露内部错误。
                    await storage.put("k/v2.pdf", b"")
                    latest_version.object_key = "k/missing.pdf"
                    await session.commit()
                    response = await client.get(path, headers=admin)
                    assert response.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(scenario())
