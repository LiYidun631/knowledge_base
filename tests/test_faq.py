"""FAQ 领域服务与管理员接口验证：两级 CRUD、语义匹配、自动生成与改写。"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.api.v1 import answer as answer_module
from knowledge_service.api.v1.schemas import KnowledgeAnswerRequest
from knowledge_service.application.faq import service as faq_service
from knowledge_service.application.faq.service import (
    cosine_similarity,
    create_faq,
    generate_for_document_version,
    match_faq,
    rewrite_faq,
    update_faq,
)
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    FaqEntry,
    KnowledgeBase,
    ModelProfile,
    OutboxEvent,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def _vector(*values: float) -> list[float]:
    return list(values)


async def _seed_kb_with_embedding(factory) -> tuple[KnowledgeBase, ModelProfile]:
    async with factory() as session:
        async with session.begin():
            embedding = ModelProfile(
                name="faq-embedding",
                kind="embedding",
                provider="openai_compatible",
                model_name="faq-embedding",
                dimensions=3,
                is_enabled=True,
            )
            session.add(embedding)
            await session.flush()
            knowledge_base = KnowledgeBase(name="FAQ 测试库", embedding_model_id=embedding.id)
            session.add(knowledge_base)
            await session.flush()
            return knowledge_base, embedding


def test_cosine_similarity_edges() -> None:
    """余弦相似度：同向量为 1，正交为 0，维度不一致与零向量安全返回 0。"""
    assert cosine_similarity([1, 0], [1, 0]) == 1.0
    assert abs(cosine_similarity([1, 0], [0, 1])) < 1e-9
    assert abs(cosine_similarity([1, 1], [1, -1])) < 1e-9
    assert cosine_similarity([1, 0], [1, 0, 1]) == 0.0
    assert cosine_similarity([0, 0], [1, 0]) == 0.0
    assert cosine_similarity([], [1]) == 0.0


def test_faq_crud_and_match(monkeypatch, tmp_path) -> None:
    """手动 FAQ 可建改查；问题变更重新向量化；匹配达到阈值返回最佳条目。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'faq-crud.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            knowledge_base, _ = await _seed_kb_with_embedding(factory)

            async def fake_embed(_profile, texts: list[str]) -> list[list[float]]:
                vectors = {
                    "如何退款？": _vector(1, 0, 0),
                    "退款需要多久？": _vector(0.9, 0.1, 0),
                    "退款周期": _vector(0.95, 0.05, 0),
                    "如何续费？": _vector(0, 1, 0),
                    "保修范围是什么？": _vector(0, 0, 1),
                    "保修范围": _vector(0, 0, 1),
                }
                return [vectors[text] for text in texts]

            monkeypatch.setattr(faq_service, "embed_texts", fake_embed)

            async with factory() as session:
                async with session.begin():
                    entry = await create_faq(
                        session,
                        kb=knowledge_base,
                        question="如何退款？",
                        answer="订单完成后七天内可申请退款。",
                    )
                    assert entry.source == "manual"
                    assert entry.document_id is None

                    saved = await session.scalar(
                        select(FaqEntry).where(FaqEntry.id == entry.id)
                    )
                    assert saved.question_embedding == _vector(1, 0, 0)

                    # 只改答案不重新向量化。
                    await update_faq(session, kb=knowledge_base, entry=entry, answer="七天内可退。")
                    assert entry.answer == "七天内可退。"

                    # 改问题后向量跟着换。
                    await update_faq(
                        session, kb=knowledge_base, entry=entry, question="退款需要多久？"
                    )
                    assert entry.question == "退款需要多久？"
                    assert entry.question_embedding == _vector(0.9, 0.1, 0)

                    # 精确同义向量命中，且库级条目参与匹配。
                    hit = await match_faq(
                        session, kb=knowledge_base, query="退款周期", threshold=0.95
                    )
                    assert hit is not None and hit.id == entry.id

                    # 阈值抬高到不可能值则落空。
                    assert (
                        await match_faq(
                            session, kb=knowledge_base, query="退款周期", threshold=1.01
                        )
                        is None
                    )

                    # 无关问题落空。
                    assert (
                        await match_faq(
                            session, kb=knowledge_base, query="保修范围", threshold=0.8
                        )
                        is None
                    )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_match_faq_excludes_stale_document_versions(monkeypatch, tmp_path) -> None:
    """文档级 FAQ 只匹配当前版本；旧版本条目自动失效，库级条目始终有效。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'faq-match.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            knowledge_base, _ = await _seed_kb_with_embedding(factory)
            async with factory() as session:
                async with session.begin():
                    document = Document(
                        knowledge_base_id=knowledge_base.id,
                        document_key="faq-doc",
                        filename="faq-doc.md",
                        extra_metadata={"product_names": ["体脂秤"], "model_names": ["A1"]},
                    )
                    session.add(document)
                    await session.flush()
                    current = DocumentVersion(
                        document_id=document.id,
                        version_number=1,
                        filename="faq-doc.md",
                        size_bytes=10,
                        content_sha256="a" * 64,
                        object_key="k/1",
                        status="completed",
                        is_current=True,
                    )
                    stale = DocumentVersion(
                        document_id=document.id,
                        version_number=2,
                        filename="faq-doc.md",
                        size_bytes=10,
                        content_sha256="b" * 64,
                        object_key="k/2",
                        status="completed",
                        is_current=False,
                    )
                    session.add_all([current, stale])
                    await session.flush()
                    kb_level = FaqEntry(
                        knowledge_base_id=knowledge_base.id,
                        question="库级问题？",
                        answer="库级答案。",
                        question_embedding=_vector(1, 0, 0),
                        source="manual",
                    )
                    current_level = FaqEntry(
                        knowledge_base_id=knowledge_base.id,
                        document_id=document.id,
                        document_version_id=current.id,
                        question="当前版本问题？",
                        answer="当前版本答案。",
                        question_embedding=_vector(0, 1, 0),
                        source="auto",
                    )
                    stale_level = FaqEntry(
                        knowledge_base_id=knowledge_base.id,
                        document_id=document.id,
                        document_version_id=stale.id,
                        question="旧版本问题？",
                        answer="旧版本答案。",
                        question_embedding=_vector(0, 0, 1),
                        source="auto",
                    )
                    session.add_all([kb_level, current_level, stale_level])

            async def fake_embed(_profile, texts: list[str]) -> list[list[float]]:
                vectors = {
                    "库级命中": _vector(1, 0, 0),
                    "版本命中": _vector(0, 1, 0),
                    "旧版命中": _vector(0, 0, 1),
                }
                return [vectors[text] for text in texts]

            monkeypatch.setattr(faq_service, "embed_texts", fake_embed)

            async with factory() as session:
                hit = await match_faq(
                    session, kb=knowledge_base, query="库级命中", threshold=0.9
                )
                assert hit is not None and hit.source == "manual"
                # 库级 FAQ 携带产品过滤时同样可命中。
                hit = await match_faq(
                    session,
                    kb=knowledge_base,
                    query="库级命中",
                    product_name="体脂秤",
                    threshold=0.9,
                )
                assert hit is not None and hit.source == "manual"
                hit = await match_faq(
                    session,
                    kb=knowledge_base,
                    query="版本命中",
                    product_name="体脂秤",
                    threshold=0.9,
                )
                assert hit is not None and hit.question == "当前版本问题？"
                # 文档级 FAQ：未携带产品过滤不放开。
                assert (
                    await match_faq(
                        session, kb=knowledge_base, query="版本命中", threshold=0.9
                    )
                    is None
                )
                # 文档级 FAQ：产品不匹配不放开。
                assert (
                    await match_faq(
                        session,
                        kb=knowledge_base,
                        query="版本命中",
                        product_name="体温计",
                        threshold=0.9,
                    )
                    is None
                )
                # 旧版本条目被过滤，即使向量完全一致也不返回。
                assert (
                    await match_faq(
                        session,
                        kb=knowledge_base,
                        query="旧版命中",
                        product_name="体脂秤",
                        threshold=0.9,
                    )
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_match_faq_product_scope(monkeypatch, tmp_path) -> None:
    """文档级 FAQ 按产品/型号过滤：标签匹配可命中，不匹配或未携带过滤均不命中；
    未标注产品的通用资料文档在携带过滤时照常参与匹配。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'faq-scope.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            knowledge_base, _ = await _seed_kb_with_embedding(factory)
            async with factory() as session:
                async with session.begin():
                    labeled = Document(
                        knowledge_base_id=knowledge_base.id,
                        document_key="labeled",
                        filename="labeled.md",
                        extra_metadata={"product_names": ["体脂秤"], "model_names": ["A1", "A2"]},
                    )
                    generic = Document(
                        knowledge_base_id=knowledge_base.id,
                        document_key="generic",
                        filename="generic.md",
                    )
                    session.add_all([labeled, generic])
                    await session.flush()

                    def _version(document: Document, key: str) -> DocumentVersion:
                        version = DocumentVersion(
                            document_id=document.id,
                            version_number=1,
                            filename=document.filename,
                            size_bytes=10,
                            content_sha256=key * 64,
                            object_key=f"k/{key}",
                            status="completed",
                            is_current=True,
                        )
                        session.add(version)
                        return version

                    labeled_version = _version(labeled, "a")
                    generic_version = _version(generic, "b")
                    await session.flush()
                    session.add_all(
                        [
                            FaqEntry(
                                knowledge_base_id=knowledge_base.id,
                                document_id=labeled.id,
                                document_version_id=labeled_version.id,
                                question="体脂秤如何校准？",
                                answer="站上秤面等待归零。",
                                question_embedding=_vector(1, 0, 0),
                                source="auto",
                            ),
                            FaqEntry(
                                knowledge_base_id=knowledge_base.id,
                                document_id=generic.id,
                                document_version_id=generic_version.id,
                                question="公司地址在哪里？",
                                answer="杭州市西湖区。",
                                question_embedding=_vector(0, 1, 0),
                                source="auto",
                            ),
                        ]
                    )

            async def fake_embed(_profile, texts: list[str]) -> list[list[float]]:
                vectors = {
                    "校准查询": _vector(1, 0, 0),
                    "地址查询": _vector(0, 1, 0),
                }
                return [vectors[text] for text in texts]

            monkeypatch.setattr(faq_service, "embed_texts", fake_embed)

            async with factory() as session:
                # 产品匹配 → 命中。
                hit = await match_faq(
                    session,
                    kb=knowledge_base,
                    query="校准查询",
                    product_name="体脂秤",
                    threshold=0.9,
                )
                assert hit is not None and hit.question == "体脂秤如何校准？"
                # 型号匹配 → 命中。
                hit = await match_faq(
                    session,
                    kb=knowledge_base,
                    query="校准查询",
                    model_name="A2",
                    threshold=0.9,
                )
                assert hit is not None and hit.question == "体脂秤如何校准？"
                # 产品不匹配 → 不命中。
                assert (
                    await match_faq(
                        session,
                        kb=knowledge_base,
                        query="校准查询",
                        product_name="体温计",
                        threshold=0.9,
                    )
                    is None
                )
                # 型号不匹配 → 不命中。
                assert (
                    await match_faq(
                        session,
                        kb=knowledge_base,
                        query="校准查询",
                        model_name="A3",
                        threshold=0.9,
                    )
                    is None
                )
                # 未携带产品/型号 → 文档级不参与，只匹配库级（此处无库级 → 不命中）。
                assert (
                    await match_faq(session, kb=knowledge_base, query="校准查询", threshold=0.9)
                    is None
                )
                # 通用资料文档：携带任意过滤时参与匹配（与检索语义一致）。
                hit = await match_faq(
                    session,
                    kb=knowledge_base,
                    query="地址查询",
                    product_name="体脂秤",
                    threshold=0.9,
                )
                assert hit is not None and hit.question == "公司地址在哪里？"
                # 通用资料文档：未携带过滤同样不放开。
                assert (
                    await match_faq(session, kb=knowledge_base, query="地址查询", threshold=0.9)
                    is None
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_generate_for_document_version(monkeypatch, tmp_path) -> None:
    """文档级自动生成：提炼问答对落库；重复触发替换旧条目；无切块报错。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'faq-gen.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            knowledge_base, _ = await _seed_kb_with_embedding(factory)
            async with factory() as session:
                async with session.begin():
                    document = Document(
                        knowledge_base_id=knowledge_base.id,
                        document_key="gen-doc",
                        filename="gen-doc.md",
                    )
                    session.add(document)
                    await session.flush()
                    version = DocumentVersion(
                        document_id=document.id,
                        version_number=1,
                        filename="gen-doc.md",
                        size_bytes=10,
                        content_sha256="c" * 64,
                        object_key="k/3",
                        status="completed",
                        is_current=True,
                    )
                    session.add(version)
                    await session.flush()
                    chunk = DocumentChunk(
                        document_id=document.id,
                        document_version_id=version.id,
                        ordinal=0,
                        level="parent",
                        content_sha256="d" * 64,
                        content="设备首次开机需要长按电源键三秒。",
                    )
                    session.add(chunk)
                    await session.flush()
                    document_id, version_id = document.id, version.id

            async def fake_pairs(**kwargs):
                return [
                    {"question": "如何开机？", "answer": "长按电源键三秒。"},
                    {"question": "如何关机？", "answer": "长按电源键五秒。"},
                ]

            monkeypatch.setattr(faq_service, "generate_faq_pairs", fake_pairs)

            async def fake_embed(_profile, texts: list[str]) -> list[list[float]]:
                return [[float(len(text)) % 3, 0, 0] for text in texts]

            monkeypatch.setattr(faq_service, "embed_texts", fake_embed)

            async with factory() as session:
                async with session.begin():
                    entries = await generate_for_document_version(
                        session,
                        document_id=document_id,
                        document_version_id=version_id,
                    )
                    assert len(entries) == 2
                    assert all(entry.source == "auto" for entry in entries)
                    assert all(entry.document_id == document_id for entry in entries)
                    assert [entry.question for entry in entries] == ["如何开机？", "如何关机？"]
                    first_ids = {entry.id for entry in entries}

                    # 重复生成替换同版本自动条目，不残留旧条目。
                    entries = await generate_for_document_version(
                        session,
                        document_id=document_id,
                        document_version_id=version_id,
                    )
                    assert {entry.id for entry in entries} != first_ids
                    remaining = list(
                        await session.scalars(
                            select(FaqEntry).where(
                                FaqEntry.document_version_id == version_id
                            )
                        )
                    )
                    assert len(remaining) == 2

            # 没有父块切块的版本无法生成。
            async with factory() as session:
                async with session.begin():
                    document = Document(
                        knowledge_base_id=knowledge_base.id,
                        document_key="empty-doc",
                        filename="empty-doc.md",
                    )
                    session.add(document)
                    await session.flush()
                    version = DocumentVersion(
                        document_id=document.id,
                        version_number=1,
                        filename="empty-doc.md",
                        size_bytes=10,
                        content_sha256="e" * 64,
                        object_key="k/4",
                        status="completed",
                        is_current=True,
                    )
                    session.add(version)
                    await session.flush()
                    from knowledge_service.application.faq.service import FaqNotFound

                    try:
                        await generate_for_document_version(
                            session,
                            document_id=document.id,
                            document_version_id=version.id,
                        )
                        raise AssertionError("空切块版本应抛出 FaqNotFound")
                    except FaqNotFound:
                        pass
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_rewrite_faq(monkeypatch, tmp_path) -> None:
    """AI 改写答案：有指令按指令、无指令自动润色，结果落库。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'faq-rewrite.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            knowledge_base, _ = await _seed_kb_with_embedding(factory)
            async def fake_embed(_profile, texts):
                return [[1.0, 0, 0] for _ in texts]

            monkeypatch.setattr(faq_service, "embed_texts", fake_embed)
            calls: list[dict] = []

            async def fake_rewrite(**kwargs) -> str:
                calls.append(kwargs)
                return "改写后的答案。"

            monkeypatch.setattr(faq_service, "rewrite_faq_answer", fake_rewrite)

            async with factory() as session:
                async with session.begin():
                    entry = await create_faq(
                        session, kb=knowledge_base, question="如何开机？", answer="长按三秒。"
                    )
                    await rewrite_faq(
                        session, entry=entry, instruction="请写得正式一些"
                    )
                    assert entry.answer == "改写后的答案。"
                    assert calls[-1]["instruction"] == "请写得正式一些"
                    assert calls[-1]["question"] == "如何开机？"

                    await rewrite_faq(session, entry=entry)
                    assert calls[-1]["instruction"] is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_http_faq_endpoints(monkeypatch, tmp_path) -> None:
    """管理员 FAQ 接口全链路：库级 CRUD、文档级生成、改写与错误分支。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'faq-http.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
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
        monkeypatch.setattr(faq_service, "get_settings", lambda: settings)
        async def fake_embed(_profile, texts):
            return [[1.0, 0, 0] for _ in texts]

        monkeypatch.setattr(faq_service, "embed_texts", fake_embed)
        async def fake_pairs(**kwargs):
            return [
                {"question": "自动问题？", "answer": "自动答案。"},
                {"question": "自动问题二？", "answer": "自动答案二。"},
            ]

        async def fake_rewrite(**kwargs):
            return "AI 改写后的答案。"

        monkeypatch.setattr(faq_service, "generate_faq_pairs", fake_pairs)
        monkeypatch.setattr(faq_service, "rewrite_faq_answer", fake_rewrite)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
        ) as anonymous:
            response = await anonymous.get(f"admin/knowledge-bases/{uuid4()}/faqs")
            assert response.status_code == 401, response.text

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers={"Authorization": "Bearer admin-test-token"},
        ) as client:
            knowledge_base, _ = await _seed_kb_with_embedding(factory)
            async with factory() as session:
                async with session.begin():
                    document = Document(
                        knowledge_base_id=knowledge_base.id,
                        document_key="http-doc",
                        filename="http-doc.md",
                    )
                    session.add(document)
                    await session.flush()
                    version = DocumentVersion(
                        document_id=document.id,
                        version_number=1,
                        filename="http-doc.md",
                        size_bytes=10,
                        content_sha256="f" * 64,
                        object_key="k/5",
                        status="completed",
                        is_current=True,
                    )
                    session.add(version)
                    await session.flush()
                    session.add(
                        DocumentChunk(
                            document_id=document.id,
                            document_version_id=version.id,
                            ordinal=0,
                            level="parent",
                            content_sha256="a1" * 32,
                            content="开机长按电源键三秒。",
                        )
                    )
                    await session.flush()
                    document_id, version_id = document.id, version.id

            # 手动建库级 FAQ。
            response = await client.post(
                f"admin/knowledge-bases/{knowledge_base.id}/faqs",
                json={"question": "如何开机？", "answer": "长按电源键三秒。"},
            )
            assert response.status_code == 200, response.text
            created = response.json()
            assert created["source"] == "manual"
            assert created["document_id"] is None

            # 文档级 FAQ 需要属于该库的文档且存在当前版本。
            response = await client.post(
                f"admin/knowledge-bases/{knowledge_base.id}/faqs",
                json={
                    "question": "文档级问题？",
                    "answer": "文档级答案。",
                    "document_id": str(document_id),
                },
            )
            assert response.status_code == 200, response.text
            doc_faq = response.json()
            assert doc_faq["document_id"] == str(document_id)
            assert doc_faq["document_version_id"] == str(version_id)

            stranger_document = Document(
                knowledge_base_id=uuid4(),
                document_key="stranger",
                filename="stranger.md",
            )
            async with factory() as session:
                async with session.begin():
                    session.add(stranger_document)
                    await session.flush()
                    stranger_id = stranger_document.id
            response = await client.post(
                f"admin/knowledge-bases/{knowledge_base.id}/faqs",
                json={
                    "question": "越权问题？",
                    "answer": "越权答案。",
                    "document_id": str(stranger_id),
                },
            )
            assert response.status_code == 422

            # 列表与文档过滤。
            response = await client.get(f"admin/knowledge-bases/{knowledge_base.id}/faqs")
            assert response.status_code == 200
            body = response.json()
            assert body["total"] == 2
            assert len(body["items"]) == 2
            response = await client.get(
                f"admin/knowledge-bases/{knowledge_base.id}/faqs",
                params={"document_id": str(document_id)},
            )
            assert response.json()["total"] == 1

            # 搜索、来源与范围过滤；document 范围必须带 document_id。
            response = await client.get(
                f"admin/knowledge-bases/{knowledge_base.id}/faqs",
                params={"q": "文档级"},
            )
            assert response.json()["total"] == 1
            response = await client.get(
                f"admin/knowledge-bases/{knowledge_base.id}/faqs",
                params={"scope": "kb"},
            )
            assert response.json()["total"] == 1
            response = await client.get(
                f"admin/knowledge-bases/{knowledge_base.id}/faqs",
                params={"scope": "document", "document_id": str(document_id)},
            )
            assert response.json()["total"] == 1
            response = await client.get(
                f"admin/knowledge-bases/{knowledge_base.id}/faqs",
                params={"scope": "document"},
            )
            assert response.status_code == 422

            # 修改答案与问题。
            response = await client.patch(
                f"admin/faqs/{created['id']}",
                json={"answer": "长按五秒。", "question": "开机要多久？"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["answer"] == "长按五秒。"
            assert response.json()["question"] == "开机要多久？"
            response = await client.patch(f"admin/faqs/{created['id']}", json={})
            assert response.status_code == 422

            # AI 改写。
            response = await client.post(f"admin/faqs/{created['id']}/rewrite", json={})
            assert response.status_code == 200, response.text
            assert response.json()["answer"] == "AI 改写后的答案。"

            # 文档级自动生成与替换。
            response = await client.post(f"admin/documents/{document_id}/faqs/generate")
            assert response.status_code == 200, response.text
            entries = response.json()["entries"]
            assert len(entries) == 2
            assert all(entry["source"] == "auto" for entry in entries)
            response = await client.post(f"admin/documents/{document_id}/faqs/generate")
            assert len(response.json()["entries"]) == 2
            response = await client.get(
                f"admin/knowledge-bases/{knowledge_base.id}/faqs",
                params={"document_id": str(document_id)},
            )
            assert response.json()["total"] == 3  # 1 条手动文档级 + 2 条自动

            # 来源过滤与分页：自动条目 2 条，每页 1 条时 total 仍为全量。
            response = await client.get(
                f"admin/knowledge-bases/{knowledge_base.id}/faqs",
                params={"source": "auto", "page_size": 1, "page": 1},
            )
            body = response.json()
            assert body["total"] == 2
            assert body["page"] == 1
            assert body["page_size"] == 1
            assert len(body["items"]) == 1
            assert body["items"][0]["source"] == "auto"

            # 随机推荐：不传产品/型号只含库级条目；传过滤后通用资料文档级也参与；limit 生效。
            response = await client.get(
                f"admin/knowledge-bases/{knowledge_base.id}/faqs/random"
            )
            assert response.status_code == 200, response.text
            assert len(response.json()) == 1  # 仅 1 条库级
            response = await client.get(
                f"admin/knowledge-bases/{knowledge_base.id}/faqs/random",
                params={"product_name": "任意产品", "limit": 2},
            )
            assert response.status_code == 200, response.text
            assert len(response.json()) == 2
            response = await client.get(
                f"admin/knowledge-bases/{knowledge_base.id}/faqs/random",
                params={"limit": 50},
            )
            assert response.status_code == 422  # limit 上限 20

            # 删除与 404 分支。
            response = await client.delete(f"admin/faqs/{created['id']}")
            assert response.status_code == 204
            response = await client.delete(f"admin/faqs/{created['id']}")
            assert response.status_code == 404
            response = await client.patch(f"admin/faqs/{uuid4()}", json={"answer": "x"})
            assert response.status_code == 404
            response = await client.get(
                f"admin/knowledge-bases/{uuid4()}/faqs"
            )
            assert response.status_code == 404

        await engine.dispose()

    asyncio.run(scenario())


def test_batch_generate_faqs_enqueues_missing_documents(monkeypatch, tmp_path) -> None:
    """批量补齐：无自动 FAQ 的文档入队，已有条目或已在途的跳过，归档库拒绝。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'faq-batch.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            knowledge_base, _ = await _seed_kb_with_embedding(factory)
            async with factory() as session:
                async with session.begin():
                    docs = [
                        Document(
                            knowledge_base_id=knowledge_base.id,
                            document_key=f"batch-{index}",
                            filename=f"batch-{index}.md",
                        )
                        for index in (1, 2)
                    ]
                    session.add_all(docs)
                    await session.flush()
                    versions = [
                        DocumentVersion(
                            document_id=doc.id,
                            version_number=1,
                            filename=doc.filename,
                            size_bytes=10,
                            content_sha256=str(index) * 64,
                            object_key=f"k/batch-{index}",
                            status="completed",
                            is_current=True,
                        )
                        for index, doc in enumerate(docs, start=1)
                    ]
                    session.add_all(versions)
                    await session.flush()
                    # 第一份文档已有自动 FAQ，应被跳过。
                    session.add(
                        FaqEntry(
                            knowledge_base_id=knowledge_base.id,
                            document_id=docs[0].id,
                            document_version_id=versions[0].id,
                            question="已有问题？",
                            answer="已有答案。",
                            question_embedding=[1.0, 0, 0],
                            source="auto",
                        )
                    )
                    doc_ids = [doc.id for doc in docs]
                    version_ids = [version.id for version in versions]

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
                response = await client.post(
                    f"admin/knowledge-bases/{knowledge_base.id}/faqs/generate"
                )
                assert response.status_code == 200, response.text
                assert response.json() == {"queued": 1, "skipped": 1}

                async with factory() as session:
                    events = list(await session.scalars(select(OutboxEvent)))
                assert len(events) == 1
                assert events[0].event_type == "document.faq_generation_requested"
                assert events[0].payload["document_id"] == str(doc_ids[1])
                assert events[0].payload["document_version_id"] == str(version_ids[1])

                # 已在排队中的文档不重复入队。
                response = await client.post(
                    f"admin/knowledge-bases/{knowledge_base.id}/faqs/generate"
                )
                assert response.json() == {"queued": 0, "skipped": 2}

                async with factory() as session:
                    async with session.begin():
                        kb = await session.get(KnowledgeBase, knowledge_base.id)
                        kb.status = "archived"
                response = await client.post(
                    f"admin/knowledge-bases/{knowledge_base.id}/faqs/generate"
                )
                assert response.status_code == 409
                assert "归档" in response.json()["detail"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_ask_endpoint_faq_direct_answer(monkeypatch, tmp_path) -> None:
    """在线问答命中 FAQ 时原样直出：跳过检索与生成，记录标记成功。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'faq-ask.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            knowledge_base, _ = await _seed_kb_with_embedding(factory)
            async with factory() as session:
                async with session.begin():
                    kb = await session.get(KnowledgeBase, knowledge_base.id)
                    kb.status = "active"
                    kb.company_id = uuid4()
                    await session.flush()
            company_id = kb.company_id

            faq_entry = SimpleNamespace(
                id=uuid4(), answer="长按电源键三秒即可开机。"
            )
            match_calls: list[dict] = []

            async def fake_match_faq(*_args, **_kwargs):
                match_calls.append(_kwargs)
                return faq_entry

            async def fake_search(*_args, **_kwargs):
                raise AssertionError("FAQ 命中后不应再检索")

            settings = Settings(
                _env_file=None,
                auth_admin_token="admin-test-token",
                faq_match_threshold=0.85,
            )
            monkeypatch.setattr(answer_module, "get_settings", lambda: settings)
            monkeypatch.setattr(answer_module, "match_faq", fake_match_faq)
            monkeypatch.setattr(answer_module, "search_knowledge_base", fake_search)

            payload = KnowledgeAnswerRequest(
                query="怎么开机？", top_k=5, retrieval_mode="hybrid"
            )
            async with factory() as session:
                async with session.begin():
                    response = await answer_module.ask_knowledge_base(
                        knowledge_base_id=knowledge_base.id,
                        company_id=company_id,
                        payload=payload,
                        session=session,
                    )
                    assert response.faq_matched is True
                    assert response.answer == "长按电源键三秒即可开机。"
                    assert response.sources == []
                    # 产品/型号过滤透传给 FAQ 匹配。
                    assert match_calls and match_calls[0]["product_name"] is None
                    assert match_calls[0]["model_name"] is None

            from knowledge_service.infrastructure.persistence.models import QARecord

            async with factory() as session:
                record = await session.scalar(select(QARecord))
                assert record is not None
                assert record.status == "success"
                assert record.answer == "长按电源键三秒即可开机。"
                # FAQ 直出需要落库标记，供问答记录页统计与筛选。
                assert record.faq_matched is True
        finally:
            await engine.dispose()

    asyncio.run(scenario())
