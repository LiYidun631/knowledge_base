"""管理端分块默认值接口：返回全局配置，仅管理员可用。"""

import asyncio
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


def test_chunk_defaults_is_admin_only(monkeypatch, tmp_path):
    settings = Settings(
        _env_file=None,
        auth_admin_token="admin-test-token",
        parent_chunk_chars=2000,
        child_chunk_chars=700,
        child_chunk_overlap_chars=90,
    )
    company_token = "company-test-token"

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chunks.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                company = Company(name="测试公司", code="company-test")
                session.add(company)
                await session.flush()
                session.add(CompanyCredential(
                    company_id=company.id,
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
                        "/api/v1/admin/chunk-defaults",
                        headers={"Authorization": "Bearer admin-test-token"},
                    )
                    assert response.status_code == 200
                    assert response.json() == {
                        "parent_chunk_chars": 2000,
                        "child_chunk_chars": 700,
                        "child_chunk_overlap_chars": 90,
                    }

                    # 公司 Token 不是管理员凭据，一律 403。
                    response = await client.get(
                        "/api/v1/admin/chunk-defaults",
                        headers={"Authorization": f"Bearer {company_token}"},
                    )
                    assert response.status_code == 403

                    response = await client.get("/api/v1/admin/chunk-defaults")
                    assert response.status_code == 401
        finally:
            await engine.dispose()

    asyncio.run(scenario())
