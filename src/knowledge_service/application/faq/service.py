"""库级与文档级 FAQ 的领域服务：CRUD、语义匹配、自动生成与答案改写。"""

import logging
import math
import random
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.application.search.service import (
    _metadata_matches_product as metadata_matches_product,
)
from knowledge_service.core.config import get_settings
from knowledge_service.infrastructure.model_providers.chat import (
    generate_faq_pairs,
    rewrite_faq_answer,
)
from knowledge_service.infrastructure.model_providers.embeddings import embed_texts
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    FaqEntry,
    KnowledgeBase,
    ModelProfile,
    OutboxEvent,
)

logger = logging.getLogger(__name__)


class FaqNotFound(RuntimeError):
    """FAQ 不存在或不属于指定知识库。"""


class FaqModelUnavailable(RuntimeError):
    """知识库未配置可用的 Embedding 模型。"""


class FaqEntryNotFound(FaqNotFound):
    """按 ID 找不到 FAQ。"""


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """向量余弦相似度；维度不一致或零向量返回 0。"""
    if len(a) != len(b) or not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def _embedding_profile(session: AsyncSession, kb: KnowledgeBase) -> ModelProfile:
    profile = await session.get(ModelProfile, kb.embedding_model_id)
    if profile is None or not profile.is_enabled or profile.kind != "embedding":
        raise FaqModelUnavailable("知识库未配置可用的 Embedding 模型")
    return profile


async def _chat_profile(session: AsyncSession) -> ModelProfile | None:
    return await session.scalar(
        select(ModelProfile).where(
            ModelProfile.kind == "chat",
            ModelProfile.is_enabled.is_(True),
            ModelProfile.is_default.is_(True),
        )
    )


async def embed_questions(
    session: AsyncSession, kb: KnowledgeBase, questions: list[str]
) -> list[list[float]]:
    """批量向量化问题文本，写入与匹配共用同一套 Embedding 模型。"""
    profile = await _embedding_profile(session, kb)
    return await embed_texts(profile, questions)


async def create_faq(
    session: AsyncSession,
    *,
    kb: KnowledgeBase,
    question: str,
    answer: str,
    document_id: UUID | None = None,
    document_version_id: UUID | None = None,
    source: str = "manual",
) -> FaqEntry:
    entry = FaqEntry(
        knowledge_base_id=kb.id,
        document_id=document_id,
        document_version_id=document_version_id,
        question=question,
        answer=answer,
        question_embedding=(await embed_questions(session, kb, [question]))[0],
        source=source,
    )
    session.add(entry)
    await session.flush()
    return entry


async def update_faq(
    session: AsyncSession,
    *,
    kb: KnowledgeBase,
    entry: FaqEntry,
    question: str | None = None,
    answer: str | None = None,
) -> FaqEntry:
    if question is not None and question.strip() and question.strip() != entry.question:
        entry.question = question.strip()
        entry.question_embedding = (await embed_questions(session, kb, [entry.question]))[0]
    if answer is not None and answer.strip():
        entry.answer = answer.strip()
    await session.flush()
    return entry


async def faq_pool(
    session: AsyncSession,
    *,
    kb: KnowledgeBase,
    product_name: str | None = None,
    model_name: str | None = None,
) -> list[FaqEntry]:
    """FAQ 候选池：库级 FAQ 全部参与；文档级 FAQ 以产品/型号权限过滤。

    当前版本的文档级条目才会入池（旧版本自动失效）；请求未携带产品/型号时
    只放行库级 FAQ，与在线问答的 match_faq 语义一致。
    """
    rows = list(
        await session.execute(
            select(FaqEntry, Document.extra_metadata)
            .outerjoin(DocumentVersion, FaqEntry.document_version_id == DocumentVersion.id)
            .outerjoin(Document, FaqEntry.document_id == Document.id)
            .where(
                FaqEntry.knowledge_base_id == kb.id,
                or_(
                    FaqEntry.document_version_id.is_(None),
                    DocumentVersion.is_current.is_(True),
                ),
            )
        )
    )
    has_product_scope = bool((product_name or "").strip()) or bool(
        (model_name or "").strip()
    )
    return [
        entry
        for entry, metadata in rows
        if entry.document_id is None
        or (
            has_product_scope
            and metadata_matches_product(
                metadata, product_name=product_name, model_name=model_name
            )
        )
    ]


async def random_faqs(
    session: AsyncSession,
    *,
    kb: KnowledgeBase,
    product_name: str | None = None,
    model_name: str | None = None,
    limit: int = 6,
) -> list[FaqEntry]:
    """从候选池随机抽取 FAQ，用于问答页的推荐问题卡片。"""
    pool = await faq_pool(
        session, kb=kb, product_name=product_name, model_name=model_name
    )
    return random.sample(pool, min(limit, len(pool)))


