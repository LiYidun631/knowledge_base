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

                today_noon = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
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
                        retrieval_duration_ms=30,
                        generation_duration_ms=60,
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
                        retrieval_duration_ms=50,
                        generation_duration_ms=200,
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
                        retrieval_duration_ms=70,
                        generation_duration_ms=300,
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
                    QARecord(
                        company_id=active_company.id,
                        request_id="stats-req-6",
                        knowledge_base_id=kb_a.id,
                        query="FAQ 直出一问",
                        status="success",
                        review_status="none",
                        retrieval_mode="hybrid",
                        top_k=5,
                        include_context=True,
                        faq_matched=True,
                        result_count=2,
                        retrieval_duration_ms=10,
                        generation_duration_ms=5,
                        total_duration_ms=50,
                        created_at=today_noon,
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
            assert body["qa_total"] == 6
            assert body["qa_by_status"] == {
                "success": 4,
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
            assert trend[-1]["qa_total"] == 4
            assert trend[-1]["qa_success"] == 3
            # 趋势仍按全部记录聚合（含 FAQ 直出），(100+300+50)/3。
            assert trend[-1]["avg_total_duration_ms"] == 150
            assert trend[-1]["total_tokens"] == 180
            assert trend[-3]["date"] == two_days_ago
            assert trend[-3]["qa_total"] == 1
            assert trend[-3]["qa_success"] == 0
            assert trend[-3]["avg_total_duration_ms"] == 400
            assert trend[-3]["total_tokens"] == 35
            # 窗口外记录不进趋势；缺失日期补零。
            assert trend[0]["qa_total"] == 0
            assert sum(point["qa_total"] for point in trend) == 5

            recent = body["recent"]
            # 窗口内 5 条（今日 4 + 两天前 1），其中 1 条 FAQ 直出；质量计数包含它。
            assert recent["qa_total"] == 5
            assert recent["qa_success"] == 3
            assert recent["qa_no_results"] == 1
            assert recent["qa_failed"] == 1
            assert recent["failure_rate"] == 20.0
            assert recent["no_result_rate"] == 20.0

            latency = recent["latency"]
            # FAQ 直出（50ms）不纳入耗时统计，只剩 3 条记录的耗时；P50/P90 用最近秩法。
            assert latency["sample_count"] == 3
            assert latency["retrieval_p50_ms"] == 50
            assert latency["retrieval_p90_ms"] == 70
            assert latency["generation_p50_ms"] == 200
            assert latency["generation_p90_ms"] == 300
            assert latency["total_p50_ms"] == 300
            assert latency["total_p90_ms"] == 400
            assert latency["total_max_ms"] == 400

            # FAQ 直出同样不参与分组；其余记录都没写 chat_model_name，归入「未记录模型」。
            assert len(recent["models"]) == 1
            model_group = recent["models"][0]
            assert model_group["key"] == ""
            assert model_group["label"] == "未记录模型"
            assert model_group["qa_total"] == 4
            assert model_group["qa_failed"] == 1
            assert model_group["failure_rate"] == 25.0
            assert model_group["avg_total_duration_ms"] == 267
            assert model_group["p90_total_duration_ms"] == 400

            kb_groups = {item["label"]: item for item in recent["knowledge_bases"]}
            assert set(kb_groups) == {"甲库", "乙库"}
            assert kb_groups["甲库"]["qa_total"] == 2
            assert kb_groups["甲库"]["failure_rate"] == 0.0
            assert kb_groups["甲库"]["avg_total_duration_ms"] == 200
            assert kb_groups["甲库"]["p90_total_duration_ms"] == 300
            assert kb_groups["乙库"]["qa_total"] == 2
            assert kb_groups["乙库"]["failure_rate"] == 50.0
            # 未命中记录没有总耗时，只按已记录的 400ms 计算平均与 P90。
            assert kb_groups["乙库"]["avg_total_duration_ms"] == 400
            assert kb_groups["乙库"]["p90_total_duration_ms"] == 400

            # 默认窗口为最近 14 天（含今天）。
            assert body["range"] == {
                "start": (today_noon - timedelta(days=13)).date().isoformat(),
                "end": today,
                "days": 14,
            }

            # 自定义窗口只看今天：趋势与概览收窄，累计 token 不受影响。
            response = await client.get("admin/statistics", params={"start": today, "end": today})
            assert response.status_code == 200, response.text
            one_day = response.json()
            assert one_day["range"] == {"start": today, "end": today, "days": 1}
            assert len(one_day["daily_trend"]) == 1
            assert one_day["daily_trend"][0]["qa_total"] == 4
            assert one_day["daily_trend"][0]["prompt_tokens"] == 150
            assert one_day["daily_trend"][0]["completion_tokens"] == 30
            assert one_day["recent"]["qa_total"] == 4
            assert one_day["recent"]["latency"]["sample_count"] == 2
            assert one_day["token_usage"]["recent_total_tokens"] == 180
            assert one_day["token_usage"]["total_tokens"] == 1325

            # 起止颠倒、窗口超长都直接拒绝。
            reversed_window = await client.get(
                "admin/statistics", params={"start": today, "end": two_days_ago}
            )
            assert reversed_window.status_code == 400
            too_long = await client.get(
                "admin/statistics",
                params={
                    "start": (today_noon - timedelta(days=400)).date().isoformat(),
                    "end": today,
                },
            )
            assert too_long.status_code == 400

    asyncio.run(scenario())


def test_http_admin_statistics_empty_recent(monkeypatch, tmp_path) -> None:
    """无问答记录时，近 14 天耗时与分组返回空值而不是报错。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin_stats_empty.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _admin_stats_app(monkeypatch, settings, factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers={"Authorization": "Bearer admin-test-token"},
        ) as client:
            response = await client.get("admin/statistics")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["qa_total"] == 0
            assert body["range"]["days"] == 14
            assert len(body["daily_trend"]) == 14
            assert body["daily_trend"][0]["prompt_tokens"] == 0
            assert body["recent"] == {
                "qa_total": 0,
                "qa_success": 0,
                "qa_no_results": 0,
                "qa_failed": 0,
                "failure_rate": 0.0,
                "no_result_rate": 0.0,
                "latency": {
                    "sample_count": 0,
                    "retrieval_p50_ms": None,
                    "retrieval_p90_ms": None,
                    "generation_p50_ms": None,
                    "generation_p90_ms": None,
                    "total_p50_ms": None,
                    "total_p90_ms": None,
                    "total_max_ms": None,
                },
                "models": [],
                "knowledge_bases": [],
            }
            # 无记录且未配置单价时成本区块全零。
            assert body["cost"] == {
                "currency": "CNY",
                "recent_cost": 0.0,
                "total_cost": 0.0,
                "recent_priced_tokens": 0,
                "recent_unpriced_tokens": 0,
                "unpriced_models": [],
                "models": [],
            }

    asyncio.run(scenario())
