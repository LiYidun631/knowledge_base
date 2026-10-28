"""生成模型单价管理与统计成本核算验证。"""

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    KnowledgeBase,
    ModelProfile,
    QARecord,
)
from knowledge_service.infrastructure.persistence.session import get_db_session

AUTH = {"Authorization": "Bearer admin-test-token"}


def _app(monkeypatch, settings, factory):
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


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test/api/v1/",
        headers=AUTH,
    )


def test_http_model_prices_crud(monkeypatch, tmp_path) -> None:
    """单价接口支持新增、按模型名覆盖、列表与删除，并拒绝非法数值。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'prices.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _app(monkeypatch, settings, factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
        ) as anonymous:
            assert (await anonymous.get("admin/model-prices")).status_code == 401

        async with _client(app) as client:
            response = await client.get("admin/model-prices")
            assert response.status_code == 200
            assert response.json() == []

            created = await client.put(
                "admin/model-prices",
                json={
                    "model_name": "qwen3.8-flash",
                    "input_price_per_million": 0.15,
                    "output_price_per_million": 0.6,
                },
            )
            assert created.status_code == 200, created.text
            created_body = created.json()
            assert created_body["model_name"] == "qwen3.8-flash"
            assert created_body["provider"] is None
            assert created_body["input_price_per_million"] == 0.15
            assert created_body["output_price_per_million"] == 0.6

            # 同名再次提交即覆盖，不会产生第二条记录。
            updated = await client.put(
                "admin/model-prices",
                json={
                    "model_name": "qwen3.8-flash",
                    "provider": "dashscope",
                    "input_price_per_million": 2,
                    "output_price_per_million": 6,
                },
            )
            assert updated.status_code == 200, updated.text
            updated_body = updated.json()
            assert updated_body["id"] == created_body["id"]
            assert updated_body["provider"] == "dashscope"
            assert updated_body["input_price_per_million"] == 2.0
            assert updated_body["output_price_per_million"] == 6.0

            listed = await client.get("admin/model-prices")
            assert [item["model_name"] for item in listed.json()] == ["qwen3.8-flash"]

            negative = await client.put(
                "admin/model-prices",
                json={
                    "model_name": "broken",
                    "input_price_per_million": -1,
                    "output_price_per_million": 1,
                },
            )
            assert negative.status_code == 422
            too_precise = await client.put(
                "admin/model-prices",
                json={
                    "model_name": "broken",
                    "input_price_per_million": 1.1234567,
                    "output_price_per_million": 1,
                },
            )
            assert too_precise.status_code == 422

            missing = await client.delete(f"admin/model-prices/{uuid4()}")
            assert missing.status_code == 404
            deleted = await client.delete(f"admin/model-prices/{created_body['id']}")
            assert deleted.status_code == 204
            assert (await client.get("admin/model-prices")).json() == []

    asyncio.run(scenario())


def test_http_statistics_cost(monkeypatch, tmp_path) -> None:
    """成本按模型单价核算：窗口与累计分开，未定价模型单独提示，FAQ 直出不计。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cost.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            async with session.begin():
                embedding = ModelProfile(
                    name="cost-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="cost-embedding",
                    dimensions=2,
                )
                company = Company(code="cost-a", name="成本甲公司")
                session.add_all([embedding, company])
                await session.flush()
                kb = KnowledgeBase(
                    company_id=company.id, name="成本库", embedding_model_id=embedding.id
                )
                session.add(kb)
                await session.flush()

                today_noon = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)

                def record(request_id: str, **kwargs) -> QARecord:
                    payload = {
                        "company_id": company.id,
                        "request_id": request_id,
                        "knowledge_base_id": kb.id,
                        "query": f"成本用例 {request_id}",
                        "status": "success",
                        "review_status": "none",
                        "retrieval_mode": "hybrid",
                        "top_k": 5,
                        "include_context": True,
                        "result_count": 1,
                        "created_at": today_noon,
                    }
                    payload.update(kwargs)
                    return QARecord(**payload)

                session.add_all(
                    [
                        record(
                            "cost-1",
                            chat_provider="dashscope",
                            chat_model_name="qwen3.8-flash",
                            prompt_tokens=1_000_000,
                            completion_tokens=500_000,
                            total_tokens=1_500_000,
                        ),
                        record(
                            "cost-2",
                            chat_provider="other",
                            chat_model_name="mystery-model",
                            prompt_tokens=100,
                            completion_tokens=100,
                            total_tokens=200,
                        ),
                        record(
                            "cost-3",
                            chat_provider="dashscope",
                            chat_model_name="qwen3.8-flash",
                            prompt_tokens=200_000,
                            completion_tokens=0,
                            total_tokens=200_000,
                            created_at=today_noon - timedelta(days=20),
                        ),
                        # FAQ 直出没有 token 记录，不应计入成本或用量。
                        record(
                            "cost-4",
                            status="no_results",
                            faq_matched=True,
                            result_count=2,
                        ),
                    ]
                )

        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _app(monkeypatch, settings, factory)
        async with _client(app) as client:
            await client.put(
                "admin/model-prices",
                json={
                    "model_name": "qwen3.8-flash",
                    "provider": "dashscope",
                    "input_price_per_million": 2,
                    "output_price_per_million": 6,
                },
            )

            response = await client.get("admin/statistics")
            assert response.status_code == 200, response.text
            cost = response.json()["cost"]
            assert cost["currency"] == "CNY"
            # 窗口内：100 万输入 * 2 元 + 50 万输出 * 6 元 = 5 元；10 天外部分只进累计。
            assert cost["recent_cost"] == 5.0
            assert cost["total_cost"] == 5.4
            assert cost["recent_priced_tokens"] == 1_500_000
            assert cost["recent_unpriced_tokens"] == 200
            assert cost["unpriced_models"] == ["mystery-model"]
            assert [item["model_name"] for item in cost["models"]] == [
                "qwen3.8-flash",
                "mystery-model",
            ]
            priced, unpriced = cost["models"]
            assert priced["priced"] is True
            assert priced["qa_count"] == 1
            assert priced["input_cost"] == 2.0
            assert priced["output_cost"] == 3.0
            assert priced["total_cost"] == 5.0
            assert unpriced["priced"] is False
            assert unpriced["qa_count"] == 1
            assert unpriced["prompt_tokens"] == 100
            assert unpriced["total_cost"] == 0.0

            # 窗口收窄到今天：累计金额不受影响。
            today = today_noon.date().isoformat()
            one_day = await client.get("admin/statistics", params={"start": today, "end": today})
            one_day_cost = one_day.json()["cost"]
            assert one_day_cost["recent_cost"] == 5.0
            assert one_day_cost["total_cost"] == 5.4

            # 删除单价后不再核算，token 仍在未定价统计里。
            price_id = (await client.get("admin/model-prices")).json()[0]["id"]
            await client.delete(f"admin/model-prices/{price_id}")
            cleared = (await client.get("admin/statistics")).json()["cost"]
            assert cleared["recent_cost"] == 0.0
            assert cleared["total_cost"] == 0.0
            assert cleared["recent_priced_tokens"] == 0
            assert cleared["recent_unpriced_tokens"] == 1_500_200
            assert cleared["unpriced_models"] == ["mystery-model", "qwen3.8-flash"]

    asyncio.run(scenario())
