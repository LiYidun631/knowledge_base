"""管理员登记接入项目：创建、去重、参数校验与鉴权。"""

import asyncio

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import AppProject  # noqa: F401
from knowledge_service.infrastructure.persistence.session import get_db_session


def _admin_projects_app(monkeypatch, settings, factory):
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


def test_http_admin_project_registration(monkeypatch, tmp_path) -> None:
    """登记后可查询；重复标识 409，非法参数 422，匿名 401。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin_projects.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _admin_projects_app(monkeypatch, settings, factory)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
        ) as anonymous:
            response = await anonymous.post(
                "admin/projects",
                json={"project_key": "health_agent", "display_name": "健康助手"},
            )
            assert response.status_code == 401

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers={"Authorization": "Bearer admin-test-token"},
        ) as client:
            # 首尾空白会被清理，保留天数缺省 90 天。
            response = await client.post(
                "admin/projects",
                json={
                    "project_key": "  health_agent  ",
                    "display_name": "  健康助手  ",
                    "description": "   ",
                },
            )
            assert response.status_code == 201, response.text
            created = response.json()
            assert created["project_key"] == "health_agent"
            assert created["display_name"] == "健康助手"
            assert created["description"] is None
            assert created["retention_days"] == 90
            assert created["is_enabled"] is True

            response = await client.get("admin/projects")
            assert response.status_code == 200
            assert [item["project_key"] for item in response.json()] == ["health_agent"]

            response = await client.post(
                "admin/projects",
                json={"project_key": "health_agent", "display_name": "重复项目"},
            )
            assert response.status_code == 409
            assert response.json()["detail"] == "项目标识已登记"

            for payload in (
                {"project_key": "bad key", "display_name": "非法标识"},
                {"project_key": "ok_key", "display_name": "   "},
                {"project_key": "ok_key", "display_name": "保留天数越界", "retention_days": 0},
            ):
                response = await client.post("admin/projects", json=payload)
                assert response.status_code == 422, response.text

        await engine.dispose()

    asyncio.run(scenario())
