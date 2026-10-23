"""知识库目录的组织操作：多级创建、同级重名、移动、非空删除与文档归属。"""

import asyncio
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.application.companies import (
    create_company,
    create_sub_library,
    create_vector_library,
)
from knowledge_service.application.documents.folders import (
    create_folder,
    delete_folder,
    list_folders,
    move_document,
    update_folder,
)
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.domain.errors import (
    FolderNameConflict,
    FolderNotEmpty,
    FolderNotFound,
)
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    CompanyCredential,
    Document,
    ModelProfile,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


async def _seed_kb(session):
    """准备一个带公司、大库、子库和两个子库的测试环境。"""
    company = await create_company(session, name="永康健康", description=None)
    embedding = ModelProfile(
        name="folder-embedding", kind="embedding", provider="openai_compatible",
        model_name="test-embedding", dimensions=2, is_default=True,
    )
    session.add(embedding)
    await session.flush()
    await create_vector_library(
        session, company_id=company.id, name="大库",
        embedding_model_id=embedding.id, rerank_model_id=None,
    )
    first = await create_sub_library(session, company_id=company.id, name="子库一")
    second = await create_sub_library(session, company_id=company.id, name="子库二")
    await session.commit()
    return company, embedding, first, second


def test_folder_service_rules(tmp_path) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'folders.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                _, _, first, second = await _seed_kb(session)
                # 以下失败分支都在写入前抛错，不会弄脏事务，无需 rollback：
                # AsyncSession 的 rollback 会过期全部对象，后续访问属性会触发同步懒加载报错。
                first_id, second_id = first.id, second.id

                root = await create_folder(session, knowledge_base_id=first_id, name="产品手册")
                child = await create_folder(
                    session, knowledge_base_id=first_id, name="2025", parent_id=root.id
                )
                # 不同父目录下允许同名；同一父目录下重名被拒绝。
                await create_folder(
                    session, knowledge_base_id=first_id, name="产品手册", parent_id=child.id
                )
                with pytest.raises(FolderNameConflict):
                    await create_folder(
                        session, knowledge_base_id=first_id, name="2025", parent_id=root.id
                    )

                # 不能把目录移动到自身或自己的子孙目录下。
                with pytest.raises(ValueError):
                    await update_folder(
                        session, knowledge_base_id=first_id, folder_id=root.id,
                        parent_id=child.id, move=True,
                    )

                document = Document(
                    knowledge_base_id=first_id, document_key="doc-1", filename="a.pdf",
                    folder_id=root.id, latest_version_number=1, status="completed",
                )
                session.add(document)
                await session.flush()
                document_id = document.id
                await session.commit()

                # 目录下有文档时拒绝删除。
                with pytest.raises(FolderNotEmpty):
                    await delete_folder(session, knowledge_base_id=first_id, folder_id=root.id)

                # 移动文档到另一子库的目录被拒绝，避免跨库归属。
                other_folder = await create_folder(
                    session, knowledge_base_id=second_id, name="别的库目录"
                )
                await session.flush()
                with pytest.raises(FolderNotFound):
                    await move_document(
                        session, document_id=document_id, folder_id=other_folder.id
                    )

                # 空目录可删除。
                leaf = await create_folder(
                    session, knowledge_base_id=first_id, name="临时", parent_id=root.id
                )
                await session.flush()
                await delete_folder(session, knowledge_base_id=first_id, folder_id=leaf.id)
                remaining = await list_folders(session, knowledge_base_id=first_id)
                assert {folder.name for folder in remaining} == {"产品手册", "2025"}
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _app(monkeypatch, settings):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_folder_api_and_document_move(monkeypatch, tmp_path) -> None:
    settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
    owner_token = "owner-company-token"
    other_token = "other-company-token"

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'folder-api.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                company, _, first, _ = await _seed_kb(session)
                other = await create_company(session, name="别的公司", description=None)
                session.add_all([
                    CompanyCredential(
                        company_id=company.id,
                        name="默认凭据",
                        token_hash=sha256(owner_token.encode()).hexdigest()
                    ),
                    CompanyCredential(
                        company_id=other.id,
                        name="默认凭据",
                        token_hash=sha256(other_token.encode()).hexdigest()
                    ),
                ])
                await session.commit()

                async def db_session():
                    yield session

                app = _app(monkeypatch, settings)
                app.dependency_overrides[get_db_session] = db_session
                admin = {"Authorization": "Bearer admin-test-token"}
                owner = {"Authorization": f"Bearer {owner_token}"}
                outsider = {"Authorization": f"Bearer {other_token}"}
                base = f"/api/v1/knowledge-bases/{first.id}/folders"
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test",
                ) as client:
                    # 管理员创建两级目录；同级重名返回 409。
                    response = await client.post(base, headers=admin, json={"name": "产品手册"})
                    assert response.status_code == 201
                    root_id = response.json()["id"]
                    response = await client.post(
                        base, headers=admin, json={"name": "2025", "parent_id": root_id}
                    )
                    assert response.status_code == 201
                    child_id = response.json()["id"]
                    response = await client.post(base, headers=admin, json={"name": "产品手册"})
                    assert response.status_code == 409

                    # 列表为扁平结构，包含 parent_id。
                    response = await client.get(base, headers=admin)
                    assert response.status_code == 200
                    by_id = {item["id"]: item for item in response.json()}
                    assert by_id[child_id]["parent_id"] == root_id

                    # 重命名与移动到根目录。
                    response = await client.patch(
                        f"{base}/{root_id}", headers=admin, json={"name": "产品资料"}
                    )
                    assert response.status_code == 200
                    assert response.json()["name"] == "产品资料"
                    response = await client.patch(
                        f"{base}/{child_id}", headers=admin, json={"parent_id": None}
                    )
                    assert response.status_code == 200
                    assert response.json()["parent_id"] is None

                    # 循环移动被拒绝：把目录移动到自己的子目录下。
                    response = await client.post(base, headers=admin, json={"name": "甲"})
                    parent_id = response.json()["id"]
                    response = await client.post(
                        base, headers=admin, json={"name": "乙", "parent_id": parent_id}
                    )
                    descendant_id = response.json()["id"]
                    response = await client.patch(
                        f"{base}/{parent_id}",
                        headers=admin,
                        json={"parent_id": descendant_id},
                    )
                    assert response.status_code == 422

                    # 目录下有文档时拒绝删除；移走文档后可删除。
                    response = await client.post(
                        base,
                        headers=admin,
                        json={"name": "占位文件目录", "parent_id": child_id},
                    )
                    leaf_id = response.json()["id"]
                    document = Document(
                        knowledge_base_id=first.id, document_key="doc-1", filename="a.pdf",
                        folder_id=UUID(leaf_id), latest_version_number=1, status="completed",
                    )
                    session.add(document)
                    await session.flush()
                    response = await client.delete(f"{base}/{leaf_id}", headers=admin)
                    assert response.status_code == 409
                    response = await client.patch(
                        f"/api/v1/documents/{document.id}",
                        headers=admin,
                        json={"folder_id": None},
                    )
                    assert response.status_code == 200
                    assert response.json()["folder_id"] is None
                    response = await client.delete(f"{base}/{leaf_id}", headers=admin)
                    assert response.status_code == 204

                    # 目录过滤列表与文档移动。
                    response = await client.patch(
                        f"/api/v1/documents/{document.id}",
                        headers=admin,
                        json={"folder_id": root_id},
                    )
                    assert response.status_code == 200
                    response = await client.get(
                        f"/api/v1/knowledge-bases/{first.id}/documents",
                        headers=admin,
                        params={"folder_id": root_id},
                    )
                    assert [item["id"] for item in response.json()] == [str(document.id)]
                    response = await client.get(
                        f"/api/v1/knowledge-bases/{first.id}/documents",
                        headers=admin,
                        params={"folder_id": child_id},
                    )
                    assert response.json() == []
                    # 文档已移入目录，root_only 只剩根目录文档（此处为空）。
                    response = await client.get(
                        f"/api/v1/knowledge-bases/{first.id}/documents",
                        headers=admin,
                        params={"root_only": "true"},
                    )
                    assert response.json() == []
                    assert response.headers["x-total-count"] == "0"

                    # 鉴权：本公司 Token 可用；其他公司 Token 对子库范围接口返回 404，不泄露归属。
                    response = await client.get(base, headers=owner)
                    assert response.status_code == 200
                    response = await client.get(base, headers=outsider)
                    assert response.status_code == 404
                    response = await client.get(base)
                    assert response.status_code == 401

                    # 移动文档到不存在或跨库的目录返回 404。
                    response = await client.patch(
                        f"/api/v1/documents/{document.id}",
                        headers=admin,
                        json={"folder_id": str(uuid4())},
                    )
                    assert response.status_code == 404
                    response = await client.patch(
                        f"/api/v1/documents/{document.id}",
                        headers=admin,
                        json={},
                    )
                    assert response.status_code == 422

                    # 归档后目录和文档归属整体只读，恢复后才能继续修改。
                    first.status = "archived"
                    await session.flush()
                    response = await client.post(base, headers=admin, json={"name": "归档后新建"})
                    assert response.status_code == 409
                    response = await client.patch(
                        f"{base}/{root_id}", headers=admin, json={"name": "归档后改名"}
                    )
                    assert response.status_code == 409
                    response = await client.delete(f"{base}/{root_id}", headers=admin)
                    assert response.status_code == 409
                    response = await client.patch(
                        f"/api/v1/documents/{document.id}",
                        headers=admin,
                        json={"folder_id": None},
                    )
                    assert response.status_code == 409
                    first.status = "active"
                    await session.flush()
                    response = await client.post(base, headers=admin, json={"name": "恢复后新建"})
                    assert response.status_code == 201
        finally:
            await engine.dispose()

    asyncio.run(scenario())
