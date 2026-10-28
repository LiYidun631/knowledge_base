"""全局搜索接口：六类资源命中、条数上限、通配符转义与只读角色可读。"""

import asyncio
from hashlib import sha256

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    AdminCredential,
    AppProject,
    Company,
    Document,
    FaqEntry,
    KnowledgeBase,
    ModelProfile,
    ProductCatalog,
    ProjectQaTrace,
    QARecord,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def test_global_search(monkeypatch, tmp_path) -> None:
    """同一关键词在六类资源中各命中一条，且每类总数与 limit 截断符合预期。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'search.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                embedding = ModelProfile(
                    name="search-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="search-embedding",
                    dimensions=2,
                )
                company = Company(code="search-co", name="搜索公司")
                session.add_all([embedding, company])
                await session.flush()
                kb = KnowledgeBase(
                    company_id=company.id,
                    name="体温计知识库",
                    description="体温计的常见问题与说明书",
                    embedding_model_id=embedding.id,
                )
                session.add(kb)
                await session.flush()
                session.add_all(
                    [
                        Document(
                            company_id=company.id,
                            knowledge_base_id=kb.id,
                            document_key="hbf-temp-manual",
                            filename="体温计说明书.pdf",
                            status="completed",
                        ),
                        FaqEntry(
                            knowledge_base_id=kb.id,
                            question="体温计怎么用",
                            answer="按下开关，等待蜂鸣提示后读数。",
                            question_embedding=[0.1, 0.2],
                            source="manual",
                        ),
                        QARecord(
                            company_id=company.id,
                            knowledge_base_id=kb.id,
                            request_id="search-req-1",
                            query="体温计测多久",
                            answer="大约三十秒即可完成测量。",
                            retrieval_mode="hybrid",
                            top_k=5,
                            status="success",
                        ),
                        ProductCatalog(product_name="体温计", model_name="额温型"),
                    ]
                )
                project = AppProject(project_key="search_proj", display_name="搜索接入项目")
                session.add(project)
                await session.flush()
                session.add(
                    ProjectQaTrace(
                        company_id=company.id,
                        project_id=project.id,
                        knowledge_base_id=kb.id,
                        external_request_id="ext-search-1",
                        query="体温计准不准",
                        status="success",
                        answer_mode="rag",
                    )
                )
                session.add(
                    AdminCredential(
                        name="只读同学",
                        role="viewer",
                        token_hash=sha256(b"viewer-token").hexdigest(),
                    )
                )
                await session.commit()

            settings = Settings(_env_file=None, auth_admin_token="search-admin-token")
            import main

            monkeypatch.setattr(main, "get_settings", lambda: settings)
            app = main.create_app()
            app.dependency_overrides[get_settings] = lambda: settings
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
                headers={"Authorization": "Bearer search-admin-token"},
            ) as client:
                response = await client.get("admin/search", params={"q": "体温计"})
                assert response.status_code == 200, response.text
                payload = response.json()
                assert payload["q"] == "体温计"
                groups = {group["kind"]: group for group in payload["groups"]}
                assert set(groups) == {
                    "knowledge_base",
                    "document",
                    "faq",
                    "qa_record",
                    "project_trace",
                    "product",
                }
                assert {kind: group["total"] for kind, group in groups.items()} == {
                    "knowledge_base": 1,
                    "document": 1,
                    "faq": 1,
                    "qa_record": 1,
                    "project_trace": 1,
                    "product": 1,
                }
                document_hit = groups["document"]["items"][0]
                assert document_hit["title"] == "体温计说明书.pdf"
                assert document_hit["knowledge_base_id"] == str(kb.id)
                assert document_hit["subtitle"] == "搜索公司 · 体温计知识库"
                # 正文摘要在命中词附近截取，答案类资源带出上下文。
                assert "三十秒" in groups["qa_record"]["items"][0]["snippet"]

                # limit 只影响返回条数，不影响总数。
                capped = await client.get("admin/search", params={"q": "体温计", "limit": 1})
                capped_groups = {group["kind"]: group for group in capped.json()["groups"]}
                assert all(len(group["items"]) <= 1 for group in capped_groups.values())

                # % 被转义为普通字符，不会扫出发全表。
                wildcard = await client.get("admin/search", params={"q": "%"})
                assert wildcard.status_code == 200
                assert sum(group["total"] for group in wildcard.json()["groups"]) == 0

                # 空白检索词直接 422，避免无意义查询。
                blank = await client.get("admin/search", params={"q": "   "})
                assert blank.status_code == 422

                # 只读角色也能用全局搜索（全是读操作）。
                viewer = await client.get(
                    "admin/search",
                    params={"q": "体温计"},
                    headers={"Authorization": "Bearer viewer-token"},
                )
                assert viewer.status_code == 200, viewer.text
                assert sum(group["total"] for group in viewer.json()["groups"]) == 6
        finally:
            await engine.dispose()

    asyncio.run(scenario())
