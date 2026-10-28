"""问答反馈：点踩进入待补资料、撤销、公司隔离与项目轨迹回传。"""

import asyncio
from hashlib import sha256

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    AppProject,
    Company,
    CompanyCredential,
    KnowledgeBase,
    ModelProfile,
    ProjectQaTrace,
    QARecord,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


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


async def _seed(factory) -> dict:
    async with factory() as session:
        async with session.begin():
            embedding = ModelProfile(
                name="feedback-embedding",
                kind="embedding",
                provider="openai_compatible",
                model_name="feedback-embedding",
                dimensions=2,
            )
            session.add(embedding)
            await session.flush()
            scopes = {}
            for code in ("alpha", "beta"):
                company = Company(code=code, name=f"{code}公司")
                session.add(company)
                await session.flush()
                session.add(
                    CompanyCredential(
                        company_id=company.id,
                        name="默认凭据",
                        token_hash=sha256(f"{code}-token".encode()).hexdigest(),
                    )
                )
                kb = KnowledgeBase(
                    company_id=company.id,
                    name=f"{code}-库",
                    embedding_model_id=embedding.id,
                )
                session.add(kb)
                await session.flush()
                scopes[code] = {"company_id": company.id, "kb_id": kb.id}
            project = AppProject(
                project_key="test_agent",
                display_name="健康管家",
                retention_days=90,
            )
            session.add(project)
            await session.flush()
            alpha = scopes["alpha"]
            open_record = QARecord(
                company_id=alpha["company_id"],
                request_id="feedback-open",
                knowledge_base_id=alpha["kb_id"],
                query="如何清洁？",
                status="success",
                review_status="none",
                retrieval_mode="hybrid",
                top_k=5,
                include_context=True,
            )
            resolved_record = QARecord(
                company_id=alpha["company_id"],
                request_id="feedback-resolved",
                knowledge_base_id=alpha["kb_id"],
                query="保修多久？",
                status="success",
                review_status="resolved",
                retrieval_mode="hybrid",
                top_k=5,
                include_context=True,
            )
            beta = scopes["beta"]
            foreign_record = QARecord(
                company_id=beta["company_id"],
                request_id="feedback-foreign",
                knowledge_base_id=beta["kb_id"],
                query="其他公司的问题",
                status="success",
                review_status="none",
                retrieval_mode="hybrid",
                top_k=5,
                include_context=True,
            )
            trace = ProjectQaTrace(
                company_id=alpha["company_id"],
                project_id=project.id,
                knowledge_base_id=alpha["kb_id"],
                external_request_id="trace-feedback-1",
                query="清洁步骤",
                status="success",
                include_context=True,
            )
            session.add_all([open_record, resolved_record, foreign_record, trace])
            await session.flush()
            scopes["project_id"] = project.id
            scopes["open_record_id"] = open_record.id
            scopes["resolved_record_id"] = resolved_record.id
            scopes["foreign_record_id"] = foreign_record.id
            scopes["trace_id"] = trace.id
            return scopes


def test_qa_feedback_marks_pending_and_can_be_cleared(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'feedback.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        scopes = await _seed(factory)
        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _app(monkeypatch, settings, factory)
        alpha = scopes["alpha"]
        company_headers = {"Authorization": "Bearer alpha-token"}
        admin_headers = {"Authorization": "Bearer admin-test-token"}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1/"
        ) as client:
            response = await client.put(
                f"qa-records/{scopes['open_record_id']}/feedback",
                params={"company_id": str(alpha["company_id"])},
                headers=company_headers,
                json={"feedback": "down", "reason": "答非所问"},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["feedback"] == "down"
            assert body["feedback_reason"] == "答非所问"
            assert body["feedback_at"] is not None
            assert body["review_status"] == "pending"

            # 已处理记录不会被用户点踩改回待补资料。
            response = await client.put(
                f"qa-records/{scopes['resolved_record_id']}/feedback",
                params={"company_id": str(alpha["company_id"])},
                headers=company_headers,
                json={"feedback": "down"},
            )
            assert response.status_code == 200
            assert response.json()["review_status"] == "resolved"

            # 跨公司记录对普通公司凭据不可见。
            response = await client.put(
                f"qa-records/{scopes['foreign_record_id']}/feedback",
                params={"company_id": str(alpha["company_id"])},
                headers=company_headers,
                json={"feedback": "down"},
            )
            assert response.status_code == 404

            # 传 null 撤销反馈，复核状态保持不变。
            response = await client.put(
                f"qa-records/{scopes['open_record_id']}/feedback",
                params={"company_id": str(alpha["company_id"])},
                headers=company_headers,
                json={"feedback": None},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["feedback"] is None
            assert body["feedback_reason"] is None
            assert body["feedback_at"] is None
            assert body["review_status"] == "pending"

            # 管理员按点踩筛选，仍能查询到历史反馈记录。
            await client.put(
                f"qa-records/{scopes['open_record_id']}/feedback",
                params={"company_id": str(alpha["company_id"])},
                headers=company_headers,
                json={"feedback": "down", "reason": "仍然不对"},
            )
            response = await client.get(
                "admin/qa-records",
                params={"feedback": "down"},
                headers=admin_headers,
            )
            assert response.status_code == 200, response.text
            # open_record 重新点踩、resolved_record 之前点踩，共两条。
            assert response.json()["total"] == 2
            assert {item["feedback"] for item in response.json()["items"]} == {"down"}

        await engine.dispose()

    asyncio.run(scenario())


def test_project_trace_feedback_updates_review_status(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace-feedback.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        scopes = await _seed(factory)
        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _app(monkeypatch, settings, factory)
        alpha = scopes["alpha"]
        company_headers = {"Authorization": "Bearer alpha-token"}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1/"
        ) as client:
            url = f"knowledge-bases/{alpha['kb_id']}/project-traces/trace-feedback-1/feedback"
            response = await client.put(
                url,
                params={"company_id": str(alpha["company_id"])},
                headers={**company_headers, "X-Project-Key": "test_agent"},
                json={"feedback": "down", "reason": "没有回答"},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["feedback"] == "down"
            assert body["review_status"] == "pending"

            response = await client.put(
                url,
                params={"company_id": str(alpha["company_id"])},
                headers={**company_headers, "X-Project-Key": "unknown-project"},
                json={"feedback": "up"},
            )
            assert response.status_code == 404

            # 管理员复核后状态变为 resolved。
            response = await client.patch(
                f"admin/project-traces/{scopes['trace_id']}/review",
                headers={"Authorization": "Bearer admin-test-token"},
                json={"review_status": "resolved"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["review_status"] == "resolved"

            response = await client.get(
                "admin/project-traces",
                params={"feedback": "down"},
                headers={"Authorization": "Bearer admin-test-token"},
            )
            assert response.status_code == 200
            assert response.json()["total"] == 1

        await engine.dispose()

    asyncio.run(scenario())
