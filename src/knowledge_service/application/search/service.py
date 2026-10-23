"""在线检索用例：当前版本过滤、BM25/向量召回、RRF 融合与可选重排。"""

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from knowledge_service.application.search.bm25 import BM25Hit, search_bm25
from knowledge_service.core.config import get_settings
from knowledge_service.infrastructure.chroma.vector_store import query_chunks
from knowledge_service.infrastructure.model_providers.embeddings import embed_texts
from knowledge_service.infrastructure.model_providers.rerank import rerank_documents
from knowledge_service.infrastructure.persistence.models import (
    Document,
    DocumentAsset,
    DocumentChunk,
    DocumentVersion,
    KnowledgeBase,
    ModelProfile,
    VectorLibrary,
)


class KnowledgeSearchNotFound(LookupError):
    """知识库不存在或已归档。"""


class KnowledgeSearchDocumentsInvalid(ValueError):
    """请求中的文档不属于目标知识库或不存在。"""


class KnowledgeSearchModelUnavailable(RuntimeError):
    """知识库绑定的检索模型缺失或已停用。"""


def document_product_labels(
    metadata: dict | None,
) -> tuple[list[str], list[str]]:
    """解析文档元数据中的产品信息，兼容旧版单值字段与新版列表字段。

    返回 (产品名称列表, 产品型号列表)，已去重去空；无产品信息时返回两个空列表。
    """
    if not metadata:
        return [], []

    def collect(*keys: str) -> list[str]:
        labels: list[str] = []
        for key in keys:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                labels.append(value.strip())
            elif isinstance(value, list):
                labels.extend(
                    str(item).strip() for item in value if str(item).strip()
                )
        return list(dict.fromkeys(labels))

    return collect("product_names", "product_name"), collect("model_names", "model_name")


def _metadata_matches_product(
    metadata: dict | None, *, product_name: str | None, model_name: str | None
) -> bool:
    """判断文档是否满足产品名称与型号过滤条件。

    完全未标注产品的文档视为通用资料（公司信息等），任何产品过滤下都命中，
    由 rerank 决定相关性；已标注的文档必须包含过滤标签才命中。
    """
    product_names, model_names = document_product_labels(metadata)
    if not product_names and not model_names:
        return True
    if product_name and product_name.strip() not in product_names:
        return False
    if model_name and model_name.strip() not in model_names:
        return False
    return True


@dataclass
class _ParentResult:
    """一个父块及各召回分支记录的分数。"""

    chunk_id: UUID
    content: str
    document_id: UUID
    document_version_id: UUID
    version_number: int
    filename: str
    page_number: int | None
    section: str | None
    section_path: list[str] = field(default_factory=list)
    cosine_distance: float | None = None
    bm25_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    assets: list[DocumentAsset] | None = None
    context: str | None = None
    context_sources: list["_ContextItem"] = field(default_factory=list)
    # 完整章节模式下用于按原文顺序合并多个检索结果。
    context_ordinal: int | None = None
    # 后台文件系统中的稳定文件 ID，用于拼接原文件下载地址。
    file_id: str | None = None
    # 入库时记录的产品型号，仅用于在线问答的型号歧义判断。
    model_name: str | None = None
    # 同一文档可属于多个型号；合并新旧元数据格式后的完整型号列表。
    model_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _ContextItem:
    """一个命中父块或相邻父块的上下文片段。"""

    chunk_id: UUID
    relation: str
    content: str
    ordinal: int


