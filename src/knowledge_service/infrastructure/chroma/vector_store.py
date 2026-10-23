"""Chroma 本地持久化集合管理与幂等向量写入。"""

import hashlib
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from knowledge_service.core.config import get_settings
from knowledge_service.infrastructure.persistence.models import ModelProfile

logger = logging.getLogger(__name__)


class ChromaWriteError(RuntimeError):
    """Chroma 集合配置或向量写入失败。"""


class ChromaSearchError(RuntimeError):
    """Chroma 查询失败或返回结构无效。"""


def collection_name(embedding: ModelProfile) -> str:
    """按不可变 Embedding 档案分集合，避免不同向量空间混用。"""
    identity = f"{embedding.id}:{embedding.dimensions}:chunk-v1"
    return "kb_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


@lru_cache(maxsize=1)
def _client():
    """惰性创建进程内 Chroma 客户端，并把数据写入持久化目录。"""
    import chromadb

    directory = Path(get_settings().chroma_persist_directory)
    directory.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(directory))


def upsert_chunks(
    *,
    embedding: ModelProfile,
    knowledge_base_id: UUID,
    company_id: UUID | None = None,
    vector_library_id: UUID | None = None,
    source_metadata: dict[str, Any] | None = None,
    document_id: UUID,
    document_version_id: UUID,
    chunks: list[dict[str, Any]],
    vectors: list[list[float]],
) -> None:
    """写入文档子块；稳定 ID 使任务重试覆盖同一版本的旧向量。"""
    if len(chunks) != len(vectors) or not chunks:
        raise ChromaWriteError("CHROMA_BATCH_INVALID: 分块与向量数量不一致或为空")
    dimensions = embedding.dimensions
    if dimensions is None or dimensions < 1:
        raise ChromaWriteError("CHROMA_PROFILE_INVALID: Embedding 维度未配置")

    client = None
    collection = None
    write_started = False
    name = collection_name(embedding)
    metadata = {
        "hnsw:space": "cosine",
        "schema_version": 1,
        "embedding_model_id": str(embedding.id),
        "embedding_dimensions": dimensions,
    }
    try:
        client = _client()
        collection = client.get_or_create_collection(name=name, metadata=metadata)
        if any(collection.metadata.get(key) != value for key, value in metadata.items()):
            raise ChromaWriteError("CHROMA_SCHEMA_MISMATCH: 集合模型或向量维度不一致")

        version_filter = {"document_version_id": str(document_version_id)}
        # 清除上次中断可能留下的部分结果，再使用固定 ID 写入完整版本。
        collection.delete(where=version_filter)
        batch_size = get_settings().embedding_batch_size
        for start in range(0, len(chunks), batch_size):
            chunk_batch = chunks[start : start + batch_size]
            vector_batch = vectors[start : start + batch_size]
            if any(len(vector) != dimensions for vector in vector_batch):
                raise ChromaWriteError("CHROMA_DIMENSION_MISMATCH: 向量维度不匹配")
            write_started = True
            source_values = {
                key: str(value)
                for key, value in (source_metadata or {}).items()
                if key
                in {"source_file_id", "source_directory_id", "source_parent_id", "source_path"}
                and value is not None
            }
            collection.upsert(
                ids=[str(chunk["id"]) for chunk in chunk_batch],
                documents=[str(chunk["content"]) for chunk in chunk_batch],
                embeddings=vector_batch,
                metadatas=[
                    {
                        "company_id": str(company_id) if company_id else "",
                        "vector_library_id": str(vector_library_id) if vector_library_id else "",
                        "knowledge_base_id": str(knowledge_base_id),
                        "document_id": str(document_id),
                        "document_version_id": str(document_version_id),
                        "ordinal": int(chunk["ordinal"]),
                        "parent_chunk_id": str(chunk.get("parent_chunk_id") or ""),
                        **source_values,
                    }
                    for chunk in chunk_batch
                ],
            )
    except ChromaWriteError:
        if write_started and collection is not None:
            _clear_failed_version_write(collection, document_version_id)
        raise
    except Exception as exc:
        if write_started and collection is not None:
            _clear_failed_version_write(collection, document_version_id)
        raise ChromaWriteError("CHROMA_WRITE_ERROR: Chroma 本地向量写入失败") from exc


def query_chunks(
    *,
    embedding: ModelProfile,
    knowledge_base_id: UUID,
    company_id: UUID | None = None,
    vector_library_id: UUID | None = None,
    document_version_ids: list[UUID],
    document_ids: list[UUID] | None,
    query_vector: list[float],
    candidate_k: int,
    version_batch_size: int,
) -> list[tuple[UUID, float]]:
    """按知识库、当前版本与可选文档范围查询最近邻子块。"""
    if not document_version_ids:
        return []
    try:
        client = _client()
        collection = client.get_collection(name=collection_name(embedding))
        hits: dict[UUID, float] = {}
        for start in range(0, len(document_version_ids), version_batch_size):
            version_batch = document_version_ids[start : start + version_batch_size]
            filters: list[dict[str, Any]] = [
                {"knowledge_base_id": {"$eq": str(knowledge_base_id)}},
                {"document_version_id": {"$in": [str(item) for item in version_batch]}},
            ]
            if company_id:
                filters.append({"company_id": {"$eq": str(company_id)}})
            if vector_library_id:
                filters.append({"vector_library_id": {"$eq": str(vector_library_id)}})
            if document_ids:
                filters.append({"document_id": {"$in": [str(item) for item in document_ids]}})
            result = collection.query(
                query_embeddings=[query_vector],
                n_results=candidate_k,
                where={"$and": filters},
                include=["distances"],
            )
            ids = result.get("ids", [[]])[0]
            distances = result.get("distances", [[]])[0]
            if len(ids) != len(distances):
                raise ValueError("Chroma 查询结果缺少距离")
            for chunk_id, distance in zip(ids, distances, strict=True):
                key = UUID(chunk_id)
                hits[key] = min(float(distance), hits.get(key, float("inf")))
        return sorted(hits.items(), key=lambda item: (item[1], str(item[0])))[:candidate_k]
    except Exception as exc:
        if "does not exist" in str(exc).lower() or "not found" in str(exc).lower():
            # 尚无向量集合时仍允许 BM25 分支返回关键词结果。
            return []
        raise ChromaSearchError("CHROMA_SEARCH_ERROR: Chroma 向量查询失败") from exc


def delete_document_versions(*, embedding: ModelProfile, version_ids: list[UUID]) -> None:
    """删除一个逻辑文档所有版本对应的 Chroma 向量。"""
    if not version_ids:
        return
    try:
        client = _client()
        collection = client.get_collection(name=collection_name(embedding))
    except Exception as exc:
        # 集合尚未创建时，说明文档没有成功写入向量，可以视为已清理。
        if "does not exist" in str(exc).lower() or "not found" in str(exc).lower():
            return
        raise ChromaWriteError("CHROMA_DELETE_ERROR: Chroma 向量删除失败") from exc
    try:
        for version_id in version_ids:
            collection.delete(where={"document_version_id": str(version_id)})
    except Exception as exc:
        raise ChromaWriteError("CHROMA_DELETE_ERROR: Chroma 向量删除失败") from exc


def _clear_failed_version_write(collection, document_version_id: UUID) -> None:
    """写入失败时清理当前版本的部分向量，不影响其他文档和版本。"""
    try:
        collection.delete(where={"document_version_id": str(document_version_id)})
    except Exception:
        logger.warning("Chroma cleanup failed for a partial document version write")
