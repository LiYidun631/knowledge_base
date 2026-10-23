"""公司级向量大库查询接口：向导判断模型选择步骤的前置条件。"""

import asyncio
from hashlib import sha256

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.application.companies import create_company, create_vector_library
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import CompanyCredential, ModelProfile
from knowledge_service.infrastructure.persistence.session import get_db_session


def _app(monkeypatch, settings):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_vector_library_query_is_company_scoped(monkeypatch, tmp_path):
    settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
    owner_token = "owner-company-token"
    other_token = "other-company-token"

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                owner = await create_company(session, name="有库公司", description=None)
                other = await create_company(session, name="无库公司", description=None)
                embedding = ModelProfile(
                    name="query-embedding", kind="embedding", provider="dashscope",
                    model_name="text-embedding-v4", dimensions=1024,
                )
                reranker = ModelProfile(
                    name="query-rerank", kind="rerank", provider="dashscope",
                    model_name="qwen3.7-text-rerank",
                )
                session.add_all([embedding, reranker])
                await session.flush()
                library = await create_vector_library(
                    session,
                    company_id=owner.id,
                    name="查询测试大库",
                    embedding_model_id=embedding.id,
                    rerank_model_id=reranker.id,
                )
                session.add_all([
                    CompanyCredential(
                        company_id=owner.id,
                        name="默认凭据",
                        token_hash=sha256(owner_token.encode()).hexdigest(),
                    ),
                    CompanyCredential(
                        company_id=other.id,
                        name="默认凭据",
                        token_hash=sha256(other_token.encode()).hexdigest(),
                    ),
                ])
                await session.commit()

                async def db_session():
                    yield session

                app = _app(monkeypatch, settings)
                app.dependency_overrides[get_db_session] = db_session
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test",
                ) as client:
                    path = f"/api/v1/companies/{owner.id}/vector-library"
                    response = await client.get(
                        path, headers={"Authorization": "Bearer admin-test-token"},
                    )
                    assert response.status_code == 200
                    payload = response.json()
                    assert payload["id"] == str(library.id)
                    assert payload["embedding_model_id"] == str(embedding.id)
                    assert payload["rerank_model_id"] == str(reranker.id)

                    response = await client.get(
                        path, headers={"Authorization": f"Bearer {owner_token}"},
                    )
                    assert response.status_code == 200

                    response = await client.get(
                        path, headers={"Authorization": f"Bearer {other_token}"},
                    )
                    assert response.status_code == 403

                    response = await client.get(
                        f"/api/v1/companies/{other.id}/vector-library",
                        headers={"Authorization": "Bearer admin-test-token"},
                    )
                    assert response.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(scenario())
