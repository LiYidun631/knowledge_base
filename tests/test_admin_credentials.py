"""多管理员凭据：签发、明文一次性、重置、停用、删除与鉴权接入。"""

import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    AdminCredential,
    Company,
    CompanyCredential,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def _app(monkeypatch, settings):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


@asynccontextmanager
async def _admin_service(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admins.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            company = Company(code="tenant", name="租户公司")
            session.add(company)
            await session.flush()
            session.add(
                CompanyCredential(
                    company_id=company.id,
                    name="默认凭据",
                    token_hash=sha256(b"tenant-token").hexdigest(),
                )
            )
            await session.commit()

        settings = Settings(_env_file=None, auth_admin_token="bootstrap-admin-token")
        app = _app(monkeypatch, settings)

        async def db_session():
            async with factory() as session:
                yield session
                await session.commit()

        app.dependency_overrides[get_db_session] = db_session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers={"Authorization": "Bearer bootstrap-admin-token"},
        ) as client:
            yield client, factory
    finally:
        await engine.dispose()


def test_admin_credential_lifecycle_and_auth(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, factory):
            # 创建：明文仅本次返回，响应禁止缓存。
            response = await client.post("admin/credentials", json={"name": "值班管理员"})
            assert response.status_code == 201, response.text
            issued = response.json()
            token = issued["token"]
            assert token.startswith("ka_")
            assert response.headers["cache-control"] == "no-store"

            # 新 Token 立即具备管理员权限，能访问管理端接口。
            admin = {"Authorization": f"Bearer {token}"}
            response = await client.get("admin/credentials", headers=admin)
            assert response.status_code == 200
            assert [item["name"] for item in response.json()] == ["值班管理员"]

            # 列表不回显任何哈希或明文。
            body = response.text
            assert token not in body
            async with factory() as session:
                credential = await session.get(AdminCredential, UUID(issued["admin_credential_id"]))
                assert credential.token_hash == sha256(token.encode()).hexdigest()
                assert credential.token_hash not in body
                assert credential.is_enabled

            # 公司 Token 无权访问管理端接口；无效 Token 返回 401。
            response = await client.get(
                "admin/credentials", headers={"Authorization": "Bearer tenant-token"}
            )
            assert response.status_code == 403
            response = await client.get(
                "admin/credentials", headers={"Authorization": "Bearer unknown-token"}
            )
            assert response.status_code == 401

            # 停用后立即失效，重新启用后恢复。
            await client.patch(
                f"admin/credentials/{issued['admin_credential_id']}", json={"is_enabled": False}
            )
            response = await client.get("admin/credentials", headers=admin)
            assert response.status_code == 401
            await client.patch(
                f"admin/credentials/{issued['admin_credential_id']}", json={"is_enabled": True}
            )
            assert (await client.get("admin/credentials", headers=admin)).status_code == 200

            # 重置：旧 Token 立即失效，新明文可用。
            response = await client.post(f"admin/credentials/{issued['admin_credential_id']}/reset")
            assert response.status_code == 200
            new_token = response.json()["token"]
            assert new_token != token
            assert (await client.get("admin/credentials", headers=admin)).status_code == 401
            assert (
                await client.get(
                    "admin/credentials", headers={"Authorization": f"Bearer {new_token}"}
                )
            ).status_code == 200

            # 删除后凭据消失且不可再用于访问。
            await client.delete(f"admin/credentials/{issued['admin_credential_id']}")
            dead = {"Authorization": f"Bearer {new_token}"}
            assert (await client.get("admin/credentials", headers=dead)).status_code == 401
            assert (await client.get("admin/credentials")).json() == []

            # .env 引导 Token 全程不受影响。
            assert (await client.get("admin/credentials")).status_code == 200

    asyncio.run(scenario())


def test_admin_credential_name_validation(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, _factory):
            response = await client.post("admin/credentials", json={"name": "   "})
            assert response.status_code == 422
            response = await client.patch(
                "admin/credentials/00000000-0000-0000-0000-000000000001",
                json={"is_enabled": False},
            )
            assert response.status_code == 404

    asyncio.run(scenario())


def test_admin_credential_mysql_ddl_shape():
    """MySQL 表结构使用 CHAR UUID、仅存哈希，token_hash 唯一。"""
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable

    ddl = str(CreateTable(AdminCredential.__table__).compile(dialect=mysql.dialect()))
    assert "CHAR(32)" in ddl
    assert "VARCHAR(64)" in ddl
    assert "ENGINE=InnoDB" in ddl
    assert "UNIQUE" in ddl
