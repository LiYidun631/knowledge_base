"""用真实 SQL 事务验证离线用例，Embedding 与 Chroma 使用测试适配器。"""

import asyncio
import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.application.documents import ingestion
from knowledge_service.application.documents.service import register_document
from knowledge_service.application.knowledge_bases.service import (
    create_knowledge_base,
    update_knowledge_base,
)
from knowledge_service.core.config import Settings
from knowledge_service.domain.errors import ModelConfigurationLocked
from knowledge_service.infrastructure.chroma.vector_store import ChromaWriteError
from knowledge_service.infrastructure.object_storage.local import LocalObjectStorage
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    IngestionJob,
    ModelProfile,
    OutboxEvent,
)


@pytest.mark.parametrize("fail_once", [False, True])
def test_ingestion_completion_retry_and_duplicate_delivery(
    tmp_path, monkeypatch, fail_once
) -> None:
    """写入失败不能标为完成；重试后分块无重复，再次投递会安全跳过。"""
    settings = Settings(storage_root=tmp_path / "objects", object_storage_backend="local")
    monkeypatch.setattr(ingestion, "get_settings", lambda: settings)
    writes = []

    async def embed(profile, texts):
        return [[0.8, 0.2] for _ in texts]

    def write(**kwargs):
        writes.append(kwargs)
        if fail_once and len(writes) == 1:
            raise ChromaWriteError("CHROMA_WRITE_ERROR: 测试瞬时故障")

    monkeypatch.setattr(ingestion, "embed_texts", embed)
    monkeypatch.setattr(ingestion, "upsert_chunks", write)

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session, session.begin():
                embedding = ModelProfile(
                    name="test-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="test-embedding-model",
                    dimensions=2,
                    is_default=True,
                    settings={"base_url": "https://example.test/v1"},
                )
                session.add(embedding)
                await session.flush()
                kb = await create_knowledge_base(
                    session,
                    name="医学资料",
                    description=None,
                    embedding_model_id=None,
                    rerank_model_id=None,
                )
                assert kb.embedding_model_id == embedding.id
                assert kb.models_locked_at is not None
                with pytest.raises(ModelConfigurationLocked):
                    await update_knowledge_base(
                        session,
                        knowledge_base_id=kb.id,
                        values={"embedding_model_id": uuid4()},
                    )
                content = "# 病历\n\n这是医学资料。请结合临床评估。".encode()
                await LocalObjectStorage(settings.storage_root).put("sample.md", content)
                document = await register_document(
                    session,
                    knowledge_base_id=kb.id,
                    document_key="medical-guide",
                    filename="sample.md",
                    media_type="text/markdown",
                    size_bytes=len(content),
                    content_sha256=hashlib.sha256(content).hexdigest(),
                    object_key="sample.md",
                )
                job = await session.scalar(
                    select(IngestionJob).where(IngestionJob.document_id == document.id)
                )
                event = await session.scalar(select(OutboxEvent))
                assert event.payload["job_id"] == str(job.id)
                assert event.payload["document_version_id"] == str(job.document_version_id)
                job_id, doc_id = job.id, document.id

            result = await ingestion.run_ingestion(job_id, factory)
            if fail_once:
                assert result == "failed"
                async with factory() as session:
                    assert (await session.get(Document, doc_id)).status == "queued"
                    assert (
                        await session.scalar(select(func.count()).select_from(DocumentChunk)) == 0
                    )
                    assert await session.scalar(select(func.count()).select_from(OutboxEvent)) == 2
                result = await ingestion.run_ingestion(job_id, factory)
            assert result == "completed"
            async with factory() as session:
                assert (await session.get(Document, doc_id)).status == "completed"
                version = await session.get(DocumentVersion, job.document_version_id)
                assert version.status == "completed"
                assert version.is_current is True
                job = await session.get(IngestionJob, job_id)
                assert job.attempts == (2 if fail_once else 1)
                chunks = list(await session.scalars(select(DocumentChunk)))
                assert len(chunks) == 2
                assert len({chunk.id for chunk in chunks}) == 2
                assert chunks[1].extra_metadata["parent_chunk_id"] == str(chunks[0].id)
            assert await ingestion.run_ingestion(job_id, factory) == "skipped"
            assert len(writes) == (2 if fail_once else 1)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_new_version", [False, True])