async def search_knowledge_base(
    session: AsyncSession,
    *,
    knowledge_base_id: UUID,
    company_id: UUID | None = None,
    query: str,
    top_k: int,
    document_ids: list[UUID] | None,
    retrieval_mode: str = "hybrid",
    include_context: bool = False,
    expand_section: bool = False,
    section_query: str | None = None,
    product_name: str | None = None,
    model_name: str | None = None,
) -> list[_ParentResult]:
    """并行召回向量和关键词候选，再融合、父块去重并可选重排。"""
    settings = get_settings()
    knowledge_base = await session.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.status == "active",
        )
    )
    if knowledge_base is None:
        raise KnowledgeSearchNotFound
    if company_id is not None:
        if knowledge_base.company_id != company_id:
            raise KnowledgeSearchNotFound
        if knowledge_base.vector_library_id is None:
            raise KnowledgeSearchNotFound
        library = await session.scalar(
            select(VectorLibrary).where(
                VectorLibrary.id == knowledge_base.vector_library_id,
                VectorLibrary.company_id == company_id,
                VectorLibrary.status == "active",
            )
        )
        if library is None:
            raise KnowledgeSearchNotFound

    normalized_document_ids = list(dict.fromkeys(document_ids or [])) or None
    if normalized_document_ids:
        found_ids = set(
            await session.scalars(
                select(Document.id).where(
                    Document.knowledge_base_id == knowledge_base_id,
                    Document.id.in_(normalized_document_ids),
                    *([Document.company_id == company_id] if company_id is not None else []),
                )
            )
        )
        if found_ids != set(normalized_document_ids):
            raise KnowledgeSearchDocumentsInvalid

    # 产品过滤先把元数据解析为文档 ID 集合，再复用现有 document_ids 下推过滤，
    # Chroma 与 BM25 召回天然只覆盖匹配文档；过滤后零命中直接短路。
    if product_name or model_name:
        product_rows = await session.execute(
            select(Document.id, Document.extra_metadata).where(
                Document.knowledge_base_id == knowledge_base_id,
                *([Document.company_id == company_id] if company_id is not None else []),
            )
        )
        matching_ids = {
            document_id
            for document_id, metadata in product_rows
            if _metadata_matches_product(
                metadata, product_name=product_name, model_name=model_name
            )
        }
        if not matching_ids:
            return []
        if normalized_document_ids:
            normalized_document_ids = [
                document_id
                for document_id in normalized_document_ids
                if document_id in matching_ids
            ]
            if not normalized_document_ids:
                return []
        else:
            normalized_document_ids = list(matching_ids)

    version_query = (
        select(DocumentVersion.id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Document.knowledge_base_id == knowledge_base_id,
            *([Document.company_id == company_id] if company_id is not None else []),
            DocumentVersion.is_current.is_(True),
            DocumentVersion.status == "completed",
        )
        .order_by(DocumentVersion.id)
    )
    if normalized_document_ids:
        version_query = version_query.where(Document.id.in_(normalized_document_ids))
    version_ids = list(await session.scalars(version_query))
    if not version_ids:
        return []

    vector_hits: list[tuple[UUID, float]] = []
    bm25_hits: list[BM25Hit] = []
    embedding = None
    if retrieval_mode in {"hybrid", "vector"}:
        embedding = await session.get(ModelProfile, knowledge_base.embedding_model_id)
        if embedding is None or not embedding.is_enabled or embedding.kind != "embedding":
            raise KnowledgeSearchModelUnavailable("Embedding")
    if retrieval_mode == "hybrid":
        # BM25 查询使用数据库会话，Embedding 使用独立 HTTP 连接，可安全并发。
        embedded, bm25_hits = await asyncio.gather(
            embed_texts(embedding, [query]),
            search_bm25(
                session,
                knowledge_base_id=knowledge_base_id,
                company_id=company_id,
                document_version_ids=version_ids,
                document_ids=normalized_document_ids,
                query=query,
                limit=settings.search_candidate_k,
            ),
        )
        query_vector = embedded[0]
    elif retrieval_mode == "vector":
        query_vector = (await embed_texts(embedding, [query]))[0]
    else:
        bm25_hits = await search_bm25(
            session,
            knowledge_base_id=knowledge_base_id,
            company_id=company_id,
            document_version_ids=version_ids,
            document_ids=normalized_document_ids,
            query=query,
            limit=settings.search_candidate_k,
        )
    if retrieval_mode in {"hybrid", "vector"}:
        vector_hits = await asyncio.to_thread(
            query_chunks,
            embedding=embedding,
            knowledge_base_id=knowledge_base_id,
            company_id=company_id,
            vector_library_id=knowledge_base.vector_library_id,
            document_version_ids=version_ids,
            document_ids=normalized_document_ids,
            query_vector=query_vector,
            candidate_k=settings.search_candidate_k,
            version_batch_size=settings.search_version_filter_batch_size,
        )
    candidate_ids = list(
        dict.fromkeys(
            [chunk_id for chunk_id, _ in vector_hits] + [hit.chunk_id for hit in bm25_hits]
        )
    )
    if not candidate_ids:
        return []

    parent = aliased(DocumentChunk)
    rows = await session.execute(
        select(
            DocumentChunk.id,
            DocumentChunk.parent_chunk_id,
            Document.id.label("document_id"),
            Document.document_key.label("file_id"),
            Document.extra_metadata.label("document_metadata"),
            DocumentVersion.id.label("document_version_id"),
            DocumentVersion.version_number,
            DocumentVersion.filename,
            parent.id.label("parent_id"),
            parent.content.label("parent_content"),
            parent.page_number,
            parent.extra_metadata.label("parent_metadata"),
        )
        .select_from(DocumentChunk)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .join(parent, parent.id == DocumentChunk.parent_chunk_id)
        .where(
            DocumentChunk.id.in_(candidate_ids),
            DocumentChunk.level == "child",
            parent.level == "parent",
            Document.knowledge_base_id == knowledge_base_id,
            *([Document.company_id == company_id] if company_id is not None else []),
            DocumentVersion.is_current.is_(True),
            DocumentVersion.status == "completed",
        )
    )
    row_by_child = {row.id: row for row in rows}
    parent_ids = list({row.parent_id for row in row_by_child.values()})
    assets_by_chunk: dict[UUID, list[DocumentAsset]] = {}
    if parent_ids:
        asset_rows = await session.scalars(
            select(DocumentAsset)
            .join(Document, Document.id == DocumentAsset.document_id)
            .where(
                DocumentAsset.document_version_id.in_(
                    {row.document_version_id for row in row_by_child.values()}
                ),
                Document.knowledge_base_id == knowledge_base_id,
                *([Document.company_id == company_id] if company_id is not None else []),
            )
        )
        for asset in asset_rows:
            for row in row_by_child.values():
                if asset.document_version_id != row.document_version_id:
                    continue
                if (asset.marker and asset.marker in row.parent_content) or (
                    not asset.marker and asset.chunk_id == row.parent_id
                ):
                    linked = assets_by_chunk.setdefault(row.parent_id, [])
                    if asset not in linked:
                        linked.append(asset)
    vector_parent: dict[UUID, tuple[_ParentResult, int]] = {}
    for chunk_id, distance in sorted(vector_hits, key=lambda hit: (hit[1], str(hit[0]))):
        row = row_by_child.get(chunk_id)
        if row is None:
            continue
        if row.parent_id not in vector_parent:
            result = _parent_result(row, assets_by_chunk)
            result.cosine_distance = distance
            vector_parent[row.parent_id] = (result, len(vector_parent) + 1)
        elif distance < vector_parent[row.parent_id][0].cosine_distance:
            vector_parent[row.parent_id][0].cosine_distance = distance

    bm25_parent: dict[UUID, tuple[_ParentResult, int]] = {}
    for hit in bm25_hits:
        row = row_by_child.get(hit.chunk_id)
        if row is None:
            continue
        if row.parent_id not in bm25_parent:
            result = _parent_result(row, assets_by_chunk)
            result.bm25_score = hit.score
            bm25_parent[row.parent_id] = (result, len(bm25_parent) + 1)
        elif hit.score > bm25_parent[row.parent_id][0].bm25_score:
            bm25_parent[row.parent_id][0].bm25_score = hit.score

    combined: dict[UUID, _ParentResult] = {}
    for parent_id in dict.fromkeys([*vector_parent, *bm25_parent]):
        result = vector_parent.get(parent_id, bm25_parent.get(parent_id))[0]
        if parent_id in vector_parent:
            result.cosine_distance = vector_parent[parent_id][0].cosine_distance
        if parent_id in bm25_parent:
            result.bm25_score = bm25_parent[parent_id][0].bm25_score
        if retrieval_mode == "hybrid":
            vector_rank = vector_parent[parent_id][1] if parent_id in vector_parent else None
            bm25_rank = bm25_parent[parent_id][1] if parent_id in bm25_parent else None
            result.rrf_score = sum(
                weight / (settings.search_rrf_k + rank)
                for rank, weight in (
                    (vector_rank, settings.search_vector_weight),
                    (bm25_rank, settings.search_bm25_weight),
                )
                if rank is not None and weight > 0
            )
        combined[parent_id] = result

    if retrieval_mode == "vector":
        ranked = [item[0] for item in sorted(vector_parent.values(), key=lambda item: item[1])]
    elif retrieval_mode == "bm25":
        ranked = [item[0] for item in sorted(bm25_parent.values(), key=lambda item: item[1])]
    else:
        ranked = sorted(combined.values(), key=lambda item: item.rrf_score or 0, reverse=True)

    if retrieval_mode != "bm25" and knowledge_base.rerank_model_id and ranked:
        reranker = await session.get(ModelProfile, knowledge_base.rerank_model_id)
        if reranker is None or not reranker.is_enabled or reranker.kind != "rerank":
            raise KnowledgeSearchModelUnavailable("Rerank")
        reranked = await rerank_documents(
            reranker,
            query=query,
            documents=[item.content for item in ranked],
        )
        reranked_results = []
        for index, score in reranked:
            result = ranked[index]
            result.rerank_score = score
            reranked_results.append(result)
        ranked = reranked_results
    ranked = ranked[:top_k]
    if include_context:
        await _attach_context(
            session,
            ranked,
            settings,
            expand_section=expand_section,
            section_query=section_query,
        )
    return ranked


