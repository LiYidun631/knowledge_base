"""验证应用进程内任务循环保留并重试数据库 Outbox 事件。"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.application.faq.service import FaqNotFound
from knowledge_service.core.config import Settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import OutboxEvent
from knowledge_service.infrastructure.task_queue import outbox


def test_failed_in_process_task_keeps_event_for_retry(tmp_path, monkeypatch):
    """任务执行异常时事件退回 pending，成功完成后才标记 published。"""

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        settings = Settings(ingestion_retry_base_seconds=1)
        process = AsyncMock(side_effect=[RuntimeError("temporary"), "completed"])
        monkeypatch.setattr(outbox, "get_settings", lambda: settings)
        monkeypatch.setattr(outbox, "get_session_factory", lambda: factory)
        monkeypatch.setattr(outbox, "process_ingestion_job", process)
        monkeypatch.setattr(outbox, "recover_stale_jobs", AsyncMock(return_value=0))
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            job_id = uuid4()
            async with factory() as session, session.begin():
                session.add(
                    OutboxEvent(
                        aggregate_type="document",
                        aggregate_id=uuid4(),
                        event_type="document.ingestion_requested",
                        payload={"job_id": str(job_id)},
                    )
                )

            assert await outbox.dispatch_once() == 0
            async with factory() as session, session.begin():
                event = await session.scalar(select(OutboxEvent))
                assert event.status == "pending"
                assert event.published_at is None
                assert event.attempts == 1
                event.available_at = datetime.now(UTC) - timedelta(seconds=1)

            assert await outbox.dispatch_once() == 1
            async with factory() as session:
                event = await session.scalar(select(OutboxEvent))
                assert event.status == "published"
                assert event.published_at is not None
                assert event.attempts == 2
            process.assert_has_awaits([((job_id,),), ((job_id,),)])
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_faq_generation_event_is_dispatched(tmp_path, monkeypatch):
    """入库完成后的 FAQ 生成事件被消费，成功后标记 published。"""

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'faq-events.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        settings = Settings(ingestion_retry_base_seconds=1)
        generate = AsyncMock(return_value=[])
        monkeypatch.setattr(outbox, "get_settings", lambda: settings)
        monkeypatch.setattr(outbox, "get_session_factory", lambda: factory)
        monkeypatch.setattr(outbox, "generate_for_document_version", generate)
        monkeypatch.setattr(outbox, "recover_stale_jobs", AsyncMock(return_value=0))
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            document_id, version_id = uuid4(), uuid4()
            async with factory() as session, session.begin():
                session.add(
                    OutboxEvent(
                        aggregate_type="document",
                        aggregate_id=document_id,
                        event_type="document.faq_generation_requested",
                        payload={
                            "document_id": str(document_id),
                            "document_version_id": str(version_id),
                        },
                    )
                )

            assert await outbox.dispatch_once() == 1
            async with factory() as session:
                event = await session.scalar(select(OutboxEvent))
                assert event.status == "published"
                assert event.published_at is not None
                assert event.attempts == 1
            generate.assert_awaited_once()
            kwargs = generate.await_args.kwargs
            assert kwargs["document_id"] == document_id
            assert kwargs["document_version_id"] == version_id
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_faq_generation_event_retry_and_dead_letter(tmp_path, monkeypatch):
    """FAQ 生成失败按有限次数重试；目标永久消失直接终态失败。"""

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'faq-retry.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        settings = Settings(
            ingestion_retry_base_seconds=1, max_ingestion_attempts=2
        )
        generate = AsyncMock(side_effect=RuntimeError("模型暂时不可用"))
        monkeypatch.setattr(outbox, "get_settings", lambda: settings)
        monkeypatch.setattr(outbox, "get_session_factory", lambda: factory)
        monkeypatch.setattr(outbox, "generate_for_document_version", generate)
        monkeypatch.setattr(outbox, "recover_stale_jobs", AsyncMock(return_value=0))
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            document_id = uuid4()
            async with factory() as session, session.begin():
                session.add(
                    OutboxEvent(
                        aggregate_type="document",
                        aggregate_id=document_id,
                        event_type="document.faq_generation_requested",
                        payload={
                            "document_id": str(document_id),
                            "document_version_id": str(uuid4()),
                        },
                    )
                )

            # 第一次失败退回 pending 等待重试。
            assert await outbox.dispatch_once() == 0
            async with factory() as session, session.begin():
                event = await session.scalar(select(OutboxEvent))
                assert event.status == "pending"
                assert event.attempts == 1
                event.available_at = datetime.now(UTC) - timedelta(seconds=1)

            # 达到最大次数后置为 failed，不再空转。
            assert await outbox.dispatch_once() == 0
            async with factory() as session:
                event = await session.scalar(select(OutboxEvent))
                assert event.status == "failed"
                assert event.attempts == 2

            # 目标不存在的永久失败：直接终态，不重试。
            generate.side_effect = FaqNotFound("文档版本不存在")
            async with factory() as session, session.begin():
                session.add(
                    OutboxEvent(
                        aggregate_type="document",
                        aggregate_id=document_id,
                        event_type="document.faq_generation_requested",
                        payload={
                            "document_id": str(document_id),
                            "document_version_id": str(uuid4()),
                        },
                    )
                )
            assert await outbox.dispatch_once() == 1
            async with factory() as session:
                events = list(
                    await session.scalars(select(OutboxEvent).order_by(OutboxEvent.created_at))
                )
                assert events[-1].status == "failed"
                assert events[-1].attempts == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