async def match_faq(
    session: AsyncSession,
    *,
    kb: KnowledgeBase,
    query: str,
    product_name: str | None = None,
    model_name: str | None = None,
    threshold: float | None = None,
) -> FaqEntry | None:
    """query 与 FAQ 问题做余弦匹配，最高分达到阈值即返回该 FAQ。

    匹配池 = 库级 FAQ + 当前版本文档级 FAQ，旧版本的文档级 FAQ 自动失效。
    权限规则：库级 FAQ 随库权限即可匹配；文档级 FAQ 以文档的产品名称与型号为准，
    仅当请求携带的产品/型号过滤与文档标签匹配时才参与匹配；
    请求未携带任何产品/型号时只匹配库级 FAQ。
    """
    settings = get_settings()
    effective_threshold = threshold if threshold is not None else settings.faq_match_threshold
    pool = await faq_pool(
        session, kb=kb, product_name=product_name, model_name=model_name
    )
    if not pool:
        return None
    query_vector = (await embed_questions(session, kb, [query]))[0]
    best = max(
        pool,
        key=lambda entry: cosine_similarity(query_vector, entry.question_embedding),
    )
    if cosine_similarity(query_vector, best.question_embedding) < effective_threshold:
        return None
    return best


async def rewrite_faq(
    session: AsyncSession,
    *,
    entry: FaqEntry,
    instruction: str | None = None,
) -> FaqEntry:
    """AI 改写 FAQ 答案；有指令按指令改，无指令自动润色。"""
    profile = await _chat_profile(session)
    entry.answer = await rewrite_faq_answer(
        question=entry.question,
        answer=entry.answer,
        instruction=instruction,
        profile=profile,
        session=session,
    )
    await session.flush()
    return entry


async def enqueue_missing_faq_generation(
    session: AsyncSession, *, kb: KnowledgeBase
) -> tuple[int, int]:
    """为当前版本还没有自动 FAQ 的文档排队生成，返回 (入队数, 跳过数)。

    已有自动条目或在途事件的文档不重复入队，避免重复调用生成模型。
    """
    rows = list(
        await session.execute(
            select(Document.id, DocumentVersion.id)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(
                Document.knowledge_base_id == kb.id,
                DocumentVersion.is_current.is_(True),
            )
        )
    )
    if not rows:
        return 0, 0
    version_ids = [version_id for _, version_id in rows]
    auto_versions = set(
        await session.scalars(
            select(FaqEntry.document_version_id).where(
                FaqEntry.document_version_id.in_(version_ids),
                FaqEntry.source == "auto",
            )
        )
    )
    in_flight: set[UUID] = set()
    pending_events = await session.scalars(
        select(OutboxEvent).where(
            OutboxEvent.event_type == "document.faq_generation_requested",
            OutboxEvent.status.in_(("pending", "processing")),
        )
    )
    for event in pending_events:
        raw_version_id = (event.payload or {}).get("document_version_id")
        if raw_version_id:
            try:
                in_flight.add(UUID(str(raw_version_id)))
            except ValueError:
                continue
    queued = 0
    skipped = 0
    for document_id, version_id in rows:
        if version_id in auto_versions or version_id in in_flight:
            skipped += 1
            continue
        session.add(
            OutboxEvent(
                event_type="document.faq_generation_requested",
                aggregate_type="document",
                aggregate_id=document_id,
                payload={
                    "document_id": str(document_id),
                    "document_version_id": str(version_id),
                },
            )
        )
        queued += 1
    await session.flush()
    return queued, skipped


async def generate_for_document_version(
    session: AsyncSession,
    *,
    document_id: UUID,
    document_version_id: UUID,
) -> list[FaqEntry]:
    """从文档当前父块内容提炼 FAQ 并落库；重复触发时先替换该版本的自动生成条目。"""
    settings = get_settings()
    version = await session.scalar(
        select(DocumentVersion).where(DocumentVersion.id == document_version_id)
    )
    if version is None:
        raise FaqNotFound("文档版本不存在")
    kb = await session.scalar(
        select(KnowledgeBase).join(
            Document, Document.knowledge_base_id == KnowledgeBase.id
        ).where(Document.id == version.document_id)
    )
    if kb is None:
        raise FaqNotFound("所属知识库不存在")
    chunks = list(
        await session.scalars(
            select(DocumentChunk)
            .where(
                DocumentChunk.document_version_id == document_version_id,
                DocumentChunk.level == "parent",
            )
            .order_by(DocumentChunk.ordinal)
        )
    )
    if not chunks:
        raise FaqNotFound("该版本没有已入库的切块，无法生成 FAQ")
    context_parts: list[str] = []
    context_chars = 0
    for chunk in chunks:
        if context_chars >= settings.faq_generation_context_chars:
            break
        context_parts.append(chunk.content)
        context_chars += len(chunk.content)
    context = "\n\n".join(context_parts)
    total_chars = sum(len(chunk.content) for chunk in chunks)
    count = (
        settings.faq_generation_large_count
        if total_chars >= settings.faq_generation_large_chars
        else settings.faq_generation_count
    )
    pairs = await generate_faq_pairs(
        context=context, count=count, profile=await _chat_profile(session), session=session
    )
    await session.execute(
        delete(FaqEntry).where(
            FaqEntry.document_version_id == document_version_id,
            FaqEntry.source == "auto",
        )
    )
    questions = [pair["question"] for pair in pairs]
    vectors = await embed_questions(session, kb, questions)
    entries = [
        FaqEntry(
            knowledge_base_id=kb.id,
            document_id=document_id,
            document_version_id=document_version_id,
            question=questions[index],
            answer=pairs[index]["answer"],
            question_embedding=vectors[index],
            source="auto",
        )
        for index in range(len(pairs))
    ]
    session.add_all(entries)
    await session.flush()
    return entries
