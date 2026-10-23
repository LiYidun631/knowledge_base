"""文档版本切块预览：分页、统计、章节与块类型，校验归属与参数上限。"""

import asyncio
from hashlib import sha256
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.application.companies import (
    create_company,
    create_sub_library,
    create_vector_library,
)
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    CompanyCredential,
    Document,
    DocumentChunk,
    DocumentVersion,
    ModelProfile,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def _app(monkeypatch, settings):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def _chunk(
    document_id,
    version_id,
    ordinal,
    level,
    content,
    parent_id=None,
    page_number=None,
    extra_metadata=None,
):
    return DocumentChunk(
        document_id=document_id,
        document_version_id=version_id,
        parent_chunk_id=parent_id,
        ordinal=ordinal,
        level=level,
        content_sha256=sha256(content.encode()).hexdigest(),
        content=content,
        token_count=len(content),
        page_number=page_number,
        extra_metadata=extra_metadata or {},
    )


def test_chunk_preview_lists_parents_and_children(monkeypatch, tmp_path) -> None:
    settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
    company_token = "owner-company-token"

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chunks.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                company = await create_company(session, name="永康健康", description=None)
                embedding = ModelProfile(
                    name="preview-embedding", kind="embedding",
                    provider="openai_compatible", model_name="test-embedding", dimensions=2,
                )
                session.add(embedding)
                await session.flush()
                await create_vector_library(
                    session, company_id=company.id, name="大库",
                    embedding_model_id=embedding.id, rerank_model_id=None,
                )
                knowledge_base = await create_sub_library(
                    session, company_id=company.id, name="子库"
                )
                document = Document(
                    company_id=company.id, knowledge_base_id=knowledge_base.id,
                    document_key="doc-1", filename="a.pdf",
                    latest_version_number=1, status="completed",
                )
                session.add(document)
                await session.flush()
                version = DocumentVersion(
                    document_id=document.id, version_number=1, filename="a.pdf",
                    size_bytes=10, content_sha256="x" * 64, object_key="k/a.pdf",
                    status="completed", is_current=True,
                )
                session.add(version)
                await session.flush()
                first_parent = _chunk(
                    document.id, version.id, 1, "parent", "父块一", page_number=None,
                    extra_metadata={"section": "第一章 / 第一节", "block_type": "text"},
                )
                second_parent = _chunk(
                    document.id, version.id, 2, "parent", "父块二较长内容", page_number=3,
                    extra_metadata={"block_type": "table"},
                )
                # 父块先落库取到 UUID，子块的 parent_chunk_id 用显式 ID 而非 ORM 对象，
                # 避免未 flush 的对象在绑参时无法解析出主键。
                # ordinal 与生产一致为全版本连续编号：父块 1、2，子块 3、4、5。
                session.add_all([first_parent, second_parent])
                await session.flush()
                session.add_all([
                    _chunk(document.id, version.id, 3, "child", "子块一", first_parent.id),
                    _chunk(document.id, version.id, 4, "child", "子块二", first_parent.id),
                    _chunk(document.id, version.id, 5, "child", "子块三", second_parent.id),
                ])
                await session.flush()
                session.add(CompanyCredential(
                    company_id=company.id,
                    name="默认凭据",
                    token_hash=sha256(company_token.encode()).hexdigest(),
                ))
                await session.commit()
                document_id, version_id = document.id, version.id

                async def db_session():
                    yield session

                app = _app(monkeypatch, settings)
                app.dependency_overrides[get_db_session] = db_session
                admin = {"Authorization": "Bearer admin-test-token"}
                owner = {"Authorization": f"Bearer {company_token}"}
                path = f"/api/v1/documents/{document_id}/versions/{version_id}/chunks"
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test",
                ) as client:
                    response = await client.get(path, headers=admin)
                    assert response.status_code == 200
                    payload = response.json()
                    assert payload["total_parents"] == 2
                    assert payload["total_children"] == 3
                    assert payload["total_chars"] == 10
                    assert payload["offset"] == 0
                    assert payload["limit"] == 100
                    parents = payload["parents"]
                    assert [item["ordinal"] for item in parents] == [1, 2]
                    assert parents[0]["content"] == "父块一"
                    assert parents[0]["char_count"] == 3
                    assert parents[0]["page_number"] is None
                    assert parents[0]["section"] == "第一章 / 第一节"
                    assert parents[0]["block_type"] == "text"
                    assert parents[1]["page_number"] == 3
                    assert parents[1]["block_type"] == "table"
                    assert [child["ordinal"] for child in parents[0]["children"]] == [3, 4]
                    assert parents[0]["children"][0]["content"] == "子块一"
                    assert parents[0]["children"][0]["char_count"] == 3
                    assert [child["ordinal"] for child in parents[1]["children"]] == [5]

                    # limit 与 offset 只作用于父块，统计不受分页影响。
                    response = await client.get(path, headers=admin, params={"limit": 1})
                    assert response.status_code == 200
                    page = response.json()
                    assert page["total_parents"] == 2
                    assert [item["ordinal"] for item in page["parents"]] == [1]
                    response = await client.get(
                        path, headers=admin, params={"offset": 1, "limit": 1}
                    )
                    assert [item["ordinal"] for item in response.json()["parents"]] == [2]

                    # 本公司 Token 可用；版本不属于文档时 404。
                    response = await client.get(path, headers=owner)
                    assert response.status_code == 200
                    response = await client.get(
                        f"/api/v1/documents/{document_id}/versions/{uuid4()}/chunks",
                        headers=admin,
                    )
                    assert response.status_code == 404
                    response = await client.get(
                        f"/api/v1/documents/{uuid4()}/versions/{version_id}/chunks",
                        headers=admin,
                    )
                    assert response.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(scenario())