async def _attach_context(
    session: AsyncSession,
    results: list[_ParentResult],
    settings,
    *,
    expand_section: bool = False,
    section_query: str | None = None,
) -> None:
    """按同一文档版本和章节读取相邻块，必要时还原完整章节。"""
    for result in results:
        rows = list(
            await session.execute(
                select(
                    DocumentChunk.id,
                    DocumentChunk.content,
                    DocumentChunk.ordinal,
                    DocumentChunk.extra_metadata,
                )
                .where(
                    DocumentChunk.document_version_id == result.document_version_id,
                    DocumentChunk.level == "parent",
                )
                .order_by(DocumentChunk.ordinal)
            )
        )
        current_index = next(
            (index for index, row in enumerate(rows) if row.id == result.chunk_id), None
        )
        if current_index is None:
            continue
        current_metadata = rows[current_index].extra_metadata or {}
        current_section = current_metadata.get("section")
        current_section_path = _section_path_value(current_metadata)
        current_section_ids = _section_ids_value(current_metadata)
        if expand_section:
            if current_section_path or current_section:
                # 整体说明类问题优先还原匹配标题下的完整章节及其子章节。
                section_parts = current_section_path or [
                    part.strip() for part in str(current_section).split(" / ")
                ]
                query_text = (section_query or "").replace(" ", "")
                section_cues = (
                    "使用方法", "使用说明", "操作步骤", "测量步骤", "测量方法",
                    "操作说明", "注意事项",
                )
                anchor_index = next(
                    (
                        index
                        for index, part in enumerate(section_parts)
                        if any(
                            cue in query_text and cue in part.replace(" ", "")
                            for cue in section_cues
                        )
                    ),
                    len(section_parts) - 1,
                )
                section_prefix_parts = section_parts[: anchor_index + 1]
                section_prefix_ids = current_section_ids[: anchor_index + 1]
                section_rows = [
                    row
                    for row in rows
                    if _is_section_descendant(
                        row.extra_metadata or {},
                        section_prefix_parts,
                        section_prefix_ids,
                    )
                ]
            else:
                # 无标题文档按命中点两侧的连续无标题块扩展，避免一个短命中块
                # 直接变成完整答案，也不跨入相邻的已标记章节。
                start = current_index
                while start > 0 and not _has_section(rows[start - 1].extra_metadata or {}):
                    start -= 1
                end = current_index + 1
                while end < len(rows) and not _has_section(rows[end].extra_metadata or {}):
                    end += 1
                section_rows = rows[start:end]
            selected = [
                _ContextItem(
                    chunk_id=row.id,
                    relation=(
                        "hit"
                        if row.id == result.chunk_id
                        else "before"
                        if row.ordinal < rows[current_index].ordinal
                        else "after"
                    ),
                    content=row.content,
                    ordinal=row.ordinal,
                )
                for row in section_rows
            ]
            sources = _fit_context_window(
                selected,
                hit_chunk_id=result.chunk_id,
                max_chars=settings.search_max_context_chars,
            )
        else:
            before: list[tuple[object, str]] = []
            before_start = max(-1, current_index - settings.search_context_before - 1)
            for index in range(current_index - 1, before_start, -1):
                row = rows[index]
                if not _same_section(row.extra_metadata or {}, current_metadata):
                    break
                before.append((row, "before"))
            before.reverse()
            selected = [*before, (rows[current_index], "hit")]
            after_end = min(len(rows), current_index + settings.search_context_after + 1)
            for index in range(current_index + 1, after_end):
                row = rows[index]
                # 跨章节时停止向后扩展，避免把不同主题拼到同一上下文。
                if not _same_section(row.extra_metadata or {}, current_metadata):
                    break
                selected.append((row, "after"))
            sources = [
                _ContextItem(
                    chunk_id=row.id,
                    relation=relation,
                    content=row.content,
                    ordinal=row.ordinal,
                )
                for row, relation in selected
            ]
        remaining = settings.search_max_context_chars
        context_by_id: dict[UUID, str] = {}
        if expand_section:
            context_by_id = {item.chunk_id: item.content for item in sources}
            ordered_items: list[_ContextItem] = []
        else:
            # 普通检索优先保留命中块，再补充前后邻块。
            hit = next(item for item in sources if item.relation == "hit")
            hit_content = hit.content[:remaining]
            remaining -= len(hit_content)
            context_by_id = {hit.chunk_id: hit_content}
            ordered_items = [item for item in sources if item.relation != "hit"]
        for item in ordered_items:
            if remaining <= 0:
                break
            content = item.content[:remaining]
            context_by_id[item.chunk_id] = content
            remaining -= len(content)
            if len(content) < len(item.content):
                break
        if not expand_section:
            sources = [
                _ContextItem(
                    chunk_id=item.chunk_id,
                    relation=item.relation,
                    content=context_by_id[item.chunk_id],
                    ordinal=item.ordinal,
                )
                for item in sources
                if item.chunk_id in context_by_id
            ]
        context_parts = [
            context_by_id[item.chunk_id]
            for item in sources
            if item.chunk_id in context_by_id
        ]
        result.context = "\n\n".join(context_parts)
        result.context_sources = sources
        result.context_ordinal = min((item.ordinal for item in sources), default=None)
        # 相邻块里的图片也必须随上下文加载，不能只取最初命中父块的图片。
        assets = await session.scalars(
            select(DocumentAsset).where(
                DocumentAsset.document_version_id == result.document_version_id
            )
        )
        result.assets = [
            asset for asset in assets
            if (asset.marker and asset.marker in result.context)
            or (not asset.marker and asset.chunk_id in context_by_id)
        ]


