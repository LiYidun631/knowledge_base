"""验证中文 BM25、当前版本过滤和混合排序。"""

import asyncio
import hashlib
from uuid import UUID, uuid4

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.api.integration import include_knowledge_module
from knowledge_service.application.search import service as search_service
from knowledge_service.application.search.bm25 import (
    BM25Hit,
    search_bm25,
    term_frequencies,
    tokenize,
)
from knowledge_service.application.search.service import _attach_context, _ParentResult
from knowledge_service.core.config import Settings
from knowledge_service.infrastructure.chroma import vector_store
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentChunk,
    DocumentChunkTerm,
    DocumentVersion,
    KnowledgeBase,
    ModelProfile,
)


def test_tokenizer_handles_chinese_english_and_repeated_terms() -> None:
    """中文相邻字可召回短语，英文大小写归一，重复词频准确。"""
    assert tokenize("高血压需要控制盐摄入") == [
        "高血",
        "血压",
        "压需",
        "需要",
        "要控",
        "控制",
        "制盐",
        "盐摄",
        "摄入",
    ]
    length, frequencies = term_frequencies("High blood pressure high")
    assert length == 4
    assert frequencies == {"high": 2, "blood": 1, "pressure": 1}


def test_search_route_is_registered_in_the_embedded_module() -> None:
    """在线检索路由随知识库模块挂载，宿主认证依赖仍由集成入口统一添加。"""
    app = FastAPI()
    include_knowledge_module(app, api_prefix="/api/v1")
    # FastAPI 会延迟合并嵌套路由，生成 OpenAPI 时能稳定得到最终公开路径。
    assert "/api/v1/knowledge-bases/{knowledge_base_id}/search" in app.openapi()["paths"]


def test_chroma_search_batches_and_filters_current_versions(monkeypatch) -> None:
    """每批 Chroma 查询都带知识库、版本和文档过滤条件。"""
    first_version, second_version = uuid4(), uuid4()
    first_chunk, second_chunk = uuid4(), uuid4()
    document_id, knowledge_base_id = uuid4(), uuid4()

    class Collection:
        calls = []

        def query(self, **kwargs):
            self.calls.append(kwargs)
            chunk_id = first_chunk if len(self.calls) == 1 else second_chunk
            return {"ids": [[str(chunk_id)]], "distances": [[0.2 if len(self.calls) == 1 else 0.1]]}

    class Client:
        def __init__(self):
            self.collection = Collection()

        def get_collection(self, *, name):
            return self.collection

    client = Client()
    profile = ModelProfile(
        name="chroma-test",
        kind="embedding",
        provider="openai_compatible",
        model_name="chroma-test",
        dimensions=2,
    )
    monkeypatch.setattr(vector_store, "_client", lambda: client)
    monkeypatch.setattr(vector_store, "collection_name", lambda _profile: "test-collection")
    results = vector_store.query_chunks(
        embedding=profile,
        knowledge_base_id=knowledge_base_id,
        document_version_ids=[first_version, second_version],
        document_ids=[document_id],
        query_vector=[0.1, 0.9],
        candidate_k=5,
        version_batch_size=1,
    )
    assert results == [(second_chunk, 0.1), (first_chunk, 0.2)]
    assert len(client.collection.calls) == 2
    for call, version_id in zip(
        client.collection.calls, [first_version, second_version], strict=True
    ):
        filters = call["where"]["$and"]
        assert {"knowledge_base_id": {"$eq": str(knowledge_base_id)}} in filters
        assert {"document_version_id": {"$in": [str(version_id)]}} in filters
        assert {"document_id": {"$in": [str(document_id)]}} in filters


