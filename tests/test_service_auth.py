import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from knowledge_service.api.v1 import documents as document_api
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    CompanyCredential,
    Document,
    DocumentAsset,
    DocumentVersion,
    IngestionJob,
    KnowledgeBase,
    ModelProfile,
    ProductCatalog,
    QARecord,
    VectorLibrary,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def _app(monkeypatch, settings):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_unconfigured_service_fails_closed_before_database(monkeypatch):
    async def scenario():
        app = _app(monkeypatch, Settings(_env_file=None))

        async def forbidden_database():
            raise AssertionError("Unauthorized requests must not open the database")
            yield

        app.dependency_overrides[get_db_session] = forbidden_database
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            assert (await client.get("/api/v1/health")).status_code == 200
            response = await client.get("/api/v1/knowledge-bases")
            assert response.status_code == 503

    asyncio.run(scenario())


@pytest.mark.parametrize("authorization", [None, "Basic wrong", "Bearer"])
def test_missing_or_invalid_token_is_rejected_before_database(monkeypatch, authorization):
    async def scenario():
        app = _app(monkeypatch, Settings(_env_file=None, auth_admin_token="admin-test-token"))

        async def forbidden_database():
            raise AssertionError("Unauthorized requests must not open the database")
            yield

        app.dependency_overrides[get_db_session] = forbidden_database
        headers = {"Authorization": authorization} if authorization else {}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/v1/knowledge-bases", headers=headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert "admin-test-token" not in response.text

    asyncio.run(scenario())


def test_openapi_requires_bearer_for_all_business_routes(monkeypatch):
    app = _app(monkeypatch, Settings(_env_file=None))
    schema = app.openapi()
    assert schema["components"]["securitySchemes"]["KnowledgeServiceBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }
    for path, methods in schema["paths"].items():
        for operation in methods.values():
            if path.endswith("/health"):
                assert not operation.get("security")
            else:
                assert operation["security"] == [{"KnowledgeServiceBearer": []}]


@pytest.mark.parametrize("admin_token", ["contains space", "line\nbreak", "中文密钥"])
def test_invalid_admin_credentials_are_rejected(admin_token):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, auth_admin_token=admin_token)


def test_company_token_mapping_is_no_longer_a_setting():
    assert "auth_company_tokens" not in Settings.model_fields