def _fit_context_window(
    items: list[_ContextItem], *, hit_chunk_id: UUID, max_chars: int
) -> list[_ContextItem]:
    """完整章节超出上下文上限时，保留命中块并按原顺序扩展其邻近块。"""
    if not items:
        return []
    hit_index = next(
        (index for index, item in enumerate(items) if item.chunk_id == hit_chunk_id),
        None,
    )
    if hit_index is None:
        return []
    total_chars = sum(len(item.content) for item in items) + max(0, 2 * (len(items) - 1))
    if total_chars <= max_chars:
        return items

    hit = items[hit_index]
    if len(hit.content) >= max_chars:
        return [
            _ContextItem(
                chunk_id=hit.chunk_id,
                relation=hit.relation,
                content=hit.content[:max_chars],
                ordinal=hit.ordinal,
            )
        ]

    selected = {hit_index}
    used_chars = len(hit.content)
    left_index = hit_index - 1
    right_index = hit_index + 1
    left_open = left_index >= 0
    right_open = right_index < len(items)
    prefer_left = True
    while left_open or right_open:
        directions = ("left", "right") if prefer_left else ("right", "left")
        added = False
        for direction in directions:
            if direction == "left" and left_open:
                candidate_index = left_index
            elif direction == "right" and right_open:
                candidate_index = right_index
            else:
                continue
            candidate = items[candidate_index]
            separator_chars = 2 if selected else 0
            if used_chars + separator_chars + len(candidate.content) <= max_chars:
                selected.add(candidate_index)
                used_chars += separator_chars + len(candidate.content)
                if direction == "left":
                    left_index -= 1
                    left_open = left_index >= 0
                else:
                    right_index += 1
                    right_open = right_index < len(items)
                added = True
                break
            if direction == "left":
                left_open = False
            else:
                right_open = False
        if not added and not left_open and not right_open:
            break
        prefer_left = not prefer_left
    return [items[index] for index in sorted(selected)]


