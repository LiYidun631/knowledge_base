"""流式问答失败记录必须通过独立会话可靠落库。"""

import asyncio
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.api.v1 import answer
from knowledge_service.application.qa.service import start_qa_record
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import QARecord


def test_stream_failure_persists_with_independent_session(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stream_failure.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            monkeypatch.setattr(answer, "get_session_factory", lambda: factory)

            async with factory() as session:
                record = await start_qa_record(
                    session,
                    knowledge_base_id=uuid4(),
                    session_id=None,
                    query="设备如何清洁？",
                    retrieval_mode="vector",
                    top_k=5,
                    include_context=True,
                    chat_provider="test",
                    chat_model_name="test-model",
                )
                await session.commit()
                record_id = record.id

            # 模拟断连场景：请求会话已关闭，独立会话仍要写入失败状态。
            await answer._persist_stream_failure(
                record_id,
                stage="retrieval",
                error=RuntimeError("CLIENT_DISCONNECTED: 客户端已断开连接"),
                results=[],
                retrieval_duration_ms=None,
                generation_duration_ms=None,
                total_duration_ms=12,
            )

            async with factory() as session:
                row = await session.get(QARecord, record_id)
                assert row is not None
                assert row.status == "failed"
                assert row.review_status == "pending"
                assert row.error_code == "CLIENT_DISCONNECTED"
                assert row.total_duration_ms == 12
        finally:
            await engine.dispose()

    asyncio.run(scenario())
