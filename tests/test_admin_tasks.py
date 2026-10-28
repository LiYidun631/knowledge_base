"""管理员任务中心聚合接口验证：入库、FAQ 生成、检索评测三类任务归一化展示。"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    Document,
    DocumentVersion,
    EvaluationRun,
    EvaluationSet,
    FaqEntry,
    IngestionJob,
    KnowledgeBase,
    ModelProfile,
    OutboxEvent,
    utc_now,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def _admin_app(monkeypatch, settings, factory):
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


def test_http_admin_tasks_aggregate(monkeypatch, tmp_path) -> None:
    """三类任务统一排序与计数，同对象只留最新记录，失败 FAQ 已补生成则隐藏。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin_tasks.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            async with session.begin():
                now = utc_now()
                yesterday = now - timedelta(days=1)
                embedding = ModelProfile(
                    name="task-embedding",
                    kind="embedding",
                    provider="openai_compatible",
                    model_name="task-embedding",
                    dimensions=2,
                )
                company_a = Company(code="task-a", name="任务甲公司")
                company_b = Company(code="task-b", name="任务乙公司")
                session.add_all([embedding, company_a, company_b])
                await session.flush()
                kb_a = KnowledgeBase(
                    company_id=company_a.id, name="任务甲库", embedding_model_id=embedding.id
                )
                kb_b = KnowledgeBase(
                    company_id=company_b.id, name="任务乙库", embedding_model_id=embedding.id
                )
                session.add_all([kb_a, kb_b])
                await session.flush()

                def make_document(
                    company,
                    kb,
                    key,
                    filename,
                    status,
                ) -> tuple[Document, DocumentVersion]:
                    # 主键显式生成，便于构造版本时直接引用文档 ID。
                    document = Document(
                        id=uuid4(),
                        company_id=company.id,
                        knowledge_base_id=kb.id,
                        document_key=key,
                        filename=filename,
                        latest_version_number=1,
                        status=status,
                    )
                    version = DocumentVersion(
                        document_id=document.id,
                        version_number=1,
                        filename=filename,
                        size_bytes=1,
                        content_sha256="0" * 64,
                        object_key=f"{key}.pdf",
                        status=status,
                        is_current=True,
                    )
                    return document, version

                # 入库：完成（今日）、失败、进行中、排队各一条，另有一条昨日完成的旧任务。
                doc_ok, ver_ok = make_document(
                    company_a, kb_a, "doc-ok", "已完成说明书.pdf", "completed"
                )
                doc_fail, ver_fail = make_document(
                    company_b, kb_b, "doc-fail", "失败说明书.pdf", "failed"
                )
                doc_run, ver_run = make_document(
                    company_a, kb_a, "doc-run", "处理中说明书.pdf", "processing"
                )
                doc_queued, ver_queued = make_document(
                    company_a, kb_a, "doc-queued", "排队说明书.pdf", "queued"
                )
                session.add_all(
                    [
                        doc_ok,
                        ver_ok,
                        doc_fail,
                        ver_fail,
                        doc_run,
                        ver_run,
                        doc_queued,
                        ver_queued,
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        IngestionJob(
                            document_id=doc_ok.id,
                            document_version_id=ver_ok.id,
                            status="completed",
                            stage="complete",
                            attempts=1,
                            started_at=now - timedelta(minutes=8),
                            finished_at=now - timedelta(minutes=5),
                        ),
                        IngestionJob(
                            document_id=doc_fail.id,
                            document_version_id=ver_fail.id,
                            status="failed",
                            stage="embed",
                            attempts=2,
                            error_details={"code": "EMBEDDING_TIMEOUT", "message": "向量化超时"},
                            started_at=now - timedelta(hours=2),
                            finished_at=now - timedelta(hours=1),
                        ),
                        IngestionJob(
                            document_id=doc_run.id,
                            document_version_id=ver_run.id,
                            status="running",
                            stage="embed",
                            attempts=1,
                            started_at=now - timedelta(minutes=3),
                        ),
                        IngestionJob(
                            document_id=doc_queued.id,
                            document_version_id=ver_queued.id,
                            status="queued",
                            attempts=0,
                        ),
                    ]
                )

                # FAQ：排队、失败（未补生成）、失败（已手动补生成，应隐藏）。
                session.add_all(
                    [
                        OutboxEvent(
                            event_type="document.faq_generation_requested",
                            aggregate_type="document",
                            aggregate_id=doc_run.id,
                            payload={
                                "document_id": str(doc_run.id),
                                "document_version_id": str(ver_run.id),
                            },
                            status="pending",
                        ),
                        OutboxEvent(
                            event_type="document.faq_generation_requested",
                            aggregate_type="document",
                            aggregate_id=doc_fail.id,
                            payload={
                                "document_id": str(doc_fail.id),
                                "document_version_id": str(ver_fail.id),
                            },
                            status="failed",
                            attempts=3,
                        ),
                        OutboxEvent(
                            event_type="document.faq_generation_requested",
                            aggregate_type="document",
                            aggregate_id=doc_queued.id,
                            payload={
                                "document_id": str(doc_queued.id),
                                "document_version_id": str(ver_queued.id),
                            },
                            status="failed",
                            attempts=3,
                        ),
                        FaqEntry(
                            knowledge_base_id=kb_a.id,
                            document_id=doc_queued.id,
                            document_version_id=ver_queued.id,
                            question="手动补的 FAQ",
                            answer="已补齐",
                            question_embedding=[0.1, 0.2],
                            source="auto",
                        ),
                    ]
                )

                # 评测：set1 进行中；set2 旧失败被今日成功顶掉；set3 昨日成功不展示。
                set_a = EvaluationSet(knowledge_base_id=kb_a.id, name="甲评测集")
                set_b = EvaluationSet(knowledge_base_id=kb_b.id, name="乙评测集")
                set_c = EvaluationSet(knowledge_base_id=kb_a.id, name="丙评测集")
                session.add_all([set_a, set_b, set_c])
                await session.flush()
                session.add_all(
                    [
                        EvaluationRun(
                            set_id=set_a.id,
                            knowledge_base_id=kb_a.id,
                            company_id=company_a.id,
                            status="running",
                            case_count=10,
                            completed_count=3,
                            started_at=now - timedelta(minutes=2),
                        ),
                        EvaluationRun(
                            set_id=set_b.id,
                            knowledge_base_id=kb_b.id,
                            company_id=company_b.id,
                            status="failed",
                            case_count=4,
                            completed_count=1,
                            error_message="检索超时",
                            started_at=yesterday,
                            finished_at=yesterday + timedelta(minutes=1),
                            created_at=yesterday,
                        ),
                        EvaluationRun(
                            set_id=set_b.id,
                            knowledge_base_id=kb_b.id,
                            company_id=company_b.id,
                            status="success",
                            case_count=4,
                            completed_count=4,
                            started_at=now - timedelta(minutes=30),
                            finished_at=now - timedelta(minutes=25),
                            created_at=now - timedelta(minutes=30),
                        ),
                        EvaluationRun(
                            set_id=set_c.id,
                            knowledge_base_id=kb_a.id,
                            company_id=company_a.id,
                            status="success",
                            case_count=2,
                            completed_count=2,
                            started_at=yesterday,
                            finished_at=yesterday + timedelta(minutes=2),
                            created_at=yesterday,
                        ),
                    ]
                )

        settings = Settings(_env_file=None, auth_admin_token="admin-test-token")
        app = _admin_app(monkeypatch, settings, factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
        ) as anonymous:
            response = await anonymous.get("admin/tasks")
            assert response.status_code == 401

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers={"Authorization": "Bearer admin-test-token"},
        ) as client:
            response = await client.get("admin/tasks")
            assert response.status_code == 200, response.text
            body = response.json()
            # 默认 open：排队/进行/失败；已完成的不在默认列表里。
            assert body["truncated"] is False
            titles = [item["title"] for item in body["items"]]
            assert "已完成说明书.pdf" not in titles
            assert len(body["items"]) == 6
            assert [item["status"] for item in body["items"][:2]] == ["running", "running"]
            assert body["counts"] == {
                "queued": 2,
                "running": 2,
                "failed": 2,
                "done_today": 2,
            }

            # 同一文档只保留最新一条；已补生成 FAQ 的失败事件被隐藏。
            faq_titles = [item["title"] for item in body["items"] if item["kind"] == "faq"]
            assert faq_titles == ["处理中说明书.pdf", "失败说明书.pdf"]

            ingest_failed = next(
                item
                for item in body["items"]
                if item["kind"] == "ingest" and item["status"] == "failed"
            )
            assert ingest_failed["detail"] == "v1 · 第 2 次尝试"
            assert ingest_failed["error_message"] == "向量化超时"
            assert ingest_failed["company_name"] == "任务乙公司"
            assert ingest_failed["knowledge_base_name"] == "任务乙库"
            assert ingest_failed["document_id"] == str(doc_fail.id)

            ingest_running = next(
                item
                for item in body["items"]
                if item["kind"] == "ingest" and item["status"] == "running"
            )
            assert ingest_running["detail"] == "v1 · 向量化"
            assert ingest_running["duration_ms"] is not None

            evaluation_running = next(
                item for item in body["items"] if item["kind"] == "evaluation"
            )
            assert evaluation_running["title"] == "甲评测集"
            assert evaluation_running["progress_current"] == 3
            assert evaluation_running["progress_total"] == 10
            assert evaluation_running["detail"] == "进度 3/10 条用例"

            # 只看入库时计数同步收窄。
            response = await client.get("admin/tasks", params={"kind": "ingest"})
            body = response.json()
            assert len(body["items"]) == 3
            assert body["counts"] == {
                "queued": 1,
                "running": 1,
                "failed": 1,
                "done_today": 1,
            }

            # 当日完成：入库完成 + 乙评测集今日成功；旧失败运行被成功顶掉。
            response = await client.get("admin/tasks", params={"status": "done"})
            body = response.json()
            assert {item["title"] for item in body["items"]} == {
                "已完成说明书.pdf",
                "乙评测集",
            }
            assert {item["status"] for item in body["items"]} == {"success"}

            response = await client.get("admin/tasks", params={"status": "failed"})
            body = response.json()
            assert len(body["items"]) == 2
            assert {item["kind"] for item in body["items"]} == {"ingest", "faq"}

            response = await client.get("admin/tasks", params={"status": "all"})
            assert len(response.json()["items"]) == 8

            # 公司与知识库过滤；乙公司默认视图只剩失败的入库任务（评测已成功）。
            response = await client.get("admin/tasks", params={"company_id": str(company_b.id)})
            body = response.json()
            assert {item["title"] for item in body["items"]} == {"失败说明书.pdf"}
            response = await client.get(
                "admin/tasks",
                params={"company_id": str(company_b.id), "status": "all"},
            )
            body = response.json()
            assert {item["title"] for item in body["items"]} == {
                "失败说明书.pdf",
                "乙评测集",
            }
            response = await client.get("admin/tasks", params={"knowledge_base_id": str(kb_a.id)})
            body = response.json()
            assert {item["knowledge_base_name"] for item in body["items"]} == {"任务甲库"}

    asyncio.run(scenario())
