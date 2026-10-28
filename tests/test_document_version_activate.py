"""版本回滚：激活历史版本切换当前检索版本，原文件预览/下载跟随当前版本。"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.api.v1 import documents as document_api
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentVersion,
    IngestionJob,
    KnowledgeBase,
    ModelProfile,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


class _StubStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, content: bytes) -> None:
        self.objects[key] = content

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]


def _make_settings() -> Settings:
    return Settings(_env_file=None, auth_admin_token="admin-test-token")


def _app(monkeypatch, settings):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


async def _seed(factory, *, kb_status: str = "active"):
    """两个入库成功版本（v2 为当前）+ 一个失败版本 v3。"""
    async with factory() as session:
        profile = ModelProfile(
            name="activate-embedding",
            kind="embedding",
            provider="openai_compatible",
            model_name="test-embedding",
            dimensions=2,
        )
        session.add(profile)
        await session.flush()
        knowledge_base = KnowledgeBase(
            name="回滚知识库",
            embedding_model_id=profile.id,
            status=kb_status,
        )
        session.add(knowledge_base)
        await session.flush()
        document = Document(
            knowledge_base_id=knowledge_base.id,
            document_key="activate-1",
            filename="说明书.pdf",
            latest_version_number=3,
            status="failed",
        )
        session.add(document)
        await session.flush()
        v1 = DocumentVersion(
            document_id=document.id,
            version_number=1,
            filename="说明书.pdf",
            size_bytes=5,
            content_sha256="a" * 64,
            object_key="k/v1.pdf",
            media_type="application/pdf",
            status="completed",
            is_current=False,
        )
        v2 = DocumentVersion(
            document_id=document.id,
            version_number=2,
            filename="说明书.pdf",
            size_bytes=6,
            content_sha256="b" * 64,
            object_key="k/v2.pdf",
            media_type="application/pdf",
            status="completed",
            is_current=True,
        )
        v3 = DocumentVersion(
            document_id=document.id,
            version_number=3,
            filename="说明书.pdf",
            size_bytes=7,
            content_sha256="c" * 64,
            object_key="k/v3.pdf",
            media_type="application/pdf",
            status="failed",
            is_current=False,
        )
        session.add_all([v1, v2, v3])
        await session.flush()
        await session.commit()
        return SimpleNamespace(
            session=session,
            knowledge_base=knowledge_base,
            document=document,
            v1=v1,
            v2=v2,
            v3=v3,
        )


def test_activate_switches_version_and_preview_follows(monkeypatch, tmp_path) -> None:
    settings = _make_settings()
    storage = _StubStorage()
    monkeypatch.setattr(document_api, "get_object_storage", lambda settings: storage)
    preview_calls: list = []

    def fake_build_preview(version_id, filename, content):
        preview_calls.append(version_id)
        return SimpleNamespace(media_type="application/pdf", content=b"%PDF " + content)

    monkeypatch.setattr(document_api, "build_preview", fake_build_preview)

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'activate.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            seeded = await _seed(factory)
            await storage.put("k/v1.pdf", b"first")
            await storage.put("k/v2.pdf", b"second")

            async def db_session():
                yield seeded.session

            app = _app(monkeypatch, settings)
            app.dependency_overrides[get_db_session] = db_session
            admin = {"Authorization": "Bearer admin-test-token"}
            doc_id = seeded.document.id
            base = f"/api/v1/documents/{doc_id}"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # 回滚前原文件与预览都取当前版本 v2。
                response = await client.get(f"{base}/file", headers=admin)
                assert response.status_code == 200
                assert response.content == b"second"
                response = await client.get(f"{base}/preview", headers=admin)
                assert response.status_code == 200
                assert preview_calls[-1] == seeded.v2.id

                # 激活 v1：v1 变为当前版本，v2 取消。
                response = await client.post(
                    f"{base}/versions/{seeded.v1.id}/activate", headers=admin
                )
                assert response.status_code == 200
                body = response.json()
                assert body["is_current"] is True
                assert body["version_number"] == 1
                versions = (await client.get(f"{base}/versions", headers=admin)).json()
                flags = {row["version_number"]: row["is_current"] for row in versions}
                assert flags == {1: True, 2: False, 3: False}

                # 原文件与预览跟随回滚后的当前版本 v1。
                response = await client.get(f"{base}/file", headers=admin)
                assert response.status_code == 200
                assert response.content == b"first"
                response = await client.get(f"{base}/preview", headers=admin)
                assert response.status_code == 200
                assert preview_calls[-1] == seeded.v1.id

                # 再次激活 v1 幂等。
                response = await client.post(
                    f"{base}/versions/{seeded.v1.id}/activate", headers=admin
                )
                assert response.status_code == 200
                assert response.json()["is_current"] is True
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_activate_rejects_failed_version_foreign_version_and_archived_kb(
    monkeypatch, tmp_path
) -> None:
    settings = _make_settings()
    storage = _StubStorage()
    monkeypatch.setattr(document_api, "get_object_storage", lambda settings: storage)

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'activate2.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            seeded = await _seed(factory)

            async def db_session():
                yield seeded.session

            app = _app(monkeypatch, settings)
            app.dependency_overrides[get_db_session] = db_session
            admin = {"Authorization": "Bearer admin-test-token"}
            base = f"/api/v1/documents/{seeded.document.id}"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # 失败版本不能激活。
                response = await client.post(
                    f"{base}/versions/{seeded.v3.id}/activate", headers=admin
                )
                assert response.status_code == 409

                # 不属于该文档的版本 404。
                response = await client.post(
                    f"/api/v1/documents/{uuid4()}/versions/{seeded.v1.id}/activate",
                    headers=admin,
                )
                assert response.status_code == 404

                # 归档知识库不能切换版本；_seed 已关闭会话，这里用 UPDATE 直接把状态落库。
                await seeded.session.execute(
                    update(KnowledgeBase)
                    .where(KnowledgeBase.id == seeded.knowledge_base.id)
                    .values(status="archived")
                )
                await seeded.session.commit()
                response = await client.post(
                    f"{base}/versions/{seeded.v1.id}/activate", headers=admin
                )
                assert response.status_code == 409
                await seeded.session.execute(
                    update(KnowledgeBase)
                    .where(KnowledgeBase.id == seeded.knowledge_base.id)
                    .values(status="active")
                )
                await seeded.session.commit()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_activate_rejected_while_ingestion_pending(monkeypatch, tmp_path) -> None:
    settings = _make_settings()
    storage = _StubStorage()
    monkeypatch.setattr(document_api, "get_object_storage", lambda settings: storage)

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'activate3.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            seeded = await _seed(factory)
            seeded.session.add(
                IngestionJob(
                    document_id=seeded.document.id,
                    document_version_id=seeded.v3.id,
                    status="queued",
                )
            )
            await seeded.session.commit()

            async def db_session():
                yield seeded.session

            app = _app(monkeypatch, settings)
            app.dependency_overrides[get_db_session] = db_session
            admin = {"Authorization": "Bearer admin-test-token"}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/api/v1/documents/{seeded.document.id}/versions/{seeded.v1.id}/activate",
                    headers=admin,
                )
                assert response.status_code == 409
        finally:
            await engine.dispose()

    asyncio.run(scenario())
