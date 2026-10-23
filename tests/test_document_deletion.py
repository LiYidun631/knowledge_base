"""验证文档物理删除及外部清理失败后的安全重试。"""

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.api.v1 import documents as document_api
from knowledge_service.application.documents.ingestion import (
    discard_ingestion_process_lock,
    ingestion_process_lock,
)
from knowledge_service.core.config import Settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentChunk,
    DocumentChunkTerm,
    DocumentVersion,
    IngestionJob,
    KnowledgeBase,
    ModelProfile,
    OutboxEvent,
)


def test_document_delete_retries_external_cleanup_and_removes_all_records(
    tmp_path, monkeypatch
) -> None:
    """向量清理首次失败时保留删除状态，重试后清理外部数据和全部关系记录。"""
    settings = Settings(storage_root=tmp_path / "objects", object_storage_backend="local")
    monkeypatch.setattr(document_api, "get_settings", lambda: settings)

    class StubStorage:
        deleted: list[str] = []

        async def delete(self, key: str) -> None:
            self.deleted.append(key)

    storage = StubStorage()
    monkeypatch.setattr(document_api, "get_object_storage", lambda settings: storage)
    attempts = []

    def delete_vectors(*, embedding, version_ids) -> None:
        attempts.append(version_ids)
        if len(attempts) == 1:
            raise RuntimeError("temporary Chroma failure")

    monkeypatch.setattr(document_api, "delete_document_versions", delete_vectors)

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'delete.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session, session.begin():
                profile = ModelProfile(
                    name="删除测试模型",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="test-embedding",
                    dimensions=2,
                    settings={"base_url": "https://example.test/v1"},
                )
                session.add(profile)
                await session.flush()
                knowledge_base = KnowledgeBase(
                    name="删除测试知识库", embedding_model_id=profile.id
                )
                session.add(knowledge_base)
                await session.flush()
                document = Document(
                    knowledge_base_id=knowledge_base.id,
                    document_key="delete-me",
                    filename="delete-me.txt",
                    latest_version_number=1,
                    status="completed",
                )
                session.add(document)
                await session.flush()
                version = DocumentVersion(
                    document_id=document.id,
                    version_number=1,
                    filename="delete-me.txt",
                    size_bytes=7,
                    content_sha256="a" * 64,
                    object_key="kb/delete-me.txt",
                    status="completed",
                    is_current=True,
                )
                session.add(version)
                await session.flush()
                parent = DocumentChunk(
                    document_id=document.id,
                    document_version_id=version.id,
                    ordinal=0,
                    level="parent",
                    content_sha256="b" * 64,
                    content="待删除内容",
                )
                session.add_all(
                    [
                        IngestionJob(
                            document_id=document.id,
                            document_version_id=version.id,
                            status="completed",
                        ),
                        parent,
                        OutboxEvent(
                            event_type="document.ingestion_requested",
                            aggregate_type="document",
                            aggregate_id=document.id,
                            payload={"document_id": str(document.id)},
                        ),
                    ]
                )
                await session.flush()
                child = DocumentChunk(
                    document_id=document.id,
                    document_version_id=version.id,
                    parent_chunk_id=parent.id,
                    ordinal=1,
                    level="child",
                    content_sha256="c" * 64,
                    content="待删除内容",
                    token_count=2,
                )
                session.add(child)
                await session.flush()
                session.add(
                    DocumentChunkTerm(
                        chunk_id=child.id,
                        document_id=document.id,
                        document_version_id=version.id,
                        term="待删",
                        frequency=1,
                    )
                )
                document_id, version_id = document.id, version.id

            async with factory() as session:
                with pytest.raises(HTTPException) as error:
                    await document_api.delete_document(document_id, session)
                assert error.value.status_code == 500

            async with factory() as session:
                document = await session.get(Document, document_id)
                version = await session.get(DocumentVersion, version_id)
                assert document.status == "deleting"
                assert version.status == "deleting"
                assert await session.scalar(select(func.count()).select_from(DocumentChunk)) == 2

            async with factory() as session:
                await document_api.delete_document(document_id, session)

            assert len(attempts) == 2
            assert attempts[0] == attempts[1] == [version_id]
            assert storage.deleted == ["kb/delete-me.txt"]
            async with factory() as session:
                for model in (
                    Document,
                    DocumentVersion,
                    DocumentChunk,
                    DocumentChunkTerm,
                    IngestionJob,
                    OutboxEvent,
                ):
                    assert await session.scalar(select(func.count()).select_from(model)) == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_delete_refuses_while_ingestion_process_lock_is_held(tmp_path, monkeypatch) -> None:
    """本进程入库协程仍在运行时，删除必须返回 409，释放后才能清理。"""
    settings = Settings(storage_root=tmp_path / "objects", object_storage_backend="local")
    monkeypatch.setattr(document_api, "get_settings", lambda: settings)

    class StubStorage:
        async def delete(self, key: str) -> None:
            return None

    monkeypatch.setattr(document_api, "get_object_storage", lambda settings: StubStorage())
    monkeypatch.setattr(document_api, "delete_document_versions", lambda **kwargs: None)

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'delete-lock.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session, session.begin():
                profile = ModelProfile(
                    name="删除锁测试模型",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="test-embedding",
                    dimensions=2,
                )
                session.add(profile)
                await session.flush()
                knowledge_base = KnowledgeBase(
                    name="删除锁测试知识库", embedding_model_id=profile.id
                )
                session.add(knowledge_base)
                await session.flush()
                document = Document(
                    knowledge_base_id=knowledge_base.id,
                    document_key="locking-doc",
                    filename="locking.txt",
                    latest_version_number=1,
                    status="processing",
                )
                session.add(document)
                await session.flush()
                version = DocumentVersion(
                    document_id=document.id,
                    version_number=1,
                    filename="locking.txt",
                    size_bytes=3,
                    content_sha256="a" * 64,
                    object_key="kb/locking.txt",
                    status="processing",
                )
                session.add(version)
                await session.flush()
                job = IngestionJob(
                    document_id=document.id,
                    document_version_id=version.id,
                    status="running",
                )
                session.add(job)
                await session.flush()
                document_id, job_id = document.id, job.id

            lock = ingestion_process_lock(job_id)
            await lock.acquire()
            try:
                async with factory() as session:
                    with pytest.raises(HTTPException) as error:
                        await document_api.delete_document(document_id, session)
                    assert error.value.status_code == 409
                async with factory() as session:
                    assert await session.get(Document, document_id) is not None
                    assert await session.get(IngestionJob, job_id) is not None
            finally:
                lock.release()
                discard_ingestion_process_lock(job_id, lock)

            async with factory() as session:
                await document_api.delete_document(document_id, session)
            async with factory() as session:
                assert await session.get(Document, document_id) is None
                assert await session.get(IngestionJob, job_id) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())