def _parent_result(
    row, assets_by_chunk: dict[UUID, list[DocumentAsset]] | None = None
) -> _ParentResult:
    """将 ORM 查询行转换为来源完整的父块候选。"""
    metadata = row.parent_metadata or {}
    _product_names, model_names = document_product_labels(row.document_metadata or {})
    return _ParentResult(
        chunk_id=row.parent_id,
        content=row.parent_content,
        document_id=row.document_id,
        document_version_id=row.document_version_id,
        version_number=row.version_number,
        filename=row.filename,
        file_id=row.file_id,
        model_name=model_names[0] if model_names else None,
        model_names=model_names,
        page_number=row.page_number,
        section=metadata.get("section"),
        section_path=_section_path_value(metadata),
        assets=(assets_by_chunk or {}).get(row.parent_id, []),
    )


def _section_path_value(metadata: dict) -> list[str]:
    """Return structured section labels, with compatibility for older chunks."""
    value = metadata.get("section_path")
    if isinstance(value, (list, tuple)):
        return [str(part) for part in value if str(part).strip()]
    section = metadata.get("section")
    return [part.strip() for part in str(section or "").split(" / ") if part.strip()]


def _section_ids_value(metadata: dict) -> list[str]:
    value = metadata.get("section_path_ids")
    return [str(part) for part in value if part] if isinstance(value, (list, tuple)) else []


def _has_section(metadata: dict) -> bool:
    return bool(_section_path_value(metadata) or _section_ids_value(metadata))


def _same_section(left: dict, right: dict) -> bool:
    left_ids, right_ids = _section_ids_value(left), _section_ids_value(right)
    if left_ids and right_ids:
        return left_ids == right_ids
    return left.get("section") == right.get("section")


def _is_section_descendant(
    metadata: dict,
    prefix: list[str],
    prefix_ids: list[str],
) -> bool:
    ids = _section_ids_value(metadata)
    if prefix_ids and ids:
        return ids[: len(prefix_ids)] == prefix_ids
    path = _section_path_value(metadata)
    return path[: len(prefix)] == prefix
