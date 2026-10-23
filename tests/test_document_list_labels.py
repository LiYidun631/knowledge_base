"""文档列表返回产品标签；同 document_key 重复上传创建新版本。"""

import asyncio

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
    ModelProfile,
    ProductCatalog,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def _app(monkeypatch, settings):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_document_list_labels_and_version_replacement(monkeypatch, tmp_path) -> None:
    settings = Settings(_env_file=None, auth_admin_token="admin-test-token")

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'labels.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                company = await create_company(session, name="永康健康", description=None)
                embedding = ModelProfile(
                    name="labels-embedding", kind="embedding", provider="openai_compatible",
                    model_name="test-embedding", dimensions=2, is_default=True,
                )
                session.add(embedding)
                await session.flush()
                await create_vector_library(
                    session, company_id=company.id, name="大库",
                    embedding_model_id=embedding.id, rerank_model_id=None,
                )
                kb = await create_sub_library(session, company_id=company.id, name="健康管家")
                session.add_all([
                    ProductCatalog(product_name="体温计", model_name="额式体温计"),
                    ProductCatalog(product_name="血压计", model_name="臂式血压计"),
                ])
                await session.commit()

                async def db_session():
                    yield session

                app = _app(monkeypatch, settings)
                app.dependency_overrides[get_db_session] = db_session
                admin = {"Authorization": "Bearer admin-test-token"}

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test",
                ) as client:
                    base = f"/api/v1/knowledge-bases/{kb.id}/documents"
                    upload_params = {
                        "company_id": str(company.id),
                        "filename": "体温计使用说明.pdf",
                        "document_key": "manual-thermometer",
                        "product_names": ["体温计"],
                        "model_names": ["额式体温计"],
                    }
                    response = await client.post(
                        base,
                        headers={**admin, "Content-Type": "application/pdf"},
                        params=upload_params,
                        content=b"fake pdf v1",
                    )
                    assert response.status_code == 202, response.text

                    # 通用资料不带标签，列表返回空列表而不是缺字段。
                    response = await client.post(
                        base,
                        headers={**admin, "Content-Type": "application/pdf"},
                        params={
                            "company_id": str(company.id),
                            "filename": "公司介绍.pdf",
                            "document_key": "company-intro",
                        },
                        content=b"fake pdf",
                    )
                    assert response.status_code == 202, response.text

                    response = await client.get(base, headers=admin)
                    assert response.status_code == 200
                    assert response.headers["x-total-count"] == "2"
                    rows = response.json()
                    assert len(rows) == 2
                    by_key = {row["document_key"]: row for row in rows}
                    assert by_key["manual-thermometer"]["product_names"] == ["体温计"]
                    assert by_key["manual-thermometer"]["model_names"] == ["额式体温计"]
                    assert by_key["manual-thermometer"]["latest_version_number"] == 1
                    assert by_key["company-intro"]["product_names"] == []
                    assert by_key["company-intro"]["model_names"] == []

                    # 文件名与文档标识都参与搜索，总数头跟随过滤条件。
                    response = await client.get(base, headers=admin, params={"q": "体温计"})
                    assert [row["document_key"] for row in response.json()] == [
                        "manual-thermometer"
                    ]
                    assert response.headers["x-total-count"] == "1"
                    response = await client.get(base, headers=admin, params={"q": "company-intro"})
                    assert [row["document_key"] for row in response.json()] == [
                        "company-intro"
                    ]
                    response = await client.get(base, headers=admin, params={"q": "不存在的文件"})
                    assert response.json() == []
                    assert response.headers["x-total-count"] == "0"

                    # 分页只影响返回条目，总数头仍为过滤后的全量。
                    response = await client.get(base, headers=admin, params={"limit": 1})
                    assert len(response.json()) == 1
                    assert response.headers["x-total-count"] == "2"
                    response = await client.get(
                        base, headers=admin, params={"limit": 1, "offset": 1}
                    )
                    assert len(response.json()) == 1
                    assert response.headers["x-total-count"] == "2"

                    # 两篇文档都在根目录，root_only 不丢数据。
                    response = await client.get(base, headers=admin, params={"root_only": True})
                    assert len(response.json()) == 2

                    # 同一 document_key 再上传 = 创建新版本，文件名可不同。
                    upload_params["filename"] = "体温计使用说明v2.pdf"
                    response = await client.post(
                        base,
                        headers={**admin, "Content-Type": "application/pdf"},
                        params=upload_params,
                        content=b"fake pdf v2",
                    )
                    assert response.status_code == 202, response.text
                    response = await client.get(base, headers=admin)
                    rows = response.json()
                    assert len(rows) == 2
                    by_key = {row["document_key"]: row for row in rows}
                    updated = by_key["manual-thermometer"]
                    assert updated["latest_version_number"] == 2
                    assert updated["filename"] == "体温计使用说明v2.pdf"
                    assert updated["product_names"] == ["体温计"]
                    assert updated["model_names"] == ["额式体温计"]

                    # 版本历史保留两版；is_current 由入库完成时翻转，这里仅核对顺序与文件名。
                    response = await client.get(
                        f"/api/v1/documents/{updated['id']}/versions", headers=admin
                    )
                    assert response.status_code == 200
                    versions = response.json()
                    assert [item["version_number"] for item in versions] == [2, 1]
                    assert versions[0]["filename"] == "体温计使用说明v2.pdf"
                    assert versions[1]["filename"] == "体温计使用说明.pdf"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