def test_reupload_only_switches_current_version_after_success(
    tmp_path, monkeypatch, fail_new_version
) -> None:
    """新版本成功后才切换当前版本，失败时保留旧版检索资格。"""
    settings = Settings(
        storage_root=tmp_path / "objects", object_storage_backend="local", max_ingestion_attempts=1
    )
    monkeypatch.setattr(ingestion, "get_settings", lambda: settings)
    writes = []
    write_should_fail = [False]

    async def embed(profile, texts):
        return [[0.8, 0.2] for _ in texts]

    def write(**kwargs):
        writes.append(kwargs)
        if write_should_fail[0]:
            raise ChromaWriteError("CHROMA_WRITE_ERROR: 模拟新版本写入失败")

    monkeypatch.setattr(ingestion, "embed_texts", embed)
    monkeypatch.setattr(ingestion, "upsert_chunks", write)

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'versions.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session, session.begin():
                embedding = ModelProfile(
                    name="test-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="test-embedding-model",
                    dimensions=2,
                    is_default=True,
                    settings={"base_url": "https://example.test/v1"},
                )
                session.add(embedding)
                await session.flush()
                kb = await create_knowledge_base(
                    session,
                    name="知识库",
                    description=None,
                    embedding_model_id=embedding.id,
                    rerank_model_id=None,
                )

            storage = LocalObjectStorage(settings.storage_root)
            first_bytes = b"# Guide\n\nFirst revision content."
            await storage.put("v1.md", first_bytes)
            async with factory() as session, session.begin():
                first = await register_document(
                    session,
                    knowledge_base_id=kb.id,
                    document_key="guide-id",
                    filename="guide.md",
                    media_type="text/markdown",
                    size_bytes=len(first_bytes),
                    content_sha256=hashlib.sha256(first_bytes).hexdigest(),
                    object_key="v1.md",
                )
                first_id = first.id
                first_job = await session.scalar(
                    select(IngestionJob).where(IngestionJob.document_id == first_id)
                )
                first_job_id = first_job.id

            assert await ingestion.run_ingestion(first_job_id, factory) == "completed"

            second_bytes = b"# Guide\n\nSecond revision content is different."
            await storage.put("v2.md", second_bytes)
            async with factory() as session, session.begin():
                second = await register_document(
                    session,
                    knowledge_base_id=kb.id,
                    document_key="guide-id",
                    filename="guide.md",
                    media_type="text/markdown",
                    size_bytes=len(second_bytes),
                    content_sha256=hashlib.sha256(second_bytes).hexdigest(),
                    object_key="v2.md",
                )
                assert second.id == first_id
                assert second.latest_version_number == 2
                second_version = await session.scalar(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == first_id,
                        DocumentVersion.version_number == 2,
                    )
                )
                second_job = await session.scalar(
                    select(IngestionJob).where(
                        IngestionJob.document_version_id == second_version.id
                    )
                )
                second_job_id = second_job.id

            write_should_fail[0] = fail_new_version
            result = await ingestion.run_ingestion(second_job_id, factory)
            assert result == ("failed" if fail_new_version else "completed")

            async with factory() as session:
                document = await session.get(Document, first_id)
                versions = list(
                    await session.scalars(
                        select(DocumentVersion)
                        .where(DocumentVersion.document_id == first_id)
                        .order_by(DocumentVersion.version_number)
                    )
                )
                assert document.latest_version_number == 2
                assert document.status == ("failed" if fail_new_version else "completed")
                expected_versions = (
                    [(1, "completed", True), (2, "failed", False)]
                    if fail_new_version
                    else [(1, "completed", False), (2, "completed", True)]
                )
                actual_versions = [
                    (item.version_number, item.status, item.is_current) for item in versions
                ]
                assert actual_versions == expected_versions
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(DocumentChunk)
                        .where(DocumentChunk.document_version_id == versions[0].id)
                    )
                    > 0
                )
                second_chunk_count = await session.scalar(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.document_version_id == versions[1].id)
                )
                assert (second_chunk_count == 0) if fail_new_version else (second_chunk_count > 0)
                assert writes[0]["document_version_id"] != writes[1]["document_version_id"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())
