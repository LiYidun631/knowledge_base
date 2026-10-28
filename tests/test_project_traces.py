"""项目轨迹：检索时记录召回快照、完成后回填、管理员查询与公司隔离。"""

import asyncio
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.model_providers.embeddings import EmbeddingProviderError
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    AppProject,
    Company,
    CompanyCredential,
    Document,
    DocumentChunk,
    DocumentChunkTerm,
    DocumentVersion,
    KnowledgeBase,
    ModelProfile,
    ProjectQaSource,
    ProjectQaTrace,
    VectorLibrary,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_db_session

PROJECT_ID = uuid4()


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
    """建立两个公司与各自的库，A 公司库内含一份可被 BM25 命中的文档。"""
    async with factory() as session:
        async with session.begin():
            embedding = ModelProfile(
                name="trace-embedding",
                kind="embedding",
                provider="openai_compatible",
                model_name="trace-embedding",
                dimensions=2,
            )
            session.add(embedding)
            await session.flush()
            project = AppProject(
                id=PROJECT_ID,
                project_key="test_agent",
                display_name="健康管家",
                description="test_agent 项目",
                retention_days=90,
            )
            session.add(project)
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
                library = VectorLibrary(
                    company_id=company.id,
                    name=f"{code}-library",
                    embedding_model_id=embedding.id,
                )
                session.add(library)
                await session.flush()
                kb = KnowledgeBase(
                    company_id=company.id,
                    vector_library_id=library.id,
                    name=f"{code}-kb",
                    embedding_model_id=embedding.id,
                )
                session.add(kb)
                await session.flush()
                scopes[code] = {
                    "company_id": company.id,
                    "knowledge_base_id": kb.id,
                }
            alpha = scopes["alpha"]
            document = Document(
                company_id=alpha["company_id"],
                knowledge_base_id=alpha["knowledge_base_id"],
                document_key="clean-manual",
                filename="清洁说明.md",
                latest_version_number=1,
                status="completed",
            )
            session.add(document)
            await session.flush()
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                filename="清洁说明.md",
                size_bytes=10,
                content_sha256="0" * 64,
                object_key="clean-manual.md",
                status="completed",
                is_current=True,
            )
            session.add(version)
            await session.flush()
            parent = DocumentChunk(
                document_id=document.id,
                document_version_id=version.id,
                ordinal=0,
                level="parent",
                content_sha256="1" * 64,
                content="设备清洁前请先断开电源，再用软布擦拭。",
                token_count=0,
                page_number=1,
            )
            session.add(parent)
            await session.flush()
            child = DocumentChunk(
                document_id=document.id,
                document_version_id=version.id,
                parent_chunk_id=parent.id,
                ordinal=1,
                level="child",
                content_sha256="2" * 64,
                content="设备清洁前请先断开电源，再用软布擦拭。",
                token_count=3,
                page_number=1,
            )
            session.add(child)
            await session.flush()
            session.add(
                DocumentChunkTerm(
                    chunk_id=child.id,
                    term="清洁",
                    document_id=document.id,
                    document_version_id=version.id,
                    frequency=1,
                )
            )
            scopes["alpha"]["document_id"] = document.id
            return scopes