@asynccontextmanager
async def _company_service(monkeypatch, tmp_path, *, raise_app_exceptions=True):
    """用独立 SQLite 数据库验证真实查询，不连接已有业务数据。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            embedding = ModelProfile(
                name="auth-embedding",
                kind="embedding",
                provider="openai_compatible",
                model_name="test",
                dimensions=2,
            )
            session.add(embedding)
            await session.flush()
            scopes = []
            for name in ("owner", "other"):
                company = Company(code=name, name=name)
                session.add(company)
                await session.flush()
                session.add(
                    CompanyCredential(
                        company_id=company.id,
                        name="默认凭据",
                        token_hash=sha256(f"{name}-test-token".encode()).hexdigest(),
                    )
                )
                library = VectorLibrary(
                    company_id=company.id,
                    name=name,
                    embedding_model_id=embedding.id,
                )
                session.add(library)
                await session.flush()
                kb = KnowledgeBase(
                    company_id=company.id,
                    vector_library_id=library.id,
                    name=name,
                    embedding_model_id=embedding.id,
                )
                session.add(kb)
                await session.flush()
                document = Document(
                    company_id=company.id,
                    knowledge_base_id=kb.id,
                    document_key="manual",
                    filename=f"{name}.txt",
                    latest_version_number=1,
                )
                session.add(document)
                await session.flush()
                version = DocumentVersion(
                    document_id=document.id,
                    version_number=1,
                    filename=document.filename,
                    size_bytes=1,
                    content_sha256="0" * 64,
                    object_key=f"{name}.txt",
                )
                session.add(version)
                await session.flush()
                asset = DocumentAsset(
                    document_id=document.id,
                    document_version_id=version.id,
                    object_key=f"{name}.png",
                    media_type="image/png",
                )
                record = QARecord(
                    company_id=company.id,
                    knowledge_base_id=kb.id,
                    request_id=name,
                    query="资料",
                    retrieval_mode="hybrid",
                    top_k=5,
                )
                job = IngestionJob(document_id=document.id, document_version_id=version.id)
                session.add_all([asset, record, job])
                await session.flush()
                scopes.append(
                    {
                        "company": str(company.id),
                        "kb": str(kb.id),
                        "document": str(document.id),
                        "asset": str(asset.id),
                        "record": str(record.id),
                    }
                )
            await session.commit()

        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _app(monkeypatch, settings)

        async def db_session():
            async with factory() as session:
                yield session
                await session.commit()

        app.dependency_overrides[get_db_session] = db_session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
            base_url="http://test/api/v1/",
            headers={"Authorization": "Bearer owner-test-token"},
        ) as client:
            yield client, scopes, factory, settings
    finally:
        await engine.dispose()


def test_company_token_can_read_own_resources_and_search(monkeypatch, tmp_path):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path) as (client, scopes, _, _settings):
            owner = scopes[0]
            response = await client.get("knowledge-bases", params={"company_id": owner["company"]})
            assert response.status_code == 200
            assert [item["id"] for item in response.json()] == [owner["kb"]]
            for path in (
                f"knowledge-bases/{owner['kb']}",
                f"knowledge-bases/{owner['kb']}/documents",
                f"documents/{owner['document']}",
                f"documents/{owner['document']}/versions",
                f"documents/{owner['document']}/ingestion",
                f"knowledge-bases/{owner['kb']}/qa-records?company_id={owner['company']}",
                f"qa-records/{owner['record']}?company_id={owner['company']}",
            ):
                response = await client.get(path)
                assert response.status_code == 200, (path, response.text)
            response = await client.post(
                f"knowledge-bases/{owner['kb']}/search",
                params={"company_id": owner["company"]},
                json={"query": "使用说明"},
            )
            assert response.status_code == 200
            assert response.json()["company_id"] == owner["company"]
            assert response.json()["knowledge_base_id"] == owner["kb"]
            assert response.json()["results"] == []

            class Storage:
                async def get(self, key):
                    assert key == "owner.png"
                    return b"image-content"

            monkeypatch.setattr(document_api, "get_object_storage", lambda _settings: Storage())
            response = await client.get(f"document-assets/{owner['asset']}")
            assert response.status_code == 200
            assert response.content == b"image-content"
            response = await client.patch(
                f"knowledge-bases/{owner['kb']}", json={"name": "更新名称"}
            )
            assert response.status_code == 200
            assert response.json()["name"] == "更新名称"
            assert (await client.get("knowledge-bases")).status_code == 422

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "knowledge-bases/{kb}"),
        ("PATCH", "knowledge-bases/{kb}"),
        ("DELETE", "knowledge-bases/{kb}"),
        ("GET", "knowledge-bases/{kb}/documents"),
        ("POST", "knowledge-bases/{kb}/documents"),
        ("POST", "knowledge-bases/{kb}/documents/from-url"),
        ("POST", "knowledge-bases/{kb}/search"),
        ("POST", "knowledge-bases/{kb}/ask"),
        ("POST", "knowledge-bases/{kb}/ask/stream"),
        ("GET", "knowledge-bases/{kb}/qa-records"),
        ("DELETE", "knowledge-bases/{kb}/file-vectors/manual"),
        ("POST", "knowledge-bases/{kb}/file-vectors/batch-delete"),
        ("GET", "documents/{document}"),
        ("GET", "documents/{document}/versions"),
        ("GET", "documents/{document}/ingestion"),
        ("POST", "documents/{document}/retry"),
        ("DELETE", "documents/{document}"),
        ("GET", "document-assets/{asset}"),
        ("GET", "qa-records/{record}"),
    ],
)
def test_company_token_cannot_access_foreign_resources(monkeypatch, tmp_path, method, path):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path) as (client, scopes, _, _settings):
            # 即使请求声明的是本公司，也必须检查资源 ID 的真实归属。
            response = await client.request(
                method,
                path.format(**scopes[1]),
                params={"company_id": scopes[0]["company"]},
            )
            assert response.status_code == 404
            assert response.json() == {"detail": "资源不存在"}

    asyncio.run(scenario())


def test_company_token_can_read_global_product_catalog(monkeypatch, tmp_path):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path) as (client, _scopes, factory, _):
            async with factory() as session:
                first = ProductCatalog(product_name="体温计", model_name="额式体温计")
                session.add_all(
                    [first, ProductCatalog(product_name="血压计", model_name="臂式血压计")]
                )
                await session.commit()
                first_id = first.id

            # 公司 Token 可读全局目录，作为外部问答客户端的标签对齐来源。
            response = await client.get("product-catalog")
            assert response.status_code == 200
            assert [item["product_name"] for item in response.json()] == [
                "体温计",
                "血压计",
            ]
            response = await client.get("product-catalog", params={"q": "额式"})
            assert [item["model_name"] for item in response.json()] == ["额式体温计"]

            # 目录维护仍仅限管理员，公司 Token 不得增删改。
            assert (
                await client.post(
                    "product-catalog",
                    json={"product_name": "血氧仪", "model_name": "指夹式"},
                )
            ).status_code == 403
            assert (
                await client.put(
                    f"product-catalog/{first_id}",
                    json={"product_name": "体温枪", "model_name": "额式体温计"},
                )
            ).status_code == 403
            assert (
                await client.post(
                    "product-catalog/batch-delete", json={"catalog_ids": [str(first_id)]}
                )
            ).status_code == 403
            assert (await client.delete(f"product-catalog/{first_id}")).status_code == 403

    asyncio.run(scenario())


def test_company_token_cannot_spoof_company_or_admin_permissions(monkeypatch, tmp_path):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path) as (client, scopes, _, _settings):
            owner, other = scopes
            for method, path in (
                ("GET", f"knowledge-bases?company_id={other['company']}"),
                ("POST", f"companies/{other['company']}/vector-library"),
                ("POST", f"companies/{other['company']}/vector-library/knowledge-bases"),
                ("DELETE", f"companies/{other['company']}/vector-library"),
                (
                    "DELETE",
                    f"companies/{other['company']}/vector-library/knowledge-bases/{other['kb']}",
                ),
                (
                    "POST",
                    f"companies/{other['company']}/vector-library/knowledge-bases/batch-delete",
                ),
                ("POST", "companies"),
                ("POST", f"companies?company_id={owner['company']}"),
                ("POST", f"knowledge-bases?company_id={owner['company']}"),
                (
                    "GET",
                    f"knowledge-bases?company_id={owner['company']}&company_id={other['company']}",
                ),
            ):
                response = await client.request(method, path)
                assert response.status_code == 403, (path, response.text)
            for path in (
                "knowledge-bases/not-a-uuid",
                "knowledge-bases?company_id=invalid",
            ):
                assert (await client.get(path)).status_code == 422

    asyncio.run(scenario())


def test_admin_can_create_company_and_access_other_company(monkeypatch, tmp_path):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path) as (client, scopes, _, _settings):
            client.headers["Authorization"] = "Bearer admin-test-token"
            response = await client.post("companies", json={"name": "新公司"})
            assert response.status_code == 201
            response = await client.get(f"documents/{scopes[1]['document']}")
            assert response.status_code == 200
            response = await client.get(
                "knowledge-bases", params={"company_id": scopes[1]["company"]}
            )
            assert [item["id"] for item in response.json()] == [scopes[1]["kb"]]
            response = await client.get("knowledge-bases", params={"company_id": str(uuid4())})
            assert response.status_code == 200
            assert response.json() == []
            assert (await client.get("knowledge-bases")).status_code == 422

    asyncio.run(scenario())


def test_company_creation_and_batch_delete_keep_company_scope(monkeypatch, tmp_path):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path) as (client, scopes, factory, _settings):
            owner, other = scopes
            response = await client.post(
                f"companies/{owner['company']}/vector-library/knowledge-bases",
                json={"name": "新增子库"},
            )
            assert response.status_code == 201
            assert response.json()["company_id"] == owner["company"]
            # 混入外公司子库时，整批拒绝，不能先删除本公司的合法子库。
            response = await client.post(
                f"companies/{owner['company']}/vector-library/knowledge-bases/batch-delete",
                json={"knowledge_base_ids": [owner["kb"], other["kb"]]},
            )
            assert response.status_code == 404
            async with factory() as session:
                assert await session.get(KnowledgeBase, UUID(owner["kb"])) is not None
                assert await session.get(KnowledgeBase, UUID(other["kb"])) is not None

    asyncio.run(scenario())


def test_archived_company_token_is_rejected(monkeypatch, tmp_path):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path) as (client, scopes, factory, _settings):
            async with factory() as session:
                company = await session.get(Company, UUID(scopes[0]["company"]))
                company.status = "archived"
                await session.commit()
            response = await client.get(f"documents/{scopes[0]['document']}")
            assert response.status_code == 403

    asyncio.run(scenario())


def test_unknown_company_token_is_rejected(monkeypatch, tmp_path):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path) as (client, scopes, _, _):
            response = await client.get(
                f"documents/{scopes[0]['document']}",
                headers={"Authorization": "Bearer unknown-token"},
            )
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert "unknown-token" not in response.text

    asyncio.run(scenario())


def test_admin_company_token_lifecycle(monkeypatch, tmp_path):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path) as (client, scopes, factory, _):
            admin = {"Authorization": "Bearer admin-test-token"}
            created = await client.post("companies", json={"name": "凭据测试"}, headers=admin)
            company_id = created.json()["id"]
            collection = f"companies/{company_id}/tokens"

            response = await client.post(collection, json={"name": "项目 A"}, headers=admin)
            assert response.status_code == 201
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["pragma"] == "no-cache"
            first = response.json()
            assert first["company_id"] == company_id
            assert first["name"] == "项目 A"
            assert first["token_type"] == "Bearer"
            assert first["token"].startswith("ks_") and len(first["token"]) >= 46
            first_id = first["company_credential_id"]
            async with factory() as session:
                credential = await session.get(CompanyCredential, UUID(first_id))
                assert credential.token_hash == sha256(first["token"].encode()).hexdigest()
                assert credential.is_enabled

            response = await client.post(collection, json={"name": "项目 B"}, headers=admin)
            assert response.status_code == 201
            second = response.json()
            second_id = second["company_credential_id"]
            assert second["token"] != first["token"]
            assert (
                await client.post(collection, json={"name": "项目 A"}, headers=admin)
            ).status_code == 409

            response = await client.get(collection, headers=admin)
            assert response.status_code == 200
            listed = response.json()
            assert {item["name"] for item in listed} == {"项目 A", "项目 B"}
            assert {item["id"] for item in listed} == {first_id, second_id}
            assert all("token" not in item and "token_hash" not in item for item in listed)

            async def access(token):
                return await client.get(
                    "knowledge-bases",
                    params={"company_id": company_id},
                    headers={"Authorization": f"Bearer {token}"},
                )

            assert (await access(first["token"])).status_code == 200
            assert (await access(second["token"])).status_code == 200

            response = await client.post(f"{collection}/{first_id}/reset", headers=admin)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            rotated = response.json()
            assert rotated["company_credential_id"] == first_id
            assert rotated["name"] == "项目 A"
            assert rotated["token"] != first["token"]
            assert (await access(first["token"])).status_code == 401
            assert (await access(rotated["token"])).status_code == 200
            # 重置一枚凭据不影响同公司其他项目。
            assert (await access(second["token"])).status_code == 200

            assert (
                await client.delete(f"{collection}/{second_id}", headers=admin)
            ).status_code == 204
            assert (
                await client.delete(f"{collection}/{second_id}", headers=admin)
            ).status_code == 204
            assert (await access(second["token"])).status_code == 401
            assert (await access(rotated["token"])).status_code == 200
            async with factory() as session:
                disabled = await session.get(CompanyCredential, UUID(second_id))
                assert not disabled.is_enabled
                assert disabled.token_hash == sha256(second["token"].encode()).hexdigest()

            # 停用的凭据可重置为新 Token 重新启用。
            response = await client.post(f"{collection}/{second_id}/reset", headers=admin)
            revived = response.json()
            assert revived["company_credential_id"] == second_id
            assert (await access(revived["token"])).status_code == 200
            assert (await access(second["token"])).status_code == 401

            # 跨公司凭据 ID 不能操作，即使管理员指定了另一家公司。
            other_company = scopes[1]["company"]
            assert (
                await client.post(
                    f"companies/{other_company}/tokens/{first_id}/reset", headers=admin
                )
            ).status_code == 404
            assert (
                await client.delete(
                    f"companies/{other_company}/tokens/{first_id}", headers=admin
                )
            ).status_code == 404
            # 管理员可读取其他公司的凭据列表，列表不泄露明文。
            response = await client.get(f"companies/{other_company}/tokens", headers=admin)
            assert response.status_code == 200
            assert [item["name"] for item in response.json()] == ["默认凭据"]
            assert "token" not in response.text

            # Token 只能访问所属公司资源，换到另一家公司会被拒绝。
            response = await client.get(
                f"documents/{scopes[0]['document']}",
                headers={"Authorization": f"Bearer {rotated['token']}"},
            )
            assert response.status_code == 404

    asyncio.run(scenario())


def test_company_credentials_share_company_scope(monkeypatch, tmp_path):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path) as (client, scopes, _, _):
            admin = {"Authorization": "Bearer admin-test-token"}
            owner = scopes[0]
            response = await client.post(
                f"companies/{owner['company']}/tokens",
                json={"name": "第二个项目"},
                headers=admin,
            )
            assert response.status_code == 201
            token = response.json()["token"]

            # 多凭据共享公司权限边界：同公司资源互通，跨公司仍隔离。
            response = await client.get(
                f"knowledge-bases/{owner['kb']}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            response = await client.get(
                f"documents/{scopes[1]['document']}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 404

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "method,suffix",
    [("GET", ""), ("POST", ""), ("POST", "/{credential}/reset"), ("DELETE", "/{credential}")],
)
def test_company_cannot_manage_tokens(monkeypatch, tmp_path, method, suffix):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path) as (client, scopes, factory, _):
            async with factory() as session:
                credential_id = await session.scalar(
                    select(CompanyCredential.id).where(
                        CompanyCredential.company_id == UUID(scopes[0]["company"])
                    )
                )
            for scope in scopes:
                path = (
                    f"companies/{scope['company']}/tokens"
                    + suffix.format(credential=credential_id)
                )
                response = await client.request(
                    method,
                    path,
                    json={"name": "越权项目"} if method == "POST" and not suffix else None,
                )
                assert response.status_code == 403
            async with factory() as session:
                credentials = list(await session.scalars(select(CompanyCredential)))
                assert len(credentials) == 2
                owner = await session.scalar(
                    select(CompanyCredential).where(
                        CompanyCredential.company_id == UUID(scopes[0]["company"])
                    )
                )
                assert owner.is_enabled
                assert owner.token_hash == sha256(b"owner-test-token").hexdigest()

    asyncio.run(scenario())


def test_token_management_rejects_missing_or_archived_company(monkeypatch, tmp_path):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path) as (client, scopes, factory, _):
            client.headers["Authorization"] = "Bearer admin-test-token"
            created = await client.post("companies", json={"name": "尚未签发"})
            company_id = created.json()["id"]
            collection = f"companies/{company_id}/tokens"
            assert (await client.get(collection)).json() == []
            assert (await client.post(f"{collection}/{uuid4()}/reset")).status_code == 404
            assert (await client.delete(f"{collection}/{uuid4()}")).status_code == 404

            missing_company = uuid4()
            assert (await client.get(f"companies/{missing_company}/tokens")).status_code == 404
            assert (
                await client.post(
                    f"companies/{missing_company}/tokens", json={"name": "不存在"}
                )
            ).status_code == 404
            assert (
                await client.post(f"companies/{missing_company}/tokens/{uuid4()}/reset")
            ).status_code == 404
            assert (
                await client.delete(f"companies/{missing_company}/tokens/{uuid4()}")
            ).status_code == 404

            async with factory() as session:
                company = await session.get(Company, UUID(scopes[0]["company"]))
                company.status = "archived"
                await session.commit()
                credential_id = await session.scalar(
                    select(CompanyCredential.id).where(
                        CompanyCredential.company_id == UUID(scopes[0]["company"])
                    )
                )
            collection = f"companies/{scopes[0]['company']}/tokens"
            assert (await client.get(collection)).status_code == 200
            assert (
                await client.post(collection, json={"name": "归档后签发"})
            ).status_code == 404
            assert (await client.post(f"{collection}/{credential_id}/reset")).status_code == 404
            # 归档后仍允许管理员撤销凭据，不能因公司状态阻断安全清理。
            assert (await client.delete(f"{collection}/{credential_id}")).status_code == 204

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["issue", "reset", "disable"])
def test_token_commit_failure_does_not_report_success(monkeypatch, tmp_path, operation):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path, raise_app_exceptions=False) as (
            client,
            scopes,
            factory,
            _,
        ):
            client.headers["Authorization"] = "Bearer admin-test-token"
            created = await client.post("companies", json={"name": "提交失败测试"})
            new_company_id = created.json()["id"]
            company_id = scopes[0]["company"]
            async with factory() as session:
                credential_id = await session.scalar(
                    select(CompanyCredential.id).where(
                        CompanyCredential.company_id == UUID(company_id)
                    )
                )

            async def fail_commit(session):
                raise RuntimeError("simulated commit failure")

            monkeypatch.setattr(AsyncSession, "commit", fail_commit)
            if operation == "issue":
                method = "POST"
                path = f"companies/{new_company_id}/tokens"
                kwargs = {"json": {"name": "项目 A"}}
            elif operation == "reset":
                method = "POST"
                path = f"companies/{company_id}/tokens/{credential_id}/reset"
                kwargs = {}
            else:
                method = "DELETE"
                path = f"companies/{company_id}/tokens/{credential_id}"
                kwargs = {}
            response = await client.request(method, path, **kwargs)
            # 即使清理会话时提交失败，也不能先发出成功响应或泄露新明文。
            assert response.status_code == 500
            assert "ks_" not in response.text
            async with factory() as session:
                if operation == "issue":
                    assert (
                        await session.scalar(
                            select(CompanyCredential.id).where(
                                CompanyCredential.company_id == UUID(new_company_id)
                            )
                        )
                    ) is None
                else:
                    credential = await session.get(CompanyCredential, credential_id)
                    assert credential.is_enabled
                    assert credential.token_hash == sha256(b"owner-test-token").hexdigest()

    asyncio.run(scenario())


def test_concurrent_issue_same_name_keeps_one_credential(monkeypatch, tmp_path):
    async def scenario():
        async with _company_service(monkeypatch, tmp_path) as (client, _, factory, _):
            client.headers["Authorization"] = "Bearer admin-test-token"
            created = await client.post("companies", json={"name": "并发签发"})
            company_id = created.json()["id"]
            collection = f"companies/{company_id}/tokens"
            responses = await asyncio.gather(
                client.post(collection, json={"name": "项目 A"}),
                client.post(collection, json={"name": "项目 A"}),
            )
            assert sorted(response.status_code for response in responses) == [201, 409]
            token = next(
                response.json()["token"]
                for response in responses
                if response.status_code == 201
            )
            async with factory() as session:
                credentials = list(
                    await session.scalars(
                        select(CompanyCredential).where(
                            CompanyCredential.company_id == UUID(company_id)
                        )
                    )
                )
                assert len(credentials) == 1
                assert credentials[0].token_hash == sha256(token.encode()).hexdigest()
            response = await client.get(
                "knowledge-bases",
                params={"company_id": company_id},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200

    asyncio.run(scenario())
