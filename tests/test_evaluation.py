"""检索评测集：用例匹配、指标汇总、运行流程、冲突与权限。"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.application import evaluation as evaluation_service
from knowledge_service.application.evaluation import (
    STALE_RUN_AFTER,
    evaluate_case,
    summarize_run,
)
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    CompanyCredential,
    EvaluationRun,
    KnowledgeBase,
    ModelProfile,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


@dataclass
class _Result:
    document_id: UUID | None
    content: str
    filename: str = "guide.txt"
    page_number: int | None = None
    section: str | None = None


def test_evaluate_case_matches_documents_and_keywords() -> None:
    """文档 ID 与关键词取「或」，召回率按期望文档覆盖比例计算。"""
    first, second, missing = uuid4(), uuid4(), uuid4()
    results = [
        _Result(document_id=first, content="无关内容"),
        _Result(document_id=second, content="保修期为两年"),
        _Result(document_id=first, content="同一文档的第二个片段"),
    ]

    outcome = evaluate_case(
        expected_document_ids=[str(first), str(second)],
        expected_keywords=[],
        results=results,
    )
    assert outcome["hit"] is True
    assert outcome["first_relevant_rank"] == 1
    assert outcome["recall"] == 1.0
    assert outcome["reciprocal_rank"] == 1.0
    assert [item["matched_by"] for item in outcome["matches"]] == [
        "document",
        "document",
        "document",
    ]

    keyword_outcome = evaluate_case(
        expected_document_ids=[],
        expected_keywords=["保修", "两年"],
        results=results,
    )
    assert keyword_outcome["first_relevant_rank"] == 2
    assert keyword_outcome["recall"] == 1.0
    assert keyword_outcome["reciprocal_rank"] == 0.5
    assert {item["matched_by"] for item in keyword_outcome["matches"]} == {
        None,
        "keyword",
    }

    miss = evaluate_case(
        expected_document_ids=[str(missing)],
        expected_keywords=["不存在"],
        results=results,
    )
    assert miss["hit"] is False
    assert miss["first_relevant_rank"] is None
    assert miss["recall"] == 0.0
    assert miss["reciprocal_rank"] == 0.0

    empty = evaluate_case(expected_document_ids=[str(first)], expected_keywords=[], results=[])
    assert empty["hit"] is False
    assert empty["matches"] == []
    assert empty["recall"] == 0.0

    partial = evaluate_case(
        expected_document_ids=[str(first), str(second), str(missing)],
        expected_keywords=[],
        results=results,
    )
    assert partial["recall"] == pytest.approx(0.6667, abs=1e-4)


def test_summarize_run_aggregates_metrics() -> None:
    """命中率、平均召回、MRR、无结果率与平均耗时按已完成用例汇总。"""
    metrics = summarize_run(
        [
            {"hit": True, "recall": 1.0, "reciprocal_rank": 1.0, "result_count": 3},
            {"hit": True, "recall": 0.5, "reciprocal_rank": 0.5, "result_count": 2},
            {"hit": False, "recall": 0.0, "reciprocal_rank": 0.0, "result_count": 0},
            {"hit": False, "recall": 0.0, "reciprocal_rank": 0.0, "result_count": 0},
        ],
        case_count=5,
        errored_count=1,
        latencies=[10, 20, 30, 40],
    )
    assert metrics["case_count"] == 5
    assert metrics["completed_count"] == 4
    assert metrics["errored_count"] == 1
    assert metrics["hit_rate"] == 0.5
    assert metrics["recall_at_k"] == 0.375
    assert metrics["mrr"] == 0.375
    assert metrics["no_result_rate"] == 0.5
    assert metrics["avg_latency_ms"] == 25

    empty = summarize_run([], case_count=0, errored_count=0, latencies=[])
    assert empty == {
        "case_count": 0,
        "completed_count": 0,
        "errored_count": 0,
        "hit_rate": 0.0,
        "recall_at_k": 0.0,
        "mrr": 0.0,
        "no_result_rate": 0.0,
        "avg_latency_ms": 0,
    }


async def _seed(factory) -> dict[str, Any]:
    async with factory() as session, session.begin():
        embedding = ModelProfile(
            name="evaluation-embedding",
            kind="embedding",
            provider="openai_compatible",
            model_name="evaluation-embedding",
            dimensions=2,
        )
        session.add(embedding)
        await session.flush()
        company = Company(code="alpha", name="alpha公司")
        session.add(company)
        await session.flush()
        session.add(
            CompanyCredential(
                company_id=company.id,
                name="默认凭据",
                token_hash=sha256(b"alpha-token").hexdigest(),
            )
        )
        knowledge_base = KnowledgeBase(
            company_id=company.id,
            name="评测库",
            embedding_model_id=embedding.id,
        )
        session.add(knowledge_base)
        await session.flush()
        return {
            "company_id": company.id,
            "kb_id": knowledge_base.id,
        }


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


async def _make_client(tmp_path, monkeypatch, name: str):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / name}", connect_args={"timeout": 10}
    )

    # SQLite 默认不执行外键动作；打开后与 MySQL 的级联删除行为一致。
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    scopes = await _seed(factory)
    settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
    app = _app(monkeypatch, settings, factory)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1/"
    )
    return engine, factory, scopes, client


async def _wait_for_run(client, run_id: UUID, headers: dict[str, str]) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + 20
    while True:
        response = await client.get(f"admin/evaluation-runs/{run_id}", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] != "running":
            return body
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("评测运行超时")
        await asyncio.sleep(0.05)


def test_evaluation_set_lifecycle_and_run(tmp_path, monkeypatch) -> None:
    """评测集与用例增删改，运行后产出指标、逐条结果与基线。"""
    admin_headers = {"Authorization": "Bearer admin-test-token"}
    document_id = uuid4()

    async def scenario() -> None:
        engine, factory, scopes, client = await _make_client(tmp_path, monkeypatch, "evaluation.db")
        calls: list[dict[str, Any]] = []

        async def fake_search(_session, **kwargs):
            calls.append(kwargs)
            if kwargs["query"] == "保修多久":
                return [_Result(document_id=uuid4(), content="整机保修期为两年")]
            return [
                _Result(document_id=uuid4(), content="无关内容"),
                _Result(document_id=document_id, content="清洁步骤：先擦拭"),
            ]

        monkeypatch.setattr(evaluation_service, "search_knowledge_base", fake_search)
        monkeypatch.setattr(evaluation_service, "get_session_factory", lambda: factory)
        try:
            async with client:
                response = await client.post(
                    "admin/evaluation-sets",
                    headers=admin_headers,
                    json={
                        "knowledge_base_id": str(scopes["kb_id"]),
                        "name": "回归评测",
                        "description": "每周回归",
                    },
                )
                assert response.status_code == 201, response.text
                evaluation_set = response.json()
                set_id = evaluation_set["id"]
                assert evaluation_set["case_count"] == 0

                response = await client.post(
                    "admin/evaluation-sets",
                    headers=admin_headers,
                    json={
                        "knowledge_base_id": str(scopes["kb_id"]),
                        "name": "回归评测",
                    },
                )
                assert response.status_code == 409

                response = await client.post(
                    f"admin/evaluation-sets/{set_id}/cases",
                    headers=admin_headers,
                    json={"query": "没有期望"},
                )
                assert response.status_code == 422

                response = await client.post(
                    f"admin/evaluation-sets/{set_id}/cases",
                    headers=admin_headers,
                    json={
                        "query": "怎么清洁",
                        "expected_document_ids": [str(document_id)],
                        "retrieval_mode": "bm25",
                        "top_k": 3,
                    },
                )
                assert response.status_code == 201, response.text
                document_case_id = response.json()["id"]

                response = await client.post(
                    f"admin/evaluation-sets/{set_id}/cases/bulk",
                    headers=admin_headers,
                    json={
                        "items": [
                            {
                                "query": "保修多久",
                                "expected_keywords": ["保修", "两年"],
                                "retrieval_mode": "bm25",
                            },
                            {
                                "query": "停用用例",
                                "expected_keywords": ["停用"],
                                "is_enabled": False,
                            },
                        ]
                    },
                )
                assert response.status_code == 201, response.text
                keyword_case = response.json()[0]

                response = await client.get(
                    f"admin/evaluation-sets/{set_id}", headers=admin_headers
                )
                assert response.status_code == 200
                body = response.json()
                assert body["set"]["case_count"] == 3
                assert body["set"]["enabled_case_count"] == 2
                assert len(body["items"]) == 3

                response = await client.patch(
                    f"admin/evaluation-cases/{document_case_id}",
                    headers=admin_headers,
                    json={"expected_document_ids": [], "expected_keywords": []},
                )
                assert response.status_code == 422

                response = await client.patch(
                    f"admin/evaluation-cases/{keyword_case['id']}",
                    headers=admin_headers,
                    json={"top_k": 4, "notes": "  高频问题  "},
                )
                assert response.status_code == 200
                assert response.json()["top_k"] == 4
                assert response.json()["notes"] == "高频问题"

                response = await client.post(
                    f"admin/evaluation-sets/{set_id}/run", headers=admin_headers
                )
                assert response.status_code == 202, response.text
                first_run_id = response.json()["id"]
                detail = await _wait_for_run(client, UUID(first_run_id), admin_headers)
                assert detail["status"] == "success", detail
                assert detail["case_count"] == 2
                assert detail["completed_count"] == 2
                assert detail["metrics"]["hit_rate"] == 1.0
                assert detail["metrics"]["recall_at_k"] == 1.0
                assert detail["metrics"]["mrr"] == 0.75
                assert detail["metrics"]["no_result_rate"] == 0.0
                assert detail["set_name"] == "回归评测"
                assert detail["knowledge_base_name"] == "评测库"
                assert detail["baseline"] is None
                assert len(detail["results"]) == 2
                assert all(result["error_message"] is None for result in detail["results"])
                document_result = next(
                    result for result in detail["results"] if result["query"] == "怎么清洁"
                )
                assert document_result["first_relevant_rank"] == 2
                assert document_result["matches"][1]["matched_by"] == "document"
                assert len(calls) == 2
                assert all(call["company_id"] is None for call in calls)
                assert {call["retrieval_mode"] for call in calls} == {"bm25"}

                response = await client.post(
                    f"admin/evaluation-sets/{set_id}/run", headers=admin_headers
                )
                assert response.status_code == 202
                second_run_id = response.json()["id"]
                second = await _wait_for_run(client, UUID(second_run_id), admin_headers)
                assert second["baseline"]["id"] == first_run_id
                assert second["baseline"]["metrics"]["mrr"] == 0.75

                response = await client.get(
                    "admin/evaluation-runs",
                    headers=admin_headers,
                    params={"set_id": set_id, "status": "success"},
                )
                assert response.status_code == 200
                assert response.json()["total"] == 2

                response = await client.delete(
                    f"admin/evaluation-runs/{second_run_id}", headers=admin_headers
                )
                assert response.status_code == 204

                response = await client.delete(
                    f"admin/evaluation-sets/{set_id}", headers=admin_headers
                )
                assert response.status_code == 204
                response = await client.get(
                    "admin/evaluation-runs",
                    headers=admin_headers,
                    params={"set_id": set_id},
                )
                assert response.status_code == 200
                assert response.json()["total"] == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_evaluation_run_conflicts_permissions_and_cleanup(tmp_path, monkeypatch) -> None:
    """同一库并发运行、归档库、无效评测集与公司凭据访问都应被拒绝。"""
    admin_headers = {"Authorization": "Bearer admin-test-token"}
    company_headers = {"Authorization": "Bearer alpha-token"}

    async def scenario() -> None:
        engine, factory, scopes, client = await _make_client(
            tmp_path, monkeypatch, "evaluation-conflicts.db"
        )

        async def fake_search(_session, **kwargs):
            return [_Result(document_id=uuid4(), content="命中关键词")]

        monkeypatch.setattr(evaluation_service, "search_knowledge_base", fake_search)
        monkeypatch.setattr(evaluation_service, "get_session_factory", lambda: factory)
        try:
            async with client:
                response = await client.post(
                    "admin/evaluation-sets",
                    headers=admin_headers,
                    json={
                        "knowledge_base_id": str(scopes["kb_id"]),
                        "name": "冲突评测",
                    },
                )
                assert response.status_code == 201
                set_id = UUID(response.json()["id"])

                response = await client.post(
                    "admin/evaluation-sets",
                    headers=admin_headers,
                    json={
                        "knowledge_base_id": str(scopes["kb_id"]),
                        "name": "空评测",
                    },
                )
                assert response.status_code == 201
                empty_set_id = response.json()["id"]

                response = await client.post(
                    f"admin/evaluation-sets/{empty_set_id}/run", headers=admin_headers
                )
                assert response.status_code == 422

                response = await client.post(
                    f"admin/evaluation-sets/{uuid4()}/run", headers=admin_headers
                )
                assert response.status_code == 404

                response = await client.post(
                    f"admin/evaluation-sets/{set_id}/cases",
                    headers=admin_headers,
                    json={"query": "关键词问题", "expected_keywords": ["关键词"]},
                )
                assert response.status_code == 201

                async with factory() as session, session.begin():
                    running = EvaluationRun(
                        set_id=set_id,
                        knowledge_base_id=scopes["kb_id"],
                        company_id=scopes["company_id"],
                        status="running",
                        case_count=1,
                        completed_count=0,
                        metrics={},
                        started_at=utc_now(),
                    )
                    session.add(running)
                    await session.flush()
                    running_id = running.id

                response = await client.post(
                    f"admin/evaluation-sets/{set_id}/run", headers=admin_headers
                )
                assert response.status_code == 409

                response = await client.delete(
                    f"admin/evaluation-runs/{running_id}", headers=admin_headers
                )
                assert response.status_code == 409

                # 超时运行会被自动标记失败，并允许启动新的运行。
                async with factory() as session, session.begin():
                    stale = await session.get(EvaluationRun, running_id)
                    stale.started_at = utc_now() - STALE_RUN_AFTER - timedelta(minutes=1)

                response = await client.post(
                    f"admin/evaluation-sets/{set_id}/run", headers=admin_headers
                )
                assert response.status_code == 202
                await _wait_for_run(client, UUID(response.json()["id"]), admin_headers)

                response = await client.get(
                    f"admin/evaluation-runs/{running_id}", headers=admin_headers
                )
                assert response.status_code == 200
                assert response.json()["status"] == "failed"
                assert "自动终止" in response.json()["error_message"]

                # 公司凭据不能访问管理端评测接口。
                response = await client.get("admin/evaluation-sets", headers=company_headers)
                assert response.status_code == 403
                response = await client.post(
                    f"admin/evaluation-sets/{set_id}/run", headers=company_headers
                )
                assert response.status_code == 403

                async with factory() as session, session.begin():
                    knowledge_base = await session.get(KnowledgeBase, scopes["kb_id"])
                    knowledge_base.status = "archived"

                response = await client.post(
                    f"admin/evaluation-sets/{set_id}/run", headers=admin_headers
                )
                assert response.status_code == 409
                assert "归档" in response.json()["detail"]

                response = await client.delete(
                    f"admin/evaluation-sets/{set_id}", headers=admin_headers
                )
                assert response.status_code == 204
                response = await client.get(
                    f"admin/evaluation-sets/{set_id}", headers=admin_headers
                )
                assert response.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(scenario())