def test_bm25_filters_old_versions_and_optional_document_scope(tmp_path) -> None:
    """BM25 只取当前成功版本，并能将检索范围限制到指定文档。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bm25.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session, session.begin():
                embedding = ModelProfile(
                    name="search-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="search-embedding",
                    dimensions=2,
                    settings={"base_url": "https://example.test/v1"},
                )
                session.add(embedding)
                await session.flush()
                kb = KnowledgeBase(name="BM25 测试库", embedding_model_id=embedding.id)
                session.add(kb)
                await session.flush()
                current_ids = []
                for key, contents in (
                    (
                        "guide-a",
                        [(1, False, "高血压高血压高血压旧版本"), (2, True, "高血压日常管理")],
                    ),
                    ("guide-b", [(1, True, "高血压患者需要监测血压")]),
                ):
                    document = Document(
                        knowledge_base_id=kb.id,
                        document_key=key,
                        filename=f"{key}.txt",
                        latest_version_number=contents[-1][0],
                        status="completed",
                    )
                    session.add(document)
                    await session.flush()
                    for version_number, is_current, content in contents:
                        version = DocumentVersion(
                            document_id=document.id,
                            version_number=version_number,
                            filename=document.filename,
                            size_bytes=len(content),
                            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                            object_key=f"{key}-{version_number}.txt",
                            status="completed",
                            is_current=is_current,
                        )
                        session.add(version)
                        await session.flush()
                        if is_current:
                            current_ids.append((document.id, version.id))
                        length, frequencies = term_frequencies(content)
                        chunk = DocumentChunk(
                            document_id=document.id,
                            document_version_id=version.id,
                            ordinal=1,
                            level="child",
                            content=content,
                            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                            token_count=length,
                        )
                        session.add(chunk)
                        await session.flush()
                        session.add_all(
                            [
                                DocumentChunkTerm(
                                    chunk_id=chunk.id,
                                    document_id=document.id,
                                    document_version_id=version.id,
                                    term=term,
                                    frequency=frequency,
                                )
                                for term, frequency in frequencies.items()
                            ]
                        )

                result = await search_bm25(
                    session,
                    knowledge_base_id=kb.id,
                    document_version_ids=[version_id for _doc_id, version_id in current_ids],
                    document_ids=None,
                    query="高血压",
                    limit=10,
                )
                assert len(result) == 2
                found_ids = [hit.chunk_id for hit in result]
                stale_version = await session.scalar(
                    select(DocumentVersion.id).where(
                        DocumentVersion.version_number == 1,
                        DocumentVersion.is_current.is_(False),
                    )
                )
                found_versions = await session.scalars(
                    select(DocumentChunk.document_version_id).where(DocumentChunk.id.in_(found_ids))
                )
                assert stale_version not in set(found_versions)

                only_second_document = await search_bm25(
                    session,
                    knowledge_base_id=kb.id,
                    document_version_ids=[version_id for _doc_id, version_id in current_ids],
                    document_ids=[current_ids[1][0]],
                    query="高血压",
                    limit=10,
                )
                assert len(only_second_document) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_hybrid_search_fuses_sources_and_drops_stale_candidates(tmp_path, monkeypatch) -> None:
    """混合检索按 RRF 融合，同时二次校验并丢弃旧版本候选。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hybrid.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session, session.begin():
                embedding = ModelProfile(
                    name="hybrid-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="hybrid-embedding",
                    dimensions=2,
                    settings={"base_url": "https://example.test/v1"},
                )
                reranker = ModelProfile(
                    name="hybrid-reranker",
                    kind="rerank",
                    provider="dashscope",
                    model_name="test-reranker",
                    settings={"base_url": "https://example.test/v1", "rerank_path": "/reranks"},
                )
                session.add_all([embedding, reranker])
                await session.flush()
                kb = KnowledgeBase(
                    name="混合检索测试库",
                    embedding_model_id=embedding.id,
                    rerank_model_id=reranker.id,
                )
                session.add(kb)
                await session.flush()
                candidates: dict[str, tuple[UUID, UUID, UUID, UUID]] = {}
                for name in ("vector", "bm25", "stale"):
                    document = Document(
                        knowledge_base_id=kb.id,
                        document_key=name,
                        filename=f"{name}.txt",
                        latest_version_number=1,
                        status="completed",
                    )
                    session.add(document)
                    await session.flush()
                    version = DocumentVersion(
                        document_id=document.id,
                        version_number=1,
                        filename=document.filename,
                        size_bytes=1,
                        content_sha256="a" * 64,
                        object_key=f"{name}.txt",
                        status="completed",
                        is_current=name != "stale",
                    )
                    session.add(version)
                    await session.flush()
                    parent = DocumentChunk(
                        document_id=document.id,
                        document_version_id=version.id,
                        ordinal=1,
                        level="parent",
                        content=f"{name} parent context",
                        content_sha256="b" * 64,
                        extra_metadata={"section": "测试章节"},
                    )
                    session.add(parent)
                    await session.flush()
                    child = DocumentChunk(
                        document_id=document.id,
                        document_version_id=version.id,
                        parent_chunk_id=parent.id,
                        ordinal=2,
                        level="child",
                        content=f"{name} child text",
                        content_sha256="c" * 64,
                        token_count=3,
                    )
                    session.add(child)
                    await session.flush()
                    candidates[name] = (document.id, version.id, child.id, parent.id)
                await session.flush()

                async def embed(_profile, _texts):
                    return [[0.2, 0.8]]

                def query(**_kwargs):
                    return [
                        (candidates["vector"][2], 0.05),
                        (candidates["stale"][2], 0.01),
                    ]

                async def lexical(*_args, **_kwargs):
                    return [
                        BM25Hit(candidates["bm25"][2], 2.0),
                        BM25Hit(candidates["stale"][2], 20.0),
                    ]

                async def rerank(_profile, *, query, documents):
                    return [
                        (index, float(len(documents) - index)) for index in range(len(documents))
                    ]

                monkeypatch.setattr(search_service, "get_settings", lambda: Settings())
                monkeypatch.setattr(search_service, "embed_texts", embed)
                monkeypatch.setattr(search_service, "query_chunks", query)
                monkeypatch.setattr(search_service, "search_bm25", lexical)
                monkeypatch.setattr(search_service, "rerank_documents", rerank)
                results = await search_service.search_knowledge_base(
                    session,
                    knowledge_base_id=kb.id,
                    query="测试查询",
                    top_k=5,
                    document_ids=None,
                )
                assert {item.document_id for item in results} == {
                    candidates["vector"][0],
                    candidates["bm25"][0],
                }
                assert results[0].document_id == candidates["vector"][0]
                assert results[0].file_id == "vector"
                assert all(item.section == "测试章节" for item in results)
                assert all(item.rerank_score is not None for item in results)

                async def unexpected_external_call(*_args, **_kwargs):
                    raise AssertionError("纯 BM25 模式不应调用外部检索服务")

                monkeypatch.setattr(search_service, "embed_texts", unexpected_external_call)
                monkeypatch.setattr(search_service, "query_chunks", unexpected_external_call)
                monkeypatch.setattr(search_service, "rerank_documents", unexpected_external_call)
                bm25_results = await search_service.search_knowledge_base(
                    session,
                    knowledge_base_id=kb.id,
                    query="测试查询",
                    top_k=5,
                    document_ids=None,
                    retrieval_mode="bm25",
                )
                assert [item.document_id for item in bm25_results] == [candidates["bm25"][0]]
                assert bm25_results[0].cosine_distance is None
                assert bm25_results[0].rerank_score is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_context_expansion_keeps_hit_and_stops_at_section_boundary(tmp_path) -> None:
    """上下文只扩展同章节父块，字符上限过小时仍保留命中块。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'context.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session, session.begin():
                embedding = ModelProfile(
                    name="context-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="context-embedding",
                    dimensions=2,
                )
                session.add(embedding)
                await session.flush()
                knowledge_base = KnowledgeBase(
                    name="上下文测试库", embedding_model_id=embedding.id
                )
                session.add(knowledge_base)
                await session.flush()
                document = Document(
                    knowledge_base_id=knowledge_base.id,
                    document_key="context-doc",
                    filename="context.txt",
                    latest_version_number=1,
                    status="completed",
                )
                session.add(document)
                await session.flush()
                version = DocumentVersion(
                    document_id=document.id,
                    version_number=1,
                    filename=document.filename,
                    size_bytes=1,
                    content_sha256="a" * 64,
                    object_key="context.txt",
                    status="completed",
                    is_current=True,
                )
                session.add(version)
                await session.flush()
                parents = []
                for ordinal, content, section in (
                    (1, "前一个父块", "章节一"),
                    (3, "命中父块", "章节一"),
                    (5, "章节二父块", "章节二"),
                ):
                    parent = DocumentChunk(
                        document_id=document.id,
                        document_version_id=version.id,
                        ordinal=ordinal,
                        level="parent",
                        content=content,
                        content_sha256="b" * 64,
                        extra_metadata={"section": section},
                    )
                    session.add(parent)
                    parents.append(parent)
                await session.flush()
                result = _ParentResult(
                    chunk_id=parents[1].id,
                    content=parents[1].content,
                    document_id=document.id,
                    document_version_id=version.id,
                    version_number=1,
                    filename=document.filename,
                    page_number=None,
                    section="章节一",
                )
                settings = Settings(
                    search_context_before=1,
                    search_context_after=1,
                    search_max_context_chars=100,
                )
                await _attach_context(session, [result], settings)
                assert [item.relation for item in result.context_sources] == ["before", "hit"]
                assert "命中父块" in result.context
                assert "章节二父块" not in result.context
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_full_section_context_includes_nested_steps_in_source_order(tmp_path) -> None:
    """整体使用方法问题应带回同一章节下不同小节的全部原文。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'full-section.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session, session.begin():
                embedding = ModelProfile(
                    name="section-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="section-embedding",
                    dimensions=2,
                )
                session.add(embedding)
                await session.flush()
                knowledge_base = KnowledgeBase(name="章节测试库", embedding_model_id=embedding.id)
                session.add(knowledge_base)
                await session.flush()
                document = Document(
                    knowledge_base_id=knowledge_base.id,
                    document_key="section-doc",
                    filename="guide.docx",
                    latest_version_number=1,
                    status="completed",
                )
                session.add(document)
                await session.flush()
                version = DocumentVersion(
                    document_id=document.id,
                    version_number=1,
                    filename=document.filename,
                    size_bytes=1,
                    content_sha256="d" * 64,
                    object_key="guide.docx",
                    status="completed",
                    is_current=True,
                )
                session.add(version)
                await session.flush()
                parents = []
                sections = (
                    "3 正确的使用方法",
                    "3 正确的使用方法 / 3.1 测量步骤",
                    "3 正确的使用方法 / 3.1 测量步骤 / 3.1.1 耳温测量",
                    "3 正确的使用方法 / 3.1 测量步骤 / 3.1.2 额温测量",
                    "4 清洁保养",
                )
                for ordinal, section in enumerate(sections):
                    section_path = [part.strip() for part in section.split(" / ")]
                    section_path_ids = [
                        "/".join(section_path[:depth])
                        for depth in range(1, len(section_path) + 1)
                    ]
                    parent = DocumentChunk(
                        document_id=document.id,
                        document_version_id=version.id,
                        ordinal=ordinal * 2 + 1,
                        level="parent",
                        content=f"原文第{ordinal + 1}段 [[KB_ASSET:marker-{ordinal}]]",
                        content_sha256="e" * 64,
                        extra_metadata={
                            "section": section,
                            "section_path": section_path,
                            "section_path_ids": section_path_ids,
                        },
                    )
                    session.add(parent)
                    parents.append(parent)
                await session.flush()
                result = _ParentResult(
                    chunk_id=parents[2].id,
                    content=parents[2].content,
                    document_id=document.id,
                    document_version_id=version.id,
                    version_number=1,
                    filename=document.filename,
                    page_number=None,
                    section=sections[2],
                    section_path=[part.strip() for part in sections[2].split(" / ")],
                )
                settings = Settings(search_max_context_chars=1000)
                await _attach_context(
                    session,
                    [result],
                    settings,
                    expand_section=True,
                    section_query="红外体温计的正确使用方法",
                )
                assert result.context is not None
                assert result.context.index("原文第1段") < result.context.index("原文第2段")
                assert result.context.index("原文第2段") < result.context.index("原文第3段")
                assert result.context.index("原文第3段") < result.context.index("原文第4段")
                assert "原文第5段" not in result.context
                assert len(result.context_sources) == 4
        finally:
            await engine.dispose()

    asyncio.run(scenario())