def test_search_records_trace_and_completion_backfills(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'traces.db'}")
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
            response = await client.post(
                f"knowledge-bases/{alpha['knowledge_base_id']}/search",
                params={"company_id": str(alpha["company_id"])},
                headers={
                    **company_headers,
                    "X-Project-Key": "test_agent",
                    "X-Trace-Id": "req-1",
                    "X-Session-Id": "session-1",
                    "X-User-Ref": "user-hash",
                },
                json={"query": "清洁", "top_k": 5, "retrieval_mode": "bm25"},
            )
            assert response.status_code == 200, response.text
            assert len(response.json()["results"]) == 1

            response = await client.post(
                f"knowledge-bases/{alpha['knowledge_base_id']}/search",
                params={"company_id": str(alpha["company_id"])},
                headers={
                    **company_headers,
                    "X-Project-Key": "test_agent",
                    "X-Trace-Id": "req-empty",
                },
                json={"query": "完全不存在的主题", "top_k": 5, "retrieval_mode": "bm25"},
            )
            assert response.status_code == 200
            assert response.json()["results"] == []

            # 未声明轨迹的请求保持无状态检索。
            response = await client.post(
                f"knowledge-bases/{alpha['knowledge_base_id']}/search",
                params={"company_id": str(alpha["company_id"])},
                headers=company_headers,
                json={"query": "清洁", "top_k": 5, "retrieval_mode": "bm25"},
            )
            assert response.status_code == 200

            # 未登记的项目与缺少 trace id 的请求都只跳过记录，不影响检索。
            response = await client.post(
                f"knowledge-bases/{alpha['knowledge_base_id']}/search",
                params={"company_id": str(alpha["company_id"])},
                headers={
                    **company_headers,
                    "X-Project-Key": "unknown-project",
                    "X-Trace-Id": "req-unknown",
                },
                json={"query": "清洁", "top_k": 5, "retrieval_mode": "bm25"},
            )
            assert response.status_code == 200

            response = await client.put(
                f"knowledge-bases/{alpha['knowledge_base_id']}/project-traces/req-1",
                params={"company_id": str(alpha["company_id"])},
                headers={**company_headers, "X-Project-Key": "test_agent"},
                json={
                    "query": "清洁",
                    "answer": "请先断开电源，再用软布擦拭。",
                    "status": "success",
                    "answer_mode": "rag",
                    "intent": {"product_names": ["体温计"]},
                    "chat_provider": "qwen",
                    "chat_model_name": "qwen-plus",
                    "generation_duration_ms": 800,
                    "total_duration_ms": 1200,
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "success"

            response = await client.put(
                f"knowledge-bases/{alpha['knowledge_base_id']}/project-traces/req-empty",
                params={"company_id": str(alpha["company_id"])},
                headers={**company_headers, "X-Project-Key": "test_agent"},
                json={
                    "query": "完全不存在的主题",
                    "status": "clarification",
                    "answer_mode": "clarification",
                    "answer": "请问您咨询哪一款产品？",
                },
            )
            assert response.status_code == 200

            # 管理员列表与详情。
            response = await client.get("admin/projects", headers=admin_headers)
            assert response.status_code == 200
            assert [item["project_key"] for item in response.json()] == ["test_agent"]

            response = await client.get("admin/project-traces", headers=admin_headers)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["total"] == 2
            item = next(row for row in body["items"] if row["external_request_id"] == "req-1")
            assert item["project_name"] == "健康管家"
            assert item["company_name"] == "alpha公司"
            assert item["knowledge_base_name"] == "alpha-kb"
            assert item["status"] == "success"
            assert item["source_count"] == 1
            assert item["answer_preview"] == "请先断开电源，再用软布擦拭。"
            assert item["retrieval_attempts"][0]["result_count"] == 1

            empty_item = next(
                row for row in body["items"] if row["external_request_id"] == "req-empty"
            )
            assert empty_item["status"] == "clarification"
            assert empty_item["source_count"] == 0

            response = await client.get(
                "admin/project-traces",
                params={"status": "success", "q": "清洁"},
                headers=admin_headers,
            )
            assert response.json()["total"] == 1
            trace_id = response.json()["items"][0]["id"]

            response = await client.get(f"admin/project-traces/{trace_id}", headers=admin_headers)
            assert response.status_code == 200
            detail = response.json()
            assert detail["answer"] == "请先断开电源，再用软布擦拭。"
            assert detail["intent"] == {"product_names": ["体温计"]}
            assert detail["chat_model_name"] == "qwen-plus"
            assert len(detail["sources"]) == 1
            assert detail["sources"][0]["attempt_index"] == 0
            assert "设备清洁" in detail["sources"][0]["content_snapshot"]
            assert detail["sources"][0]["bm25_score"] is not None

        async with factory() as session:
            traces = list(await session.scalars(select(ProjectQaTrace)))
            assert len(traces) == 2
            sources = list(await session.scalars(select(ProjectQaSource)))
            assert len(sources) == 1
            assert sources[0].trace_id == next(
                trace.id for trace in traces if trace.external_request_id == "req-1"
            )
        await engine.dispose()

    asyncio.run(scenario())


def test_search_failure_records_failed_trace(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace-fail.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        scopes = await _seed(factory)
        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _app(monkeypatch, settings, factory)
        alpha = scopes["alpha"]

        from knowledge_service.api.v1 import search as search_api

        async def failing_search(*args, **kwargs):
            raise EmbeddingProviderError("embedding timeout")

        monkeypatch.setattr(search_api, "search_knowledge_base", failing_search)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1/"
        ) as client:
            response = await client.post(
                f"knowledge-bases/{alpha['knowledge_base_id']}/search",
                params={"company_id": str(alpha["company_id"])},
                headers={
                    "Authorization": "Bearer alpha-token",
                    "X-Project-Key": "test_agent",
                    "X-Trace-Id": "req-fail",
                },
                json={"query": "清洁", "retrieval_mode": "bm25"},
            )
            # 轨迹写入失败不影响检索错误本身的响应。
            assert response.status_code == 503

        async with factory() as session:
            trace = await session.scalar(
                select(ProjectQaTrace).where(ProjectQaTrace.external_request_id == "req-fail")
            )
            assert trace is not None
            assert trace.status == "failed"
            assert trace.error_code == "EmbeddingProviderError"
            assert trace.retrieval_attempts[0]["result_count"] == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_expired_traces_are_purged_lazily(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace-purge.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        scopes = await _seed(factory)
        alpha = scopes["alpha"]
        async with factory() as session:
            async with session.begin():
                project = await session.get(AppProject, PROJECT_ID)
                project.retention_days = 1
                project.last_purged_at = None
                session.add(
                    ProjectQaTrace(
                        company_id=alpha["company_id"],
                        project_id=PROJECT_ID,
                        knowledge_base_id=alpha["knowledge_base_id"],
                        external_request_id="old-request",
                        query="旧问题",
                        status="no_results",
                        include_context=False,
                        result_count=0,
                        source_count=0,
                        created_at=utc_now() - timedelta(days=2),
                    )
                )

        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _app(monkeypatch, settings, factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1/"
        ) as client:
            response = await client.post(
                f"knowledge-bases/{alpha['knowledge_base_id']}/search",
                params={"company_id": str(alpha["company_id"])},
                headers={
                    "Authorization": "Bearer alpha-token",
                    "X-Project-Key": "test_agent",
                    "X-Trace-Id": "new-request",
                },
                json={"query": "清洁", "retrieval_mode": "bm25"},
            )
            assert response.status_code == 200

        async with factory() as session:
            traces = list(await session.scalars(select(ProjectQaTrace)))
            assert [trace.external_request_id for trace in traces] == ["new-request"]
            project = await session.get(AppProject, PROJECT_ID)
            assert project.last_purged_at is not None
        await engine.dispose()

    asyncio.run(scenario())


def test_company_tokens_stay_isolated(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace-auth.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        scopes = await _seed(factory)
        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _app(monkeypatch, settings, factory)
        alpha, beta = scopes["alpha"], scopes["beta"]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1/"
        ) as client:
            # beta 公司写入轨迹。
            response = await client.post(
                f"knowledge-bases/{beta['knowledge_base_id']}/search",
                params={"company_id": str(beta["company_id"])},
                headers={
                    "Authorization": "Bearer beta-token",
                    "X-Project-Key": "test_agent",
                    "X-Trace-Id": "shared-request-id",
                },
                json={"query": "清洁", "retrieval_mode": "bm25"},
            )
            assert response.status_code == 200

            # alpha 使用相同 trace id 只会创建自己的轨迹。
            response = await client.post(
                f"knowledge-bases/{alpha['knowledge_base_id']}/search",
                params={"company_id": str(alpha["company_id"])},
                headers={
                    "Authorization": "Bearer alpha-token",
                    "X-Project-Key": "test_agent",
                    "X-Trace-Id": "shared-request-id",
                },
                json={"query": "清洁", "retrieval_mode": "bm25"},
            )
            assert response.status_code == 200

            # 公司 Token 不能进入管理端。
            response = await client.get(
                "admin/project-traces",
                headers={"Authorization": "Bearer alpha-token"},
            )
            assert response.status_code == 403

            # 不能为其他公司的知识库回填轨迹。
            response = await client.put(
                f"knowledge-bases/{beta['knowledge_base_id']}/project-traces/shared-request-id",
                params={"company_id": str(alpha["company_id"])},
                headers={
                    "Authorization": "Bearer alpha-token",
                    "X-Project-Key": "test_agent",
                },
                json={"query": "清洁", "status": "success", "answer": "泄露"},
            )
            assert response.status_code == 404

            # 跨公司访问未登记项目同样被拒绝。
            response = await client.put(
                f"knowledge-bases/{alpha['knowledge_base_id']}/project-traces/shared-request-id",
                params={"company_id": str(alpha["company_id"])},
                headers={
                    "Authorization": "Bearer alpha-token",
                    "X-Project-Key": "missing-project",
                },
                json={"query": "清洁", "status": "success", "answer": "x"},
            )
            assert response.status_code == 404

        async with factory() as session:
            traces = list(await session.scalars(select(ProjectQaTrace)))
            assert len(traces) == 2
            status_by_company = {trace.company_id: trace.status for trace in traces}
            assert set(status_by_company) == {alpha["company_id"], beta["company_id"]}
            assert status_by_company[alpha["company_id"]] == "running"
            assert status_by_company[beta["company_id"]] == "no_results"
        await engine.dispose()

    asyncio.run(scenario())
