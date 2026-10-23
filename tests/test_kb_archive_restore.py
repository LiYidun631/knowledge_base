import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.application.knowledge_bases import service as kb_service
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.domain.errors import KnowledgeBaseNotFound
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    Document,
    DocumentVersion,
    IngestionJob,
    KnowledgeBase,
    ModelProfile,
    OutboxEvent,
    VectorLibrary,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


@asynccontextmanager
async def _fixture(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'archive.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            embedding = ModelProfile(
                name="归档-embedding",
                kind="embedding",
                provider="openai_compatible",
                model_name="text-embedding-3-small",
                dimensions=1536,
                is_default=True,
            )
            company = Company(code="archive-company", name="归档公司")
            session.add_all([embedding, company])
            await session.flush()
            library = VectorLibrary(
                company_id=company.id,
                name="归档大库",
                embedding_model_id=embedding.id,
            )
            session.add(library)
            await session.flush()
            kb = KnowledgeBase(
                company_id=company.id,
                vector_library_id=library.id,
                name="归档子库",
                embedding_model_id=embedding.id,
            )
            session.add(kb)
            await session.commit()
            yield factory, company.id, kb.id
    finally:
        await engine.dispose()


def test_archive_hides_from_list_and_restore_recovers(tmp_path) -> None:
    async def run() -> None:
        async with _fixture(tmp_path) as (factory, company_id, kb_id):
            async with factory() as session, session.begin():
                await kb_service.archive_knowledge_base(session, knowledge_base_id=kb_id)

                default_list = await kb_service.list_knowledge_bases(
                    session, company_id=company_id, offset=0, limit=50
                )
                assert all(item.id != kb_id for item in default_list)

                full_list = await kb_service.list_knowledge_bases(
                    session, company_id=company_id, offset=0, limit=50, include_archived=True
                )
                archived = next(item for item in full_list if item.id == kb_id)
                assert archived.status == "archived"
                assert archived.archived_at is not None

                restored = await kb_service.restore_knowledge_base(
                    session, knowledge_base_id=kb_id
                )
                assert restored.status == "active"
                assert restored.archived_at is None

                with pytest.raises(KnowledgeBaseNotFound):
                    await kb_service.restore_knowledge_base(
                        session, knowledge_base_id=uuid4()
                    )

    asyncio.run(run())


def test_http_archive_list_and_restore_flow(monkeypatch, tmp_path) -> None:
    async def scenario():
        async with _fixture(tmp_path) as (factory, company_id, kb_id):
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
                response = await client.delete(f"knowledge-bases/{kb_id}")
                assert response.status_code == 204

                response = await client.get(
                    "knowledge-bases", params={"company_id": str(company_id)}
                )
                assert all(item["id"] != str(kb_id) for item in response.json())

                response = await client.get(
                    "knowledge-bases",
                    params={"company_id": str(company_id), "include_archived": "true"},
                )
                archived = next(
                    item for item in response.json() if item["id"] == str(kb_id)
                )
                assert archived["status"] == "archived"

                response = await client.post(f"knowledge-bases/{kb_id}/restore")
                assert response.status_code == 200, response.text
                assert response.json()["status"] == "active"

                response = await client.get(
                    "knowledge-bases", params={"company_id": str(company_id)}
                )
                assert any(item["id"] == str(kb_id) for item in response.json())

                response = await client.post(
                    "knowledge-bases/11111111-1111-4111-8111-111111111111/restore"
                )
                assert response.status_code == 404

    asyncio.run(scenario())


def test_retry_rejected_for_archived_kb(monkeypatch, tmp_path) -> None:
    """归档库不允许重试失败入库；恢复后重试重新排队并写入 Outbox 事件。"""
    async def scenario():
        async with _fixture(tmp_path) as (factory, _, kb_id):
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
            async with factory() as session, session.begin():
                document = Document(
                    knowledge_base_id=kb_id,
                    document_key="retry-doc",
                    filename="retry.txt",
                    latest_version_number=1,
                    status="failed",
                )
                session.add(document)
                await session.flush()
                version = DocumentVersion(
                    document_id=document.id,
                    version_number=1,
                    filename="retry.txt",
                    size_bytes=3,
                    content_sha256="a" * 64,
                    object_key="kb/retry.txt",
                    status="failed",
                    is_current=True,
                )
                session.add(version)
                await session.flush()
                job = IngestionJob(
                    document_id=document.id,
                    document_version_id=version.id,
                    status="failed",
                    stage="parse",
                    attempts=1,
                    error_details={"code": "INGESTION_INTERNAL_ERROR"},
                )
                session.add(job)
                await session.flush()
                document_id, job_id = document.id, job.id

            async with factory() as session, session.begin():
                await kb_service.archive_knowledge_base(session, knowledge_base_id=kb_id)

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test/api/v1/",
                headers={"Authorization": "Bearer admin-test-token"},
            ) as client:
                response = await client.post(f"documents/{document_id}/retry")
                assert response.status_code == 409
                assert "归档" in response.json()["detail"]
                async with factory() as session:
                    job = await session.get(IngestionJob, job_id)
                    assert job.status == "failed"

                async with factory() as session, session.begin():
                    await kb_service.restore_knowledge_base(session, knowledge_base_id=kb_id)

                response = await client.post(f"documents/{document_id}/retry")
                assert response.status_code == 200, response.text
                assert response.json()["status"] == "queued"
                async with factory() as session:
                    job = await session.get(IngestionJob, job_id)
                    assert job.status == "queued"
                    events = await session.scalar(
                        select(func.count()).select_from(OutboxEvent)
                    )
                    assert events == 1

    asyncio.run(scenario())
