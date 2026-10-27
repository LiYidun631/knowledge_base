"""验证产品元数据的解析、检索过滤、上传登记与选项聚合。"""

import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.api.v1 import documents as document_api
from knowledge_service.api.v1.documents import _normalize_product_lists
from knowledge_service.application.documents import service as document_service
from knowledge_service.application.search import service as search_service
from knowledge_service.application.search.bm25 import BM25Hit
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    CompanyCredential,
    Document,
    DocumentChunk,
    DocumentVersion,
    KnowledgeBase,
    ModelProfile,
    VectorLibrary,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def test_document_product_labels_merges_legacy_and_list_shapes() -> None:
    """旧版单值字段与新版列表字段都能解析，且去重去空。"""
    legacy = {"product_name": "体温计", "model_name": "额式体温计"}
    assert search_service.document_product_labels(legacy) == (["体温计"], ["额式体温计"])

    lists = {
        "product_names": ["体温计", "血氧仪", "体温计"],
        "model_names": ["额式体温计", "  ", "指夹血氧仪"],
    }
    products, models = search_service.document_product_labels(lists)
    assert products == ["体温计", "血氧仪"]
    assert models == ["额式体温计", "指夹血氧仪"]

    mixed = {
        "product_name": "体温计",
        "product_names": ["体温计", "血氧仪"],
        "model_name": "额式体温计",
        "model_names": ["耳式体温计"],
    }
    products, models = search_service.document_product_labels(mixed)
    assert products == ["体温计", "血氧仪"]
    assert models == ["耳式体温计", "额式体温计"]

    assert search_service.document_product_labels(None) == ([], [])
    assert search_service.document_product_labels({}) == ([], [])


def test_normalize_product_lists_merges_params_and_rejects_oversize() -> None:
    """上传参数合并旧单值与新列表，超长与空白被拦截或清理。"""
    products, models = _normalize_product_lists(
        "体温计", None, ["体温计", "血氧仪"], ["额式体温计", "额式体温计"]
    )
    assert products == ["体温计", "血氧仪"]
    assert models == ["额式体温计"]

    assert _normalize_product_lists("  ", "  ", None, None) == ([], [])
    assert _normalize_product_lists(None, "额式", [], ["  "]) == ([], ["额式"])

    with pytest.raises(Exception) as error:
        _normalize_product_lists(None, None, ["超" * 201], None)
    assert getattr(error.value, "status_code", None) == 422


