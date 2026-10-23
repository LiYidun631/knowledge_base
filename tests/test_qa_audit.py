"""验证在线问答记录、来源快照和阶段日志的持久化。"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.application.qa.service import (
    add_qa_event,
    finish_qa_failure,
    finish_qa_no_results,
    finish_qa_success,
    start_qa_record,
)
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    KnowledgeBase,
    ModelProfile,
    QAEvent,
    QARecord,
    QASource,
)


def test_qa_success_saves_answer_source_snapshot_and_events(tmp_path) -> None:
    """成功问答会保存答案、来源快照、检索分数和阶段耗时。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'qa.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
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
                    session.add(embedding)
                    await session.flush()
                    knowledge_base = KnowledgeBase(
                        name="问答审计测试库", embedding_model_id=embedding.id
                    )
                    session.add(knowledge_base)
                    await session.flush()
                    record = await start_qa_record(
                        session,
                        knowledge_base_id=knowledge_base.id,
                        session_id=uuid4(),
                        query="如何退款？",
                        retrieval_mode="hybrid",
                        top_k=5,
                        include_context=True,
                        chat_provider="dashscope",
                        chat_model_name="qwen-plus",
                    )
                    add_qa_event(
                        session,
                        record,
                        stage="retrieval",
                        status="success",
                        duration_ms=12,
                        details={"result_count": 1},
                    )
                    result = SimpleNamespace(
                        document_id=uuid4(),
                        document_version_id=uuid4(),
                        chunk_id=uuid4(),
                        filename="退款说明.md",
                        version_number=1,
                        page_number=2,
                        section="退款规则",
                        content="退款需要在订单完成后七天内申请。",
                        context="退款规则\n退款需要在订单完成后七天内申请。",
                        cosine_distance=0.12,
                        bm25_score=1.4,
                        rrf_score=0.02,
                        rerank_score=0.9,
                        assets=[],
                    )
                    await finish_qa_success(
                        session,
                        record,
                        answer="您可以在订单完成后七天内申请退款。",
                        results=[result],
                        retrieval_duration_ms=12,
                        generation_duration_ms=80,
                        total_duration_ms=95,
                        token_usage={"prompt_tokens": 200, "completion_tokens": 50},
                    )

            async with factory() as session:
                saved = await session.scalar(select(QARecord))
                source = await session.scalar(select(QASource))
                events = list(await session.scalars(select(QAEvent).order_by(QAEvent.created_at)))
                assert saved is not None
                assert saved.status == "success"
                assert saved.review_status == "none"
                assert saved.answer == "您可以在订单完成后七天内申请退款。"
                assert saved.result_count == 1
                # 模型只返回输入/输出时，总 token 由两者相加补全。
                assert saved.prompt_tokens == 200
                assert saved.completion_tokens == 50
                assert saved.total_tokens == 250
                assert source is not None
                assert source.content_snapshot == result.content
                assert source.rerank_score == 0.9
                assert [event.stage for event in events] == ["request", "retrieval", "generation"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_qa_failure_and_no_results_are_persisted(tmp_path) -> None:
    """模型失败和无召回结果都要留下可查询的状态。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'qa-status.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                async with session.begin():
                    embedding = ModelProfile(
                        name="qa-status-embedding",
                        kind="embedding",
                        provider="openai_compatible",
                        model_name="qa-status-embedding",
                        dimensions=2,
                    )
                    session.add(embedding)
                    await session.flush()
                    knowledge_base = KnowledgeBase(
                        name="问答状态测试库", embedding_model_id=embedding.id
                    )
                    session.add(knowledge_base)
                    await session.flush()
                    no_result = await start_qa_record(
                        session,
                        knowledge_base_id=knowledge_base.id,
                        session_id=None,
                        query="没有答案的问题",
                        retrieval_mode="bm25",
                        top_k=5,
                        include_context=True,
                        chat_provider="dashscope",
                        chat_model_name="qwen-plus",
                    )
                    await finish_qa_no_results(
                        session,
                        no_result,
                        retrieval_duration_ms=3,
                        total_duration_ms=3,
                    )
                    failed = await start_qa_record(
                        session,
                        knowledge_base_id=knowledge_base.id,
                        session_id=None,
                        query="模型超时的问题",
                        retrieval_mode="hybrid",
                        top_k=5,
                        include_context=True,
                        chat_provider="dashscope",
                        chat_model_name="qwen-plus",
                    )
                    await finish_qa_failure(
                        session,
                        failed,
                        stage="generation",
                        error=TimeoutError("CHAT_TIMEOUT: 聊天模型请求超时"),
                        results=[],
                        retrieval_duration_ms=20,
                        generation_duration_ms=60,
                        total_duration_ms=80,
                    )
            async with factory() as session:
                records = list(
                    await session.scalars(
                        select(QARecord).order_by(QARecord.created_at)
                    )
                )
                assert [record.status for record in records] == ["no_results", "failed"]
                assert [record.review_status for record in records] == ["pending", "pending"]
                assert records[0].answer == "知识库中没有检索到与问题相关的内容。"
                assert records[1].error_code == "CHAT_TIMEOUT"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_qa_success_low_rerank_marks_pending(tmp_path) -> None:
    """成功问答的最佳 rerank 低于阈值时进入待补资料；无 rerank 分数不误标。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'qa-review.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                async with session.begin():
                    embedding = ModelProfile(
                        name="qa-review-embedding",
                        kind="embedding",
                        provider="openai_compatible",
                        model_name="qa-review-embedding",
                        dimensions=2,
                    )
                    session.add(embedding)
                    await session.flush()
                    knowledge_base = KnowledgeBase(
                        name="问答复核测试库", embedding_model_id=embedding.id
                    )
                    session.add(knowledge_base)
                    await session.flush()

                    async def make_record(query: str) -> QARecord:
                        return await start_qa_record(
                            session,
                            knowledge_base_id=knowledge_base.id,
                            session_id=None,
                            query=query,
                            retrieval_mode="hybrid",
                            top_k=5,
                            include_context=True,
                            chat_provider="dashscope",
                            chat_model_name="qwen-plus",
                        )

                    def make_result(rerank_score: float | None) -> SimpleNamespace:
                        return SimpleNamespace(
                            document_id=uuid4(),
                            document_version_id=uuid4(),
                            chunk_id=uuid4(),
                            filename="说明.md",
                            version_number=1,
                            page_number=1,
                            section=None,
                            content="内容",
                            context=None,
                            cosine_distance=0.3,
                            bm25_score=1.0,
                            rrf_score=0.01,
                            rerank_score=rerank_score,
                            assets=[],
                        )

                    low = await make_record("低分问题")
                    await finish_qa_success(
                        session,
                        low,
                        answer="回答",
                        results=[make_result(0.5), make_result(0.55)],
                        retrieval_duration_ms=10,
                        generation_duration_ms=10,
                        total_duration_ms=20,
                    )
                    assert low.review_status == "pending"

                    # 阈值可调：同样 0.5 的最佳分在 0.4 阈值下不算低分。
                    ok = await make_record("阈值内问题")
                    await finish_qa_success(
                        session,
                        ok,
                        answer="回答",
                        results=[make_result(0.5)],
                        retrieval_duration_ms=10,
                        generation_duration_ms=10,
                        total_duration_ms=20,
                        badcase_rerank_threshold=0.4,
                    )
                    assert ok.review_status == "none"

                    # 未启用重排（无 rerank 分数）时无法判断质量，保持未复核。
                    unranked = await make_record("无重排分数问题")
                    await finish_qa_success(
                        session,
                        unranked,
                        answer="回答",
                        results=[make_result(None)],
                        retrieval_duration_ms=10,
                        generation_duration_ms=10,
                        total_duration_ms=20,
                    )
                    assert unranked.review_status == "none"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
