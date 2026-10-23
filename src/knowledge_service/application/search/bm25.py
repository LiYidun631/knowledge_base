"""中文字符二元组与英文词项组成的 MySQL BM25 倒排检索。"""

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.core.config import get_settings
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentChunk,
    DocumentChunkTerm,
    DocumentVersion,
)

_TOKEN_PATTERN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f]+"
    r"|[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*"
)


@dataclass(frozen=True)
class BM25Hit:
    """BM25 召回到的子块及其相关度分数。"""

    chunk_id: UUID
    score: float


def tokenize(text: str, *, max_tokens: int | None = None) -> list[str]:
    """中文按相邻二字切词，英文和数字按连续词切分，统一转小写。"""
    tokens: list[str] = []
    for match in _TOKEN_PATTERN.finditer(text.casefold()):
        value = match.group()
        if "\u3400" <= value[0] <= "\U0003134f":
            # 使用重叠二元组保留中文短语的局部词序；单个汉字仍可独立检索。
            words = (
                [value] if len(value) == 1 else [value[i : i + 2] for i in range(len(value) - 1)]
            )
        else:
            words = [value[:128]]
        tokens.extend(words)
        if max_tokens is not None and len(tokens) >= max_tokens:
            return tokens[:max_tokens]
    return tokens


def term_frequencies(text: str) -> tuple[int, dict[str, int]]:
    """生成分块长度和词频，供离线入库与迁移回填共用。"""
    tokens = tokenize(text)
    return len(tokens), dict(Counter(tokens))


async def search_bm25(
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    company_id: UUID | None = None,
    document_version_ids: list[UUID],
    document_ids: list[UUID] | None,
    query: str,
    limit: int,
) -> list[BM25Hit]:
    """只搜索当前成功版本，按标准 BM25 公式计算子块相关度。"""
    settings = get_settings()
    terms = list(dict.fromkeys(tokenize(query, max_tokens=settings.search_max_query_terms)))
    if not terms or not document_version_ids:
        return []

    scope = [
        Document.knowledge_base_id == knowledge_base_id,
        DocumentChunk.level == "child",
        DocumentVersion.id.in_(document_version_ids),
        DocumentVersion.status == "completed",
        DocumentVersion.is_current.is_(True),
    ]
    if company_id is not None:
        scope.append(Document.company_id == company_id)
    if document_ids:
        scope.append(Document.id.in_(document_ids))

    corpus_size, average_length = (
        await session.execute(
            select(func.count(DocumentChunk.id), func.avg(DocumentChunk.token_count))
            .select_from(DocumentChunk)
            .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(*scope)
        )
    ).one()
    if not corpus_size:
        return []
    average_length = max(float(average_length or 0), 1.0)

    # 词项文档频率只在同一知识库的当前成功版本中统计。
    frequencies = (
        select(
            DocumentChunkTerm.term.label("term"),
            func.count(DocumentChunkTerm.chunk_id).label("document_frequency"),
        )
        .select_from(DocumentChunkTerm)
        .join(DocumentChunk, DocumentChunk.id == DocumentChunkTerm.chunk_id)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunkTerm.document_version_id)
        .join(Document, Document.id == DocumentChunkTerm.document_id)
        .where(*scope, DocumentChunkTerm.term.in_(terms))
        .group_by(DocumentChunkTerm.term)
        .subquery()
    )
    rows = await session.execute(
        select(
            DocumentChunk.id,
            DocumentChunk.token_count,
            DocumentChunkTerm.term,
            DocumentChunkTerm.frequency,
            frequencies.c.document_frequency,
        )
        .select_from(DocumentChunkTerm)
        .join(DocumentChunk, DocumentChunk.id == DocumentChunkTerm.chunk_id)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .join(frequencies, frequencies.c.term == DocumentChunkTerm.term)
        .where(*scope, DocumentChunkTerm.term.in_(terms))
    )

    k1, b = settings.bm25_k1, settings.bm25_b
    scores: defaultdict[UUID, float] = defaultdict(float)
    for chunk_id, length, _term, frequency, document_frequency in rows:
        df = int(document_frequency)
        inverse_frequency = math.log(1 + (corpus_size - df + 0.5) / (df + 0.5))
        normalized_tf = (frequency * (k1 + 1)) / (
            frequency + k1 * (1 - b + b * max(length, 1) / average_length)
        )
        scores[chunk_id] += inverse_frequency * normalized_tf

    return [
        BM25Hit(chunk_id=chunk_id, score=score)
        for chunk_id, score in sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))[
            :limit
        ]
    ]