@asynccontextmanager
async def _search_fixture(tmp_path):
    """构造带三个文档的知识库：列表元数据、旧版单值元数据、无产品信息。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'product-filter.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            embedding = ModelProfile(
                name="filter-embedding",
                kind="embedding",
                provider="openai_compatible",
                model_name="filter-embedding",
                dimensions=2,
                is_enabled=True,
            )
            session.add(embedding)
            await session.flush()
            kb = KnowledgeBase(
                name="产品过滤测试库",
                embedding_model_id=embedding.id,
                models_locked_at=utc_now(),
            )
            session.add(kb)
            await session.flush()
            metadata_cases = {
                "list": {
                    "product_names": ["体温计"],
                    "model_names": ["额式体温计", "耳式体温计"],
                },
                "legacy": {"product_name": "血氧仪", "model_name": "指夹血氧仪"},
                "none": {},
            }
            candidates: dict[str, tuple[UUID, UUID]] = {}
            for name, metadata in metadata_cases.items():
                document = Document(
                    knowledge_base_id=kb.id,
                    document_key=name,
                    filename=f"{name}.txt",
                    latest_version_number=1,
                    status="completed",
                    extra_metadata=metadata,
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
                    is_current=True,
                )
                session.add(version)
                await session.flush()
                parent = DocumentChunk(
                    document_id=document.id,
                    document_version_id=version.id,
                    ordinal=1,
                    level="parent",
                    content=f"{name} 说明书内容",
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
                    content=f"{name} 使用方法",
                    content_sha256="c" * 64,
                    token_count=3,
                )
                session.add(child)
                await session.flush()
                candidates[name] = (document.id, child.id)
            await session.commit()
            yield factory, kb.id, candidates
    finally:
        await engine.dispose()


def test_metadata_matches_product_treats_unlabeled_as_general() -> None:
    """完全未标注产品的文档视为通用资料，任何过滤下都命中；已标注文档必须含过滤标签。"""
    matches = search_service._metadata_matches_product

    assert matches({}, product_name="体温计", model_name="额式体温计") is True
    assert matches(None, product_name="体温计", model_name=None) is True

    labeled = {"product_names": ["体温计"], "model_names": ["额式体温计"]}
    assert matches(labeled, product_name="体温计", model_name="额式体温计") is True
    assert matches(labeled, product_name="体温计", model_name=None) is True
    assert matches(labeled, product_name=None, model_name="额式体温计") is True
    assert matches(labeled, product_name="血氧仪", model_name=None) is False
    assert matches(labeled, product_name="体温计", model_name="耳式体温计") is False


def test_search_product_filter_restricts_to_matching_documents(tmp_path, monkeypatch) -> None:
    """产品过滤返回匹配文档与未标注的通用文档；已标注不匹配的文档被排除。"""

    async def scenario() -> None:
        async with _search_fixture(tmp_path) as (factory, kb_id, candidates):
            document_ids = {name: ids[0] for name, ids in candidates.items()}
            bm25_calls: list[list] = []

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
                bm25_calls.append(list(document_ids or []))
                allowed = set(document_ids) if document_ids else None
                return [
                    BM25Hit(candidates[name][1], float(index + 1))
                    for index, name in enumerate(candidates)
                    if allowed is None or candidates[name][0] in allowed
                ]

            monkeypatch.setattr(search_service, "get_settings", lambda: Settings())
            monkeypatch.setattr(search_service, "search_bm25", lexical)

            async def search(**kwargs):
                async with factory() as session:
                    return await search_service.search_knowledge_base(
                        session,
                        knowledge_base_id=kb_id,
                        query="使用说明",
                        top_k=10,
                        **kwargs,
                    )

            results = await search(
                document_ids=None, retrieval_mode="bm25"
            )
            assert {item.filename for item in results} == {
                "list.txt", "legacy.txt", "none.txt"
            }

            # 未标注的 none.txt 是通用资料，任何产品过滤下都命中。
            results = await search(
                document_ids=None, retrieval_mode="bm25", product_name="体温计"
            )
            assert [item.filename for item in results] == ["list.txt", "none.txt"]
            assert results[0].model_names == ["额式体温计", "耳式体温计"]
            assert results[0].model_name == "额式体温计"

            results = await search(
                document_ids=None, retrieval_mode="bm25", model_name="指夹血氧仪"
            )
            assert [item.filename for item in results] == ["legacy.txt", "none.txt"]

            results = await search(
                document_ids=None,
                retrieval_mode="bm25",
                product_name="体温计",
                model_name="耳式体温计",
            )
            assert [item.filename for item in results] == ["list.txt", "none.txt"]

            # 已标注文档全部不匹配时，通用文档仍被召回。
            before = len(bm25_calls)
            results = await search(
                document_ids=None, retrieval_mode="bm25", product_name="心电图机"
            )
            assert [item.filename for item in results] == ["none.txt"]
            assert len(bm25_calls) == before + 1

            # 显式指定文档集与过滤求交后为空时短路，不触发召回。
            before = len(bm25_calls)
            results = await search(
                document_ids=[document_ids["legacy"]],
                retrieval_mode="bm25",
                product_name="体温计",
            )
            assert results == []
            assert len(bm25_calls) == before

            results = await search(
                document_ids=[document_ids["list"], document_ids["none"]],
                retrieval_mode="bm25",
                product_name="体温计",
            )
            assert [item.filename for item in results] == ["list.txt", "none.txt"]

            results = await search(
                document_ids=[document_ids["none"]],
                retrieval_mode="bm25",
                product_name="体温计",
            )
            assert [item.filename for item in results] == ["none.txt"]

    asyncio.run(scenario())


def test_register_document_stores_product_metadata(tmp_path) -> None:
    """登记文档时产品列表写入 extra_metadata，空值字段被剔除。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'register.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                embedding = ModelProfile(
                    name="register-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="register-embedding",
                    dimensions=2,
                )
                session.add(embedding)
                await session.flush()
                kb = KnowledgeBase(
                    name="登记测试库",
                    embedding_model_id=embedding.id,
                    models_locked_at=utc_now(),
                )
                session.add(kb)
                await session.flush()
                document = await document_service.register_document(
                    session,
                    knowledge_base_id=kb.id,
                    company_id=None,
                    document_key="manual.txt",
                    filename="manual.txt",
                    media_type="text/plain",
                    size_bytes=10,
                    content_sha256="d" * 64,
                    object_key="manual.txt",
                    source_metadata={
                        "product_name": "体温计",
                        "model_name": "额式体温计",
                        "product_names": ["体温计", "血氧仪"],
                        "model_names": ["额式体温计", "耳式体温计"],
                        "ignored_none": None,
                    },
                )
                await session.commit()
                stored = document.extra_metadata
                assert stored["product_name"] == "体温计"
                assert stored["model_name"] == "额式体温计"
                assert stored["product_names"] == ["体温计", "血氧仪"]
                assert stored["model_names"] == ["额式体温计", "耳式体温计"]
                assert "ignored_none" not in stored
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@asynccontextmanager
async def _options_fixture(tmp_path):
    """构造带公司/大库/子库和三个带产品元数据文档的知识库。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'options.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            embedding = ModelProfile(
                name="options-embedding",
                kind="embedding",
                provider="openai_compatible",
                model_name="options-embedding",
                dimensions=2,
                is_default=True,
            )
            company = Company(code="options-company", name="选项公司")
            session.add_all([embedding, company])
            await session.flush()
            library = VectorLibrary(
                company_id=company.id,
                name="选项大库",
                embedding_model_id=embedding.id,
            )
            session.add(library)
            await session.flush()
            kb = KnowledgeBase(
                company_id=company.id,
                vector_library_id=library.id,
                name="选项子库",
                embedding_model_id=embedding.id,
            )
            session.add(kb)
            await session.flush()
            for index, metadata in enumerate(
                (
                    {"product_names": ["体温计"], "model_names": ["额式体温计", "耳式体温计"]},
                    {"product_name": "血氧仪", "model_name": "指夹血氧仪"},
                    {},
                )
            ):
                session.add(
                    Document(
                        knowledge_base_id=kb.id,
                        document_key=f"doc-{index}",
                        filename=f"doc-{index}.txt",
                        status="completed",
                        extra_metadata=metadata,
                    )
                )
            await session.commit()
            yield factory, kb.id, company.id
    finally:
        await engine.dispose()


def _catalog_app(monkeypatch, settings, factory):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    async def db_session():
        async with factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db_session] = db_session
    return app


def test_http_product_options_derive_from_documents(monkeypatch, tmp_path) -> None:
    """产品选项按库内文档标注推导，与全局产品目录无关。"""

    async def scenario() -> None:
        async with _options_fixture(tmp_path) as (factory, kb_id, _company_id):
            settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
            app = _catalog_app(monkeypatch, settings, factory)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test/api/v1/",
                headers={"Authorization": "Bearer admin-test-token"},
            ) as client:
                response = await client.get(f"knowledge-bases/{kb_id}/product-options")
                assert response.status_code == 200, response.text
                assert response.json() == {
                    "product_names": ["体温计", "血氧仪"],
                    "model_names": ["指夹血氧仪", "耳式体温计", "额式体温计"],
                    "entries": [
                        {"product_name": "体温计", "model_name": "耳式体温计"},
                        {"product_name": "体温计", "model_name": "额式体温计"},
                        {"product_name": "血氧仪", "model_name": "指夹血氧仪"},
                    ],
                }

                # 全局目录的新增条目不影响问答选项（库内文档没标注就不出现）。
                for product_name, model_name in (
                    ("体温计", "耳式体温计"),
                    ("血压计", "臂式血压计"),
                ):
                    response = await client.post(
                        "product-catalog",
                        json={"product_name": product_name, "model_name": model_name},
                    )
                    assert response.status_code == 201, response.text
                response = await client.get(f"knowledge-bases/{kb_id}/product-options")
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["product_names"] == ["体温计", "血氧仪"]
                assert body["entries"] == [
                    {"product_name": "体温计", "model_name": "耳式体温计"},
                    {"product_name": "体温计", "model_name": "额式体温计"},
                    {"product_name": "血氧仪", "model_name": "指夹血氧仪"},
                ]

                response = await client.get(f"knowledge-bases/{uuid4()}/product-options")
                assert response.status_code == 200
                assert response.json() == {
                    "product_names": [],
                    "model_names": [],
                    "entries": [],
                }

    asyncio.run(scenario())


def test_http_product_catalog_crud_batch_delete_and_upload_validation(
    monkeypatch, tmp_path
) -> None:
    """全局目录 CRUD、重复 409、搜索过滤、批量删除、上传校验与鉴权边界。"""

    class Storage:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        async def put(self, key: str, content: bytes) -> None:
            self.objects[key] = content

        async def delete(self, key: str) -> None:
            self.objects.pop(key, None)

    async def scenario() -> None:
        async with _options_fixture(tmp_path) as (factory, kb_id, company_id):
            storage = Storage()
            monkeypatch.setattr(
                document_api, "get_object_storage", lambda settings: storage
            )
            async with factory() as session:
                session.add(
                    CompanyCredential(
                        company_id=company_id,
                        name="默认凭据",
                        token_hash=sha256(b"owner-company-token").hexdigest(),
                    )
                )
                await session.commit()
            settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
            app = _catalog_app(monkeypatch, settings, factory)
            admin = {"Authorization": "Bearer admin-test-token"}
            owner = {"Authorization": "Bearer owner-company-token"}
            base = "product-catalog"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test/api/v1/",
            ) as client:
                # 公司 Token 不可管理目录，但可以读产品选项。
                response = await client.post(
                    base, headers=owner, json={"product_name": "体温计", "model_name": "额式体温计"}
                )
                assert response.status_code == 403
                response = await client.get(
                    f"knowledge-bases/{kb_id}/product-options", headers=owner
                )
                assert response.status_code == 200, response.text

                for product_name, model_name in (
                    ("体温计", "额式体温计"),
                    ("体温计", "耳式体温计"),
                    ("血压计", "臂式血压计"),
                ):
                    response = await client.post(
                        base,
                        headers=admin,
                        json={"product_name": product_name, "model_name": model_name},
                    )
                    assert response.status_code == 201, response.text

                response = await client.post(
                    base, headers=admin, json={"product_name": "体温计", "model_name": "额式体温计"}
                )
                assert response.status_code == 409

                # 列表按产品名称、型号排序；关键词与产品名称过滤生效。
                response = await client.get(base, headers=admin)
                assert response.status_code == 200, response.text
                entries = response.json()
                assert [(e["product_name"], e["model_name"]) for e in entries] == [
                    ("体温计", "耳式体温计"),
                    ("体温计", "额式体温计"),
                    ("血压计", "臂式血压计"),
                ]
                response = await client.get(base, headers=admin, params={"q": "式体温"})
                assert [(e["product_name"], e["model_name"]) for e in response.json()] == [
                    ("体温计", "耳式体温计"),
                    ("体温计", "额式体温计"),
                ]
                response = await client.get(
                    base, headers=admin, params={"product_name": "血压计"}
                )
                assert [(e["product_name"], e["model_name"]) for e in response.json()] == [
                    ("血压计", "臂式血压计")
                ]
                response = await client.get(base, headers=admin, params={"q": "指夹"})
                assert response.json() == []

                ear = next(e for e in entries if e["model_name"] == "耳式体温计")
                arm = next(e for e in entries if e["product_name"] == "血压计")
                forehead = next(e for e in entries if e["model_name"] == "额式体温计")

                # 批量删除按请求顺序返回；不存在的 ID 使整个请求 404。
                response = await client.post(
                    f"{base}/batch-delete",
                    headers=admin,
                    json={"catalog_ids": [ear["id"], arm["id"]]},
                )
                assert response.status_code == 200, response.text
                assert response.json() == {
                    "deleted_count": 2,
                    "deleted_ids": [ear["id"], arm["id"]],
                }
                response = await client.post(
                    f"{base}/batch-delete",
                    headers=admin,
                    json={"catalog_ids": [ear["id"], str(uuid4())]},
                )
                assert response.status_code == 404
                assert response.json()["detail"]["catalog_ids"]

                # 单条删除；重复删除返回 404。
                response = await client.delete(f"{base}/{forehead['id']}", headers=admin)
                assert response.status_code == 204
                response = await client.delete(f"{base}/{forehead['id']}", headers=admin)
                assert response.status_code == 404
                response = await client.get(base, headers=admin)
                assert response.json() == []

                # 上传校验：未登记的产品/型号被拒绝，登记过的可通过。
                for params in (
                    {"product_names": ["心电图机"]},
                    {"product_names": ["体温计"], "model_names": ["不存在型号"]},
                ):
                    response = await client.post(
                        f"knowledge-bases/{kb_id}/documents",
                        headers={**admin, "Content-Type": "application/pdf"},
                        params={"company_id": str(company_id), "filename": "manual.pdf", **params},
                        content=b"fake pdf content",
                    )
                    assert response.status_code == 422, response.text
                # 目录已被清空，任何标签上传都应被拒绝。
                response = await client.post(
                    f"knowledge-bases/{kb_id}/documents",
                    headers={**admin, "Content-Type": "application/pdf"},
                    params={
                        "company_id": str(company_id),
                        "filename": "manual.pdf",
                        "product_names": ["体温计"],
                        "model_names": ["额式体温计"],
                    },
                    content=b"fake pdf content",
                )
                assert response.status_code == 422, response.text
                assert not storage.objects

                # 重新登记后上传通过。
                response = await client.post(
                    base,
                    headers=admin,
                    json={"product_name": "体温计", "model_name": "额式体温计"},
                )
                assert response.status_code == 201, response.text
                response = await client.post(
                    f"knowledge-bases/{kb_id}/documents",
                    headers={**admin, "Content-Type": "application/pdf"},
                    params={
                        "company_id": str(company_id),
                        "filename": "manual.pdf",
                        "product_names": ["体温计"],
                        "model_names": ["额式体温计"],
                    },
                    content=b"fake pdf content",
                )
                assert response.status_code == 202, response.text
                assert storage.objects

    asyncio.run(scenario())


def test_http_product_catalog_update_renames_and_cascades(monkeypatch, tmp_path) -> None:
    """目录改名同步更新同名/同型号条目与文档元数据；冲突 409、无变化幂等。"""

    async def scenario() -> None:
        async with _options_fixture(tmp_path) as (factory, kb_id, company_id):
            async with factory() as session:
                session.add(
                    CompanyCredential(
                        company_id=company_id,
                        name="默认凭据",
                        token_hash=sha256(b"owner-company-token").hexdigest(),
                    )
                )
                await session.commit()
            settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
            app = _catalog_app(monkeypatch, settings, factory)
            admin = {"Authorization": "Bearer admin-test-token"}
            owner = {"Authorization": "Bearer owner-company-token"}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test/api/v1/",
            ) as client:
                created: dict[tuple[str, str], str] = {}
                for product_name, model_name in (
                    ("体温计", "额式体温计"),
                    ("体温计", "耳式体温计"),
                    ("血氧仪", "指夹血氧仪"),
                ):
                    response = await client.post(
                        "product-catalog",
                        headers=admin,
                        json={"product_name": product_name, "model_name": model_name},
                    )
                    assert response.status_code == 201, response.text
                    created[(product_name, model_name)] = response.json()["id"]

                entry_id = created[("体温计", "额式体温计")]

                # 公司 Token 不可修改目录。
                response = await client.put(
                    f"product-catalog/{entry_id}",
                    headers=owner,
                    json={"product_name": "红外体温计", "model_name": "额式体温计"},
                )
                assert response.status_code == 403

                # 产品名改名：目录中同名行与文档元数据一起改，旧名称全部消失。
                response = await client.put(
                    f"product-catalog/{entry_id}",
                    headers=admin,
                    json={"product_name": "红外体温计", "model_name": "额式体温计"},
                )
                assert response.status_code == 200, response.text
                assert response.json()["product_name"] == "红外体温计"

                entries = (await client.get("product-catalog", headers=admin)).json()
                assert {(e["product_name"], e["model_name"]) for e in entries} == {
                    ("红外体温计", "额式体温计"),
                    ("红外体温计", "耳式体温计"),
                    ("血氧仪", "指夹血氧仪"),
                }

                async with factory() as session:
                    labeled = await session.scalar(
                        select(Document.extra_metadata).where(
                            Document.filename == "doc-0.txt"
                        )
                    )
                    legacy = await session.scalar(
                        select(Document.extra_metadata).where(
                            Document.filename == "doc-1.txt"
                        )
                    )
                    unlabeled = await session.scalar(
                        select(Document.extra_metadata).where(
                            Document.filename == "doc-2.txt"
                        )
                    )
                assert labeled == {
                    "product_names": ["红外体温计"],
                    "model_names": ["额式体温计", "耳式体温计"],
                }
                assert legacy == {"product_name": "血氧仪", "model_name": "指夹血氧仪"}
                assert unlabeled == {}

                options = (
                    await client.get(
                        f"knowledge-bases/{kb_id}/product-options", headers=owner
                    )
                ).json()
                assert options["product_names"] == ["红外体温计", "血氧仪"]

                # 型号改名：只影响该型号的条目与文档。
                response = await client.put(
                    f"product-catalog/{entry_id}",
                    headers=admin,
                    json={"product_name": "红外体温计", "model_name": "枪式体温计"},
                )
                assert response.status_code == 200, response.text
                entries = (await client.get("product-catalog", headers=admin)).json()
                assert {(e["product_name"], e["model_name"]) for e in entries} == {
                    ("红外体温计", "枪式体温计"),
                    ("红外体温计", "耳式体温计"),
                    ("血氧仪", "指夹血氧仪"),
                }
                async with factory() as session:
                    labeled = await session.scalar(
                        select(Document.extra_metadata).where(
                            Document.filename == "doc-0.txt"
                        )
                    )
                assert labeled["model_names"] == ["枪式体温计", "耳式体温计"]

                # 改名后与现有条目冲突 → 409。
                other_id = created[("体温计", "耳式体温计")]
                response = await client.put(
                    f"product-catalog/{other_id}",
                    headers=admin,
                    json={"product_name": "红外体温计", "model_name": "枪式体温计"},
                )
                assert response.status_code == 409, response.text

                # 无变化提交幂等，不产生副作用。
                response = await client.put(
                    f"product-catalog/{entry_id}",
                    headers=admin,
                    json={"product_name": "红外体温计", "model_name": "枪式体温计"},
                )
                assert response.status_code == 200, response.text

                response = await client.put(
                    f"product-catalog/{uuid4()}",
                    headers=admin,
                    json={"product_name": "体温计", "model_name": "额式体温计"},
                )
                assert response.status_code == 404

    asyncio.run(scenario())


def test_http_search_accepts_product_filter_and_short_circuits(monkeypatch, tmp_path) -> None:
    """检索接口接受产品过滤参数；无可用版本时返回空结果且不触达模型。"""

    async def scenario() -> None:
        async with _options_fixture(tmp_path) as (factory, kb_id, company_id):
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
                    f"knowledge-bases/{kb_id}/search",
                    params={"company_id": str(company_id)},
                    json={"query": "使用说明", "product_name": "心电图机"},
                )
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["results"] == []

                response = await client.post(
                    f"knowledge-bases/{kb_id}/search",
                    params={"company_id": str(company_id)},
                    json={"query": "使用说明", "model_name": "   "},
                )
                assert response.status_code == 200, response.text

    asyncio.run(scenario())
