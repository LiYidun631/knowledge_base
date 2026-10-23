"""管理员统计仪表盘接口验证。"""

import asyncio
from datetime import datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    Document,
    KnowledgeBase,
    ModelProfile,
    QARecord,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def _admin_stats_app(monkeypatch, settings, factory):
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


def test_http_admin_statistics(monkeypatch, tmp_path) -> None:
    """统计接口返回总量、状态分布与近 14 天趋势，缺失日期补零。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin_stats.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            async with session.begin():
                embedding = ModelProfile(
                    name="stats-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="stats-embedding",
                    dimensions=2,
                )
                active_company = Company(code="stats-a", name="统计甲公司")
                archived_company = Company(code="stats-b", name="统计乙公司", status="archived")
                session.add_all([embedding, active_company, archived_company])
                await session.flush()
                kb_a = KnowledgeBase(
                    company_id=active_company.id, name="甲库", embedding_model_id=embedding.id
                )
                kb_b = KnowledgeBase(
                    company_id=active_company.id, name="乙库", embedding_model_id=embedding.id
                )
                kb_archived = KnowledgeBase(
                    company_id=active_company.id,
                    name="归档库",
                    embedding_model_id=embedding.id,
                    status="archived",
                )
                session.add_all([kb_a, kb_b, kb_archived])
                await session.flush()
                documents = [
                    Document(
                        company_id=active_company.id,
                        knowledge_base_id=kb_a.id,
                        document_key="doc-1",
                        filename="已完成.md",
                        status="completed",
                    ),
                    Document(
                        company_id=active_company.id,
                        knowledge_base_id=kb_a.id,
                        document_key="doc-2",
                        filename="处理中.md",
                        status="processing",
                    ),
                    Document(
                        company_id=active_company.id,
                        knowledge_base_id=kb_b.id,
                        document_key="doc-3",
                        filename="失败.md",
                        status="failed",
                    ),
                ]
                session.add_all(documents)

                today_noon = datetime.utcnow().replace(
                    hour=12, minute=0, second=0, microsecond=0
                )
                records = [
                    QARecord(
                        company_id=active_company.id,
                        request_id="stats-req-1",
                        knowledge_base_id=kb_a.id,
                        query="今天的成功一问",
                        status="success",
                        review_status="none",
                        retrieval_mode="hybrid",
                        top_k=5,
                        include_context=True,
                        result_count=1,
                        total_duration_ms=100,
                        prompt_tokens=100,
                        completion_tokens=20,
                        total_tokens=120,
                        created_at=today_noon,
                    ),
                    QARecord(
                        company_id=active_company.id,
                        request_id="stats-req-2",
                        knowledge_base_id=kb_a.id,
                        query="今天的成功二问",
                        status="success",
                        review_status="none",
                        retrieval_mode="hybrid",
                        top_k=5,
                        include_context=True,
                        result_count=1,
                        total_duration_ms=300,
                        prompt_tokens=50,
                        completion_tokens=10,
                        total_tokens=60,
                        created_at=today_noon,
                    ),
                    QARecord(
                        company_id=active_company.id,
                        request_id="stats-req-3",
                        knowledge_base_id=kb_b.id,
                        query="今天未命中",
                        status="no_results",
                        review_status="pending",
                        retrieval_mode="bm25",
                        top_k=5,
                        include_context=True,
                        result_count=0,
                        created_at=today_noon,
                    ),
                    QARecord(
                        company_id=active_company.id,
                        request_id="stats-req-4",
                        knowledge_base_id=kb_b.id,
                        query="两天前失败",
                        status="failed",
                        review_status="pending",
                        retrieval_mode="vector",
                        top_k=3,
                        include_context=False,
                        result_count=0,
                        total_duration_ms=400,
                        error_code="CHAT_TIMEOUT",
                        prompt_tokens=30,
                        completion_tokens=5,
                        total_tokens=35,
                        created_at=today_noon - timedelta(days=2),
                    ),
                    QARecord(
                        company_id=active_company.id,
                        request_id="stats-req-5",
                        knowledge_base_id=kb_a.id,
                        query="二十天前的记录",
                        status="success",
                        review_status="none",
                        retrieval_mode="hybrid",
                        top_k=5,
                        include_context=True,
                        result_count=1,
                        prompt_tokens=999,
                        completion_tokens=111,
                        total_tokens=1110,
                        created_at=today_noon - timedelta(days=20),
                    ),
                ]
                session.add_all(records)

        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _admin_stats_app(monkeypatch, settings, factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
        ) as anonymous:
            response = await anonymous.get("admin/statistics")
            assert response.status_code == 401

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers={"Authorization": "Bearer admin-test-token"},
        ) as client:
            response = await client.get("admin/statistics")
            assert response.status_code == 200, response.text
            body = response.json()
            # 归档公司/库不计入活跃总量。
            assert body["companies_total"] == 1
            assert body["knowledge_bases_total"] == 2
            assert body["documents_total"] == 3
            assert body["documents_by_status"] == {
                "completed": 1,
                "processing": 1,
                "failed": 1,
            }
            assert body["qa_total"] == 5
            assert body["qa_by_status"] == {
                "success": 3,
                "no_results": 1,
                "failed": 1,
            }
            assert body["review_pending"] == 2

            token_usage = body["token_usage"]
            # 累计包含窗口外记录；近 14 天只算窗口内；未记录用量的问答不计入条数。
            assert token_usage == {
                "prompt_tokens": 1179,
                "completion_tokens": 146,
                "total_tokens": 1325,
                "recent_prompt_tokens": 180,
                "recent_completion_tokens": 35,
                "recent_total_tokens": 215,
                "measured_qa_count": 4,
            }

            trend = body["daily_trend"]
            assert len(trend) == 14
            today = today_noon.date().isoformat()
            two_days_ago = (today_noon - timedelta(days=2)).date().isoformat()
            assert trend[-1]["date"] == today
            assert trend[-1]["qa_total"] == 3
            assert trend[-1]["qa_success"] == 2
            assert trend[-1]["avg_total_duration_ms"] == 200
            assert trend[-1]["total_tokens"] == 180
            assert trend[-3]["date"] == two_days_ago
            assert trend[-3]["qa_total"] == 1
            assert trend[-3]["qa_success"] == 0
            assert trend[-3]["avg_total_duration_ms"] == 400
            assert trend[-3]["total_tokens"] == 35
            # 窗口外记录不进趋势；缺失日期补零。
            assert trend[0]["qa_total"] == 0
            assert sum(point["qa_total"] for point in trend) == 4

    asyncio.run(scenario())
