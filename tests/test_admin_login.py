"""管理员账号密码登录：会话签发与吊销、爆破限流、与长期 Token 共存。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from hashlib import sha256

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.application import admin_auth, admin_credentials
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    AdminCredential,
    AdminSession,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_db_session

BOOTSTRAP = {"Authorization": "Bearer bootstrap-admin-token"}
PASSWORD = "kb-Login-2026"


def _app(monkeypatch, settings):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


@asynccontextmanager
async def _login_service(monkeypatch, tmp_path):
    admin_auth._login_failures.clear()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'login.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
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
        ) as client:
            yield client, factory
    finally:
        admin_auth._login_failures.clear()
        await engine.dispose()


async def _seed_admin(factory, *, name="李运维", role="admin", username="liyidun"):
    """直连领域服务建管理员，避免测试用登录接口自身造数据。"""
    async with factory() as session:
        credential, token = await admin_credentials.create_admin_credential(
            session, name=name, role=role, username=username, password=PASSWORD
        )
        await session.commit()
    return credential.id, token


async def _login(client, username="liyidun", password=PASSWORD):
    response = await client.post(
        "admin/login", json={"username": username, "password": password}
    )
    return response


def test_login_issues_session_token_and_logout_revokes_it(monkeypatch, tmp_path):
    async def scenario():
        async with _login_service(monkeypatch, tmp_path) as (client, factory):
            credential_id, long_token = await _seed_admin(factory)
            response = await _login(client)
            assert response.status_code == 200, response.text
            body = response.json()
            session_token = body["token"]
            assert session_token.startswith("kas_")
            assert body["name"] == "李运维"
            assert body["admin_role"] == "admin"
            assert response.headers["cache-control"] == "no-store"
            # 明文会话与密码都不出现在响应里。
            assert PASSWORD not in response.text

            session_headers = {"Authorization": f"Bearer {session_token}"}
            response = await client.get("admin/me", headers=session_headers)
            assert response.status_code == 200
            assert response.json() == {
                "name": "李运维",
                "role": "admin",
                "credential_id": str(credential_id),
                "is_bootstrap": False,
            }

            async with factory() as session:
                record = (
                    await session.scalars(select(AdminSession))
                ).one()
                assert record.credential_id == credential_id
                assert record.token_hash == sha256(session_token.encode()).hexdigest()
                assert record.expires_at > utc_now() + timedelta(days=6)
                credential = await session.get(AdminCredential, credential_id)
                assert credential.password_hash.startswith("pbkdf2_sha256$")
                assert PASSWORD not in credential.password_hash

            # 登出撤销当前会话，长期 Token 与引导 Token 不受影响。
            assert (await client.post("admin/logout", headers=session_headers)).status_code == 204
            assert (await client.get("admin/me", headers=session_headers)).status_code == 401
            assert (
                await client.get(
                    "admin/me", headers={"Authorization": f"Bearer {long_token}"}
                )
            ).status_code == 200
            assert (await client.get("admin/me", headers=BOOTSTRAP)).status_code == 200

    asyncio.run(scenario())


def test_login_rejects_bad_password_unknown_account_and_disabled(monkeypatch, tmp_path):
    async def scenario():
        async with _login_service(monkeypatch, tmp_path) as (client, factory):
            credential_id, _ = await _seed_admin(factory)
            for username, password in (("liyidun", "wrong-password"), ("ghost", PASSWORD)):
                response = await _login(client, username=username, password=password)
                assert response.status_code == 401
                # 统一文案，不暴露账号是否存在。
                assert response.json()["detail"] == "账号或密码不正确"

            await client.patch(
                f"admin/credentials/{credential_id}",
                json={"is_enabled": False},
                headers=BOOTSTRAP,
            )
            assert (await _login(client)).status_code == 401

    asyncio.run(scenario())


def test_login_rate_limits_repeated_failures(monkeypatch, tmp_path):
    async def scenario():
        async with _login_service(monkeypatch, tmp_path) as (client, factory):
            await _seed_admin(factory)
            for _ in range(admin_auth.LOGIN_MAX_FAILURES):
                assert (await _login(client, password="bad-pass-123")).status_code == 401
            response = await _login(client)
            assert response.status_code == 429
            assert int(response.headers["retry-after"]) > 0
            # 限流窗口内即使密码正确也拒绝。
            admin_auth.clear_login_failures("127.0.0.1")
            assert (await _login(client)).status_code == 200

    asyncio.run(scenario())


def test_password_reset_disables_and_duplicate_account_revoke_sessions(monkeypatch, tmp_path):
    async def scenario():
        async with _login_service(monkeypatch, tmp_path) as (client, factory):
            credential_id, _ = await _seed_admin(factory)
            session_token = (await _login(client)).json()["token"]
            headers = {"Authorization": f"Bearer {session_token}"}

            # 改密即踢会话，旧密码不再可用。
            response = await client.patch(
                f"admin/credentials/{credential_id}",
                json={"password": "kb-Login-2027"},
                headers=BOOTSTRAP,
            )
            assert response.status_code == 200, response.text
            assert response.json()["has_password"] is True
            assert (await client.get("admin/me", headers=headers)).status_code == 401
            assert (await _login(client)).status_code == 401
            assert (await _login(client, password="kb-Login-2027")).status_code == 200

            # 停用同样踢会话。
            session_token = (await _login(client, password="kb-Login-2027")).json()["token"]
            await client.patch(
                f"admin/credentials/{credential_id}",
                json={"is_enabled": False},
                headers=BOOTSTRAP,
            )
            assert (
                await client.get("admin/me", headers={"Authorization": f"Bearer {session_token}"})
            ).status_code == 401
            await client.patch(
                f"admin/credentials/{credential_id}",
                json={"is_enabled": True},
                headers=BOOTSTRAP,
            )

            # 收回账号密码登录后会话与账号入口一起失效。
            response = await client.patch(
                f"admin/credentials/{credential_id}",
                json={"username": ""},
                headers=BOOTSTRAP,
            )
            assert response.status_code == 200
            assert response.json()["username"] is None
            assert (await _login(client, password="kb-Login-2027")).status_code == 401
            async with factory() as session:
                assert (await session.scalars(select(AdminSession))).first() is None

    asyncio.run(scenario())


def test_expired_session_is_rejected(monkeypatch, tmp_path):
    async def scenario():
        async with _login_service(monkeypatch, tmp_path) as (client, factory):
            await _seed_admin(factory)
            session_token = (await _login(client)).json()["token"]
            async with factory() as session:
                await session.execute(
                    update(AdminSession).values(expires_at=utc_now() - timedelta(minutes=1))
                )
                await session.commit()
            assert (
                await client.get("admin/me", headers={"Authorization": f"Bearer {session_token}"})
            ).status_code == 401
            # 过期会话在下次登录时被清理。
            assert (await _login(client)).status_code == 200
            async with factory() as session:
                assert len((await session.scalars(select(AdminSession))).all()) == 1

    asyncio.run(scenario())


def test_credentials_api_manages_accounts(monkeypatch, tmp_path):
    async def scenario():
        async with _login_service(monkeypatch, tmp_path) as (client, factory):
            create_payload = {
                "name": "运营小王",
                "role": "operator",
                "username": " Wang ",
                "password": PASSWORD,
            }
            response = await client.post(
                "admin/credentials", json=create_payload, headers=BOOTSTRAP
            )
            assert response.status_code == 201, response.text
            credential_id = response.json()["admin_credential_id"]
            assert (await _login(client, username="wang")).status_code == 200

            # 账号统一小写，重复注册（大小写不同）返回 409。
            response = await client.post(
                "admin/credentials",
                json={"name": "重复账号", "username": "WANG", "password": PASSWORD},
                headers=BOOTSTRAP,
            )
            assert response.status_code == 409
            # 开通账号必须同时给密码。
            response = await client.post(
                "admin/credentials",
                json={"name": "缺密码", "username": "noPass"},
                headers=BOOTSTRAP,
            )
            assert response.status_code == 400
            # 只发 Token 的管理员不开通账号密码登录。
            response = await client.post(
                "admin/credentials", json={"name": "脚本用"}, headers=BOOTSTRAP
            )
            assert response.status_code == 201

            response = await client.get("admin/credentials", headers=BOOTSTRAP)
            assert response.status_code == 200
            by_name = {item["name"]: item for item in response.json()}
            assert by_name["运营小王"]["username"] == "wang"
            assert by_name["运营小王"]["has_password"] is True
            assert by_name["脚本用"]["username"] is None
            assert by_name["脚本用"]["has_password"] is False
            assert "password_hash" not in response.text
            assert PASSWORD not in response.text

            # 运营角色的会话可登录，但管理端凭据接口仍要求 admin。
            operator_token = (await _login(client, username="wang")).json()["token"]
            assert (
                await client.get(
                    "admin/me", headers={"Authorization": f"Bearer {operator_token}"}
                )
            ).json()["role"] == "operator"
            assert (
                await client.get(
                    "admin/credentials", headers={"Authorization": f"Bearer {operator_token}"}
                )
            ).status_code == 403
            assert (
                await client.post(
                    f"admin/credentials/{credential_id}/reset",
                    headers={"Authorization": f"Bearer {operator_token}"},
                )
            ).status_code == 403

    asyncio.run(scenario())
