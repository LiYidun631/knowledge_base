"""管理员角色权限与写操作审计：角色矩阵、审计落库、查询导出与保留期清理。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from hashlib import sha256
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.application.audit import purge_expired_audit_logs
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    AdminCredential,
    AuditLog,
    Company,
    CompanyCredential,
    utc_now,
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
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'roles.db'}")
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
        # 测试不跑 lifespan，手动绑定审计会话工厂。
        app.state.knowledge_audit_session_factory = factory

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


async def _issue(client, *, name: str, role: str) -> dict:
    response = await client.post("admin/credentials", json={"name": name, "role": role})
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_role_matrix_and_audit_log(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, factory):
            # 环境引导 Token 固定为 admin。
            me = (await client.get("admin/me")).json()
            assert me == {
                "name": "环境引导 Token",
                "role": "admin",
                "credential_id": None,
                "is_bootstrap": True,
            }

            operator = await _issue(client, name="运营同学", role="operator")
            viewer = await _issue(client, name="只读同学", role="viewer")
            operator_headers = _bearer(operator["token"])
            viewer_headers = _bearer(viewer["token"])

            assert (await client.get("admin/me", headers=operator_headers)).json()["role"] == (
                "operator"
            )
            assert (await client.get("admin/me", headers=viewer_headers)).json()["role"] == (
                "viewer"
            )

            # 运营：可读业务数据与写业务数据，不能碰凭据/模型配置/审计。
            assert (
                await client.get("admin/statistics", headers=operator_headers)
            ).status_code == 200
            company = await client.post(
                "companies", json={"name": "运营创建的公司"}, headers=operator_headers
            )
            assert company.status_code == 201, company.text
            for path in ("admin/credentials", "admin/model-prices", "admin/audit-logs"):
                assert (
                    await client.get(path, headers=operator_headers)
                ).status_code == 403, path

            # 只读：可读，不能写业务数据，也不能碰管理员专属资源。
            assert (
                await client.get("admin/qa-records", headers=viewer_headers)
            ).status_code == 200
            denied = await client.post(
                "companies", json={"name": "只读越权公司"}, headers=viewer_headers
            )
            assert denied.status_code == 403
            assert (
                await client.get("admin/credentials", headers=viewer_headers)
            ).status_code == 403

            # 审计只记管理端写操作，且记录操作者快照与结果状态。
            async with factory() as session:
                rows = list(
                    await session.scalars(
                        select(AuditLog).order_by(AuditLog.created_at, AuditLog.id)
                    )
                )
            assert rows
            assert all(row.method in {"POST", "PUT", "PATCH", "DELETE"} for row in rows)

            operator_write = next(
                row for row in rows if row.actor_name == "运营同学" and row.method == "POST"
            )
            assert operator_write.actor_role == "operator"
            assert operator_write.path == "/api/v1/companies"
            assert operator_write.status_code == 201
            assert operator_write.credential_id == UUID(operator["admin_credential_id"])

            denied_write = next(
                row
                for row in rows
                if row.actor_name == "只读同学" and row.path == "/api/v1/companies"
            )
            assert denied_write.actor_role == "viewer"
            assert denied_write.status_code == 403

            # 审计查询：管理员可读，按操作者与状态过滤。
            listing = await client.get("admin/audit-logs", params={"actor_name": "运营"})
            assert listing.status_code == 200
            body = listing.json()
            assert body["total"] >= 1
            assert {item["actor_name"] for item in body["items"]} == {"运营同学"}

            export = await client.get("admin/audit-logs/export")
            assert export.status_code == 200
            assert export.text.startswith("\ufeff")
            assert "操作者" in export.text
            assert "运营同学" in export.text

            # 角色可调整，立即对新请求生效。
            patched = await client.patch(
                f"admin/credentials/{operator['admin_credential_id']}",
                json={"role": "viewer"},
            )
            assert patched.status_code == 200
            assert patched.json()["role"] == "viewer"
            assert (
                await client.post(
                    "companies",
                    json={"name": "降级后越权"},
                    headers=operator_headers,
                )
            ).status_code == 403

            # 停用后立即失效，恢复后重新可用。
            await client.patch(
                f"admin/credentials/{operator['admin_credential_id']}",
                json={"is_enabled": False},
            )
            assert (
                await client.get("admin/me", headers=operator_headers)
            ).status_code == 401
            await client.patch(
                f"admin/credentials/{operator['admin_credential_id']}",
                json={"is_enabled": True},
            )
            assert (
                await client.get("admin/me", headers=operator_headers)
            ).status_code == 200

            # 删除只读凭据后其 Token 失效。
            assert (
                await client.delete(
                    f"admin/credentials/{viewer['admin_credential_id']}"
                )
            ).status_code == 204
            assert (await client.get("admin/me", headers=viewer_headers)).status_code == 401

    asyncio.run(scenario())


def test_default_role_is_admin_and_invalid_role_rejected(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, factory):
            response = await client.post("admin/credentials", json={"name": "默认管理员"})
            assert response.status_code == 201
            credential_id = response.json()["admin_credential_id"]
            async with factory() as session:
                credential = await session.get(AdminCredential, UUID(credential_id))
                assert credential.role == "admin"
            invalid = await client.post(
                "admin/credentials", json={"name": "非法角色", "role": "owner"}
            )
            assert invalid.status_code == 422

    asyncio.run(scenario())


def test_purge_expired_audit_logs(tmp_path):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                session.add_all(
                    [
                        AuditLog(
                            actor_name="old",
                            actor_role="admin",
                            method="POST",
                            path="/api/v1/companies",
                            status_code=201,
                            duration_ms=1,
                            created_at=utc_now() - timedelta(days=200),
                        ),
                        AuditLog(
                            actor_name="fresh",
                            actor_role="admin",
                            method="POST",
                            path="/api/v1/companies",
                            status_code=201,
                            duration_ms=1,
                        ),
                    ]
                )
                await session.commit()
                removed = await purge_expired_audit_logs(session)
                await session.commit()
                assert removed == 1
                names = list(await session.scalars(select(AuditLog.actor_name)))
                assert names == ["fresh"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_admin_only_resources_reject_company_token(monkeypatch, tmp_path):
    """公司 Token 仍按原有语义拒绝管理端接口，不因角色改造放宽。"""

    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, _factory):
            headers = {"Authorization": "Bearer tenant-token"}
            assert (await client.get("admin/audit-logs", headers=headers)).status_code == 403
            assert (await client.get("admin/me", headers=headers)).status_code == 403

    asyncio.run(scenario())
