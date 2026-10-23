"""管理员问答记录列表与详情接口验证。"""

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    Document,
    DocumentAsset,
    DocumentVersion,
    KnowledgeBase,
    ModelProfile,
    QAEvent,
    QARecord,
    QASource,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def _admin_qa_app(monkeypatch, settings, factory):
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


def test_http_admin_qa_records_list_and_detail(monkeypatch, tmp_path) -> None:
    """管理员可跨公司分页查询问答记录，支持状态/公司/库/关键词/时间过滤。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin_qa.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            async with session.begin():
                embedding = ModelProfile(
                    name="qa-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="qa-embedding",
                    dimensions=2,
                )
                company_a = Company(code="qa-a", name="问答甲公司")
                company_b = Company(code="qa-b", name="问答乙公司")
                session.add_all([embedding, company_a, company_b])
                await session.flush()
                kb_a = KnowledgeBase(
                    company_id=company_a.id, name="甲库", embedding_model_id=embedding.id
                )
                kb_b = KnowledgeBase(
                    company_id=company_b.id, name="乙库", embedding_model_id=embedding.id
                )
                session.add_all([kb_a, kb_b])
                await session.flush()
                base = datetime(2026, 10, 1, 8, 0)
                records = [
                    QARecord(
                        company_id=company_a.id,
                        request_id=str(uuid4()),
                        knowledge_base_id=kb_a.id,
                        query="如何退款？",
                        status="success",
                        review_status="none",
                        faq_matched=True,
                        retrieval_mode="hybrid",
                        top_k=5,
                        include_context=True,
                        result_count=2,
                        created_at=base,
                    ),
                    QARecord(
                        company_id=company_a.id,
                        request_id=str(uuid4()),
                        knowledge_base_id=kb_a.id,
                        query="保修期多久？",
                        status="no_results",
                        review_status="pending",
                        retrieval_mode="bm25",
                        top_k=5,
                        include_context=True,
                        result_count=0,
                        created_at=base + timedelta(days=1),
                    ),
                    QARecord(
                        company_id=company_b.id,
                        request_id=str(uuid4()),
                        knowledge_base_id=kb_b.id,
                        query="体温计怎么校准？",
                        status="failed",
                        review_status="pending",
                        retrieval_mode="vector",
                        top_k=3,
                        include_context=False,
                        result_count=0,
                        error_code="UpstreamTimeout",
                        created_at=base + timedelta(days=2),
                    ),
                ]
                session.add_all(records)
                await session.flush()
                first = records[0]
                session.add(
                    QASource(
                        company_id=company_a.id,
                        qa_record_id=first.id,
                        rank=1,
                        filename="退款说明.md",
                        version_number=1,
                        content_snapshot="七天无理由退款。",
                        rerank_score=0.92,
                    )
                )
                session.add(
                    QAEvent(
                        company_id=company_a.id,
                        qa_record_id=first.id,
                        request_id=first.request_id,
                        stage="retrieval",
                        status="success",
                        duration_ms=12,
                        message="召回 2 条",
                    )
                )

        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _admin_qa_app(monkeypatch, settings, factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
        ) as anonymous:
            response = await anonymous.get("admin/qa-records")
            assert response.status_code == 401

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers={"Authorization": "Bearer admin-test-token"},
        ) as client:
            response = await client.get("admin/qa-records")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["total"] == 3
            assert [item["status"] for item in body["items"]] == [
                "failed",
                "no_results",
                "success",
            ]
            # 附带公司与知识库名称，供管理页直接展示。
            assert body["items"][0]["company_name"] == "问答乙公司"
            assert body["items"][0]["knowledge_base_name"] == "乙库"
            assert body["items"][2]["company_name"] == "问答甲公司"
            assert body["items"][2]["knowledge_base_name"] == "甲库"

            response = await client.get("admin/qa-records", params={"status": "no_results"})
            body = response.json()
            assert body["total"] == 1
            assert body["items"][0]["query"] == "保修期多久？"

            response = await client.get("admin/qa-records", params={"q": "退款"})
            body = response.json()
            assert body["total"] == 1
            assert body["items"][0]["status"] == "success"

            # FAQ 直出过滤：默认记录未命中，只有显式标记的条目返回。
            response = await client.get(
                "admin/qa-records", params={"faq_matched": "true"}
            )
            body = response.json()
            assert body["total"] == 1
            assert body["items"][0]["query"] == "如何退款？"
            assert body["items"][0]["faq_matched"] is True
            response = await client.get(
                "admin/qa-records", params={"faq_matched": "false"}
            )
            assert response.json()["total"] == 2

            response = await client.get(
                "admin/qa-records", params={"company_id": str(company_b.id)}
            )
            body = response.json()
            assert body["total"] == 1
            assert body["items"][0]["query"] == "体温计怎么校准？"

            response = await client.get(
                "admin/qa-records", params={"knowledge_base_id": str(kb_a.id)}
            )
            assert response.json()["total"] == 2

            response = await client.get(
                "admin/qa-records",
                params={
                    "start_time": (base + timedelta(hours=12)).isoformat(),
                    "end_time": (base + timedelta(days=1, hours=12)).isoformat(),
                },
            )
            body = response.json()
            assert body["total"] == 1
            assert body["items"][0]["query"] == "保修期多久？"

            response = await client.get(
                "admin/qa-records", params={"page": 1, "page_size": 2}
            )
            body = response.json()
            assert body["total"] == 3
            assert len(body["items"]) == 2
            response = await client.get(
                "admin/qa-records", params={"page": 2, "page_size": 2}
            )
            assert len(response.json()["items"]) == 1

            response = await client.get(f"admin/qa-records/{first.id}")
            assert response.status_code == 200, response.text
            detail = response.json()
            assert detail["query"] == "如何退款？"
            assert len(detail["sources"]) == 1
            assert detail["sources"][0]["rerank_score"] == 0.92
            assert len(detail["events"]) == 1
            assert detail["events"][0]["stage"] == "retrieval"

            response = await client.get(f"admin/qa-records/{uuid4()}")
            assert response.status_code == 404

            # 待补资料列表：未命中与失败记录自动进入。
            response = await client.get("admin/qa-records", params={"review_status": "pending"})
            body = response.json()
            assert body["total"] == 2
            assert {item["query"] for item in body["items"]} == {"保修期多久？", "体温计怎么校准？"}

            # 标记已补充后从待补资料中消失；可重新打开。
            pending_id = records[1].id
            response = await client.patch(
                f"admin/qa-records/{pending_id}/review",
                json={"review_status": "resolved"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["review_status"] == "resolved"
            assert response.json()["company_name"] == "问答甲公司"
            response = await client.get("admin/qa-records", params={"review_status": "pending"})
            assert response.json()["total"] == 1
            response = await client.patch(
                f"admin/qa-records/{pending_id}/review",
                json={"review_status": "none"},
            )
            assert response.status_code == 200
            assert response.json()["review_status"] == "none"

            # 非法复核状态与不存在的记录。
            response = await client.patch(
                f"admin/qa-records/{pending_id}/review",
                json={"review_status": "pending"},
            )
            assert response.status_code == 422
            response = await client.patch(
                f"admin/qa-records/{uuid4()}/review",
                json={"review_status": "resolved"},
            )
            assert response.status_code == 404

    asyncio.run(scenario())


def test_http_admin_qa_record_snapshot_renders_asset_markers(monkeypatch, tmp_path) -> None:
    """详情接口把快照中的 [[KB_ASSET_KEY:...]] 占位符替换成图片 Markdown URL。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin_qa_asset.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            async with session.begin():
                embedding = ModelProfile(
                    name="qa-asset-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="qa-asset-embedding",
                    dimensions=2,
                )
                company = Company(code="qa-asset", name="图片公司")
                session.add_all([embedding, company])
                await session.flush()
                kb = KnowledgeBase(
                    company_id=company.id, name="图片库", embedding_model_id=embedding.id
                )
                session.add(kb)
                await session.flush()
                document = Document(
                    company_id=company.id,
                    knowledge_base_id=kb.id,
                    document_key="asset-doc",
                    filename="说明书.docx",
                    latest_version_number=1,
                )
                session.add(document)
                await session.flush()
                version = DocumentVersion(
                    document_id=document.id,
                    version_number=1,
                    filename=document.filename,
                    size_bytes=1,
                    content_sha256="0" * 64,
                    object_key="asset-doc.docx",
                )
                session.add(version)
                await session.flush()
                asset = DocumentAsset(
                    document_id=document.id,
                    document_version_id=version.id,
                    object_key="assets/doc/1.png",
                    media_type="image/png",
                    marker="[[KB_ASSET_KEY:assets/doc/1.png]]",
                )
                record = QARecord(
                    company_id=company.id,
                    knowledge_base_id=kb.id,
                    request_id=str(uuid4()),
                    query="图片表格",
                    status="success",
                    review_status="none",
                    retrieval_mode="hybrid",
                    top_k=5,
                    include_context=True,
                    result_count=1,
                )
                session.add_all([asset, record])
                await session.flush()
                session.add(
                    QASource(
                        company_id=company.id,
                        qa_record_id=record.id,
                        rank=1,
                        document_id=document.id,
                        document_version_id=version.id,
                        filename="说明书.docx",
                        version_number=1,
                        content_snapshot="步骤一：[[KB_ASSET_KEY:assets/doc/1.png]] 之后继续。",
                        asset_ids=[str(asset.id)],
                    )
                )
            source_id = asset.id

        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _admin_qa_app(monkeypatch, settings, factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers={"Authorization": "Bearer admin-test-token"},
        ) as client:
            response = await client.get(f"admin/qa-records/{record.id}")
            assert response.status_code == 200, response.text
            content = response.json()["sources"][0]["content_snapshot"]
            assert "[[KB_ASSET" not in content
            assert f"document-assets/{source_id}" in content
            assert content.startswith("步骤一：![")

    asyncio.run(scenario())
