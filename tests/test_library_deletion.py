"""验证公司级大库、子库和文件向量删除边界。"""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.api.v1 import companies as company_api
from knowledge_service.api.v1 import documents as document_api
from knowledge_service.api.v1.schemas import BatchFileVectorDeleteRequest
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    Document,
    KnowledgeBase,
    ModelProfile,
    VectorLibrary,
)


def _build_scope():
    """构造一组公司、大库和子库对象，供接口级测试复用。"""
    company = Company(code=f"company-{uuid4().hex}", name=f"测试公司-{uuid4().hex}")
    embedding = ModelProfile(
        name=f"deletion-embedding-{uuid4().hex}",
        kind="embedding",
        provider="openai_compatible",
        model_name="test-embedding",
        dimensions=2,
    )
    return company, embedding


def test_vector_library_delete_requires_all_sub_libraries_to_be_removed(tmp_path) -> None:
    """大库存在子库时返回 409，删除边界不能被绕过。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                company, embedding = _build_scope()
                session.add_all([company, embedding])
                await session.flush()
                library = VectorLibrary(
                    company_id=company.id,
                    name="测试大库",
                    embedding_model_id=embedding.id,
                    models_locked_at=company.created_at,
                )
                session.add(library)
                await session.flush()
                sub_library = KnowledgeBase(
                    company_id=company.id,
                    vector_library_id=library.id,
                    name="测试子库",
                    embedding_model_id=embedding.id,
                    models_locked_at=library.models_locked_at,
                )
                session.add(sub_library)
                await session.commit()

                with pytest.raises(HTTPException) as error:
                    await company_api.delete_vector_library(company.id, session)

                assert error.value.status_code == 409
                assert await session.scalar(select(VectorLibrary.id)) == library.id
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_sub_library_delete_removes_sub_library_after_vector_cleanup(tmp_path, monkeypatch) -> None:
    """子库删除先调用统一向量清理，再删除子库元数据。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sub-library.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                company, embedding = _build_scope()
                session.add_all([company, embedding])
                await session.flush()
                library = VectorLibrary(
                    company_id=company.id,
                    name="测试大库",
                    embedding_model_id=embedding.id,
                    models_locked_at=company.created_at,
                )
                session.add(library)
                await session.flush()
                sub_library = KnowledgeBase(
                    company_id=company.id,
                    vector_library_id=library.id,
                    name="待删除子库",
                    embedding_model_id=embedding.id,
                    models_locked_at=library.models_locked_at,
                )
                session.add(sub_library)
                await session.flush()
                sub_library_id = sub_library.id
                await session.commit()

                deleted_document_ids = []

                async def fake_delete_documents(_session, document_ids):
                    """测试替身只记录调用，真实清理逻辑由文档删除测试覆盖。"""
                    deleted_document_ids.extend(document_ids)
                    return [str(document_id) for document_id in document_ids]

                monkeypatch.setattr(company_api, "delete_documents", fake_delete_documents)
                await company_api.delete_sub_library(company.id, sub_library_id, session)

                assert deleted_document_ids == []
                assert await session.get(KnowledgeBase, sub_library_id) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_batch_file_vector_delete_requires_company_scope(tmp_path, monkeypatch) -> None:
    """批量删除只匹配当前公司的文件 ID，并返回后台业务文件 ID。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'file-vectors.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                company, embedding = _build_scope()
                session.add_all([company, embedding])
                await session.flush()
                library = VectorLibrary(
                    company_id=company.id,
                    name="测试大库",
                    embedding_model_id=embedding.id,
                    models_locked_at=company.created_at,
                )
                session.add(library)
                await session.flush()
                sub_library = KnowledgeBase(
                    company_id=company.id,
                    vector_library_id=library.id,
                    name="文件子库",
                    embedding_model_id=embedding.id,
                    models_locked_at=library.models_locked_at,
                )
                session.add(sub_library)
                await session.flush()
                document = Document(
                    company_id=company.id,
                    knowledge_base_id=sub_library.id,
                    document_key="file-001",
                    filename="说明书.pdf",
                    latest_version_number=1,
                    status="completed",
                )
                session.add(document)
                await session.commit()

                deleted_document_ids = []

                async def fake_delete_documents(_session, document_ids):
                    """测试替身避免测试依赖真实 Chroma 和对象存储。"""
                    deleted_document_ids.extend(document_ids)
                    return [str(document_id) for document_id in document_ids]

                monkeypatch.setattr(document_api, "delete_documents", fake_delete_documents)
                result = await document_api.batch_delete_file_vectors(
                    sub_library.id,
                    company.id,
                    BatchFileVectorDeleteRequest(file_ids=["file-001", "file-001"]),
                    session,
                )

                assert result.deleted_count == 1
                assert result.deleted_ids == ["file-001"]
                assert deleted_document_ids == [document.id]
        finally:
            await engine.dispose()

    asyncio.run(scenario())
