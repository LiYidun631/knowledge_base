"""公司删除：逐级保护边界、凭据清理与权限校验。"""

import asyncio
from hashlib import sha256
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.application.companies import create_company
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    CompanyCredential,
    ModelProfile,
    VectorLibrary,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def _app(monkeypatch, settings):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_delete_company_requires_empty_company_and_removes_credential(
    monkeypatch, tmp_path
) -> None:
    settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
    company_token = "owner-company-token"

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'company.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                company = await create_company(session, name="待删除公司", description=None)
                embedding = ModelProfile(
                    name="company-embedding", kind="embedding",
                    provider="openai_compatible", model_name="test-embedding", dimensions=2,
                )
                session.add(embedding)
                await session.flush()
                library = VectorLibrary(
                    company_id=company.id, name="测试大库",
                    embedding_model_id=embedding.id, models_locked_at=company.created_at,
                )
                session.add(library)
                session.add(CompanyCredential(
                    company_id=company.id,
                    name="默认凭据",
                    token_hash=sha256(company_token.encode()).hexdigest(),
                ))
                await session.commit()
                company_id = company.id

                async def db_session():
                    yield session

                app = _app(monkeypatch, settings)
                app.dependency_overrides[get_db_session] = db_session
                admin = {"Authorization": "Bearer admin-test-token"}
                owner = {"Authorization": f"Bearer {company_token}"}
                path = f"/api/v1/companies/{company_id}"
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test",
                ) as client:
                    # 公司 Token 调用管理员接口 403，未认证 401。
                    response = await client.delete(path, headers=owner)
                    assert response.status_code == 403
                    response = await client.delete(path)
                    assert response.status_code == 401

                    # 公司下还有大库时 409 逐级保护。
                    response = await client.delete(path, headers=admin)
                    assert response.status_code == 409

                    # 删掉大库后公司可删除，凭据一并清除。
                    await session.delete(library)
                    await session.commit()
                    response = await client.delete(path, headers=admin)
                    assert response.status_code == 204
                    assert await session.scalar(
                        select(Company).where(Company.id == company_id)
                    ) is None
                    assert await session.scalar(
                        select(CompanyCredential).where(
                            CompanyCredential.company_id == company_id
                        )
                    ) is None

                    # 重复删除 404；不存在的公司 404。
                    response = await client.delete(path, headers=admin)
                    assert response.status_code == 404
                    response = await client.delete(f"/api/v1/companies/{uuid4()}", headers=admin)
                    assert response.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(scenario())
