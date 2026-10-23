"""管理端公司列表接口：仅管理员可读，含归档公司，公司 Token 返回 403。"""

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import Company, CompanyCredential
from knowledge_service.infrastructure.persistence.session import get_db_session


def _app(monkeypatch, settings):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_company_list_is_admin_only(monkeypatch, tmp_path):
    settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
    company_token = "company-test-token"

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'companies.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                now = datetime.now(UTC)
                newer = Company(
                    name="新公司", code="company-new",
                    created_at=now, updated_at=now,
                )
                older = Company(
                    name="旧公司", code="company-old", status="archived",
                    created_at=now - timedelta(seconds=10),
                    updated_at=now - timedelta(seconds=10),
                )
                session.add_all([newer, older])
                await session.flush()
                session.add(CompanyCredential(
                    company_id=newer.id,
                    name="默认凭据",
                    token_hash=sha256(company_token.encode()).hexdigest(),
                ))
                await session.commit()

                async def db_session():
                    yield session

                app = _app(monkeypatch, settings)
                app.dependency_overrides[get_db_session] = db_session
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test",
                ) as client:
                    response = await client.get(
                        "/api/v1/companies",
                        headers={"Authorization": "Bearer admin-test-token"},
                    )
                    assert response.status_code == 200
                    payload = response.json()
                    assert [item["name"] for item in payload] == ["新公司", "旧公司"]
                    assert [item["status"] for item in payload] == ["active", "archived"]
                    # 凭据数量与真实签发状态一致：新公司一枚启用，旧公司从未签发。
                    assert [item["credential_count"] for item in payload] == [1, 0]
                    assert [item["active_credential_count"] for item in payload] == [1, 0]
                    assert [item["has_vector_library"] for item in payload] == [False, False]
                    assert "token" not in payload[0]

                    response = await client.get(
                        "/api/v1/companies",
                        headers={"Authorization": f"Bearer {company_token}"},
                    )
                    assert response.status_code == 403

                    response = await client.get("/api/v1/companies")
                    assert response.status_code == 401
        finally:
            await engine.dispose()

    asyncio.run(scenario())
