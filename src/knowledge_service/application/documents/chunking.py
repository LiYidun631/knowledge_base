"""结构化文本切分与可重复生成的父子分块标识。"""

import hashlib
import re
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from knowledge_service.application.documents.parsing import ParsedBlock
from knowledge_service.core.config import get_settings

CHUNK_SCHEMA_VERSION = "chunk-v2"


@dataclass(frozen=True)
class Chunk:
    """供关系库保存并写入向量库的分块记录。"""

    id: UUID
    ordinal: int
    content: str
    page_number: int | None
    metadata: dict[str, str | int | None]


def build_chunks(document_id: UUID, blocks: list[ParsedBlock]) -> list[Chunk]:
    """按结构块生成 parent/child；表格子块始终携带列头。"""
    settings = get_settings()
    if settings.child_chunk_chars <= settings.child_chunk_overlap_chars:
        raise ValueError("child_chunk_chars must be greater than its overlap")

    chunks: list[Chunk] = []
    ordinal = 0
    for block_index, block in enumerate(blocks):
        if block.kind == "table":
            parent_parts = _table_parts(block, settings.parent_chunk_chars)
        else:
            parent_parts = _split_text(block.content, settings.parent_chunk_chars)
        for parent_index, parent_text in enumerate(parent_parts):
            parent_id = _stable_id(document_id, "parent", block_index, parent_index, parent_text)
            base_metadata: dict[str, str | int | None] = {
                "schema_version": CHUNK_SCHEMA_VERSION,
                "block_type": block.kind,
                "section": block.section,
                "block_index": block_index,
            }
            parent_metadata = base_metadata | {"level": "parent"}
            chunks.append(
                Chunk(parent_id, ordinal, parent_text, block.page_number, parent_metadata)
            )
            ordinal += 1

            if block.kind == "table":
                children = _table_parts(block, settings.child_chunk_chars, source=parent_text)
            else:
                children = _split_with_overlap(
                    parent_text,
                    settings.child_chunk_chars,
                    settings.child_chunk_overlap_chars,
                )
            for child_index, child_text in enumerate(children):
                child_id = _stable_id(parent_id, "child", child_index, 0, child_text)
                child_metadata = base_metadata | {
                    "level": "child",
                    "parent_chunk_id": str(parent_id),
                }
                chunks.append(
                    Chunk(child_id, ordinal, child_text, block.page_number, child_metadata)
                )
                ordinal += 1
    return chunks


def _stable_id(owner_id: UUID, level: str, index: int, part: int, text: str) -> UUID:
    """把文档版本、切分版本、序号与正文摘要纳入稳定 ID。"""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    identity = f"{owner_id}:{CHUNK_SCHEMA_VERSION}:{level}:{index}:{part}:{digest}"
    return uuid5(NAMESPACE_URL, identity)


def _split_text(text: str, limit: int) -> list[str]:
    """优先按句子或空白边界切分，超长句才按字符上限切开。"""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    sentences = re.split(r"(?<=[。！？!?；;\n])", text)
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        if not sentence:
            continue
        if len(sentence) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.extend(_split_long_sentence(sentence, limit))
            continue
        if current and len(current) + len(sentence) > limit:
            parts.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        parts.append(current)
    return parts


def _split_long_sentence(text: str, limit: int) -> list[str]:
    """长句优先寻找空白边界，中文连续文本按字符安全切分。"""
    result: list[str] = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        if cut < limit * 0.6:
            cut = limit
        result.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        result.append(text)
    return result


def _split_with_overlap(text: str, limit: int, overlap: int) -> list[str]:
    """在相邻 child 间保留有限重叠，避免边界处丢失语义。"""
    parts = _split_text(text, limit - overlap)
    if len(parts) < 2 or overlap == 0:
        return parts
    output = [parts[0]]
    for part in parts[1:]:
        prefix = output[-1][-overlap:]
        output.append(prefix + part)
    return output


def _table_parts(block: ParsedBlock, limit: int, source: str | None = None) -> list[str]:
    """按完整行分组并重复表头，不把表格数据切成无上下文的裸文本。"""
    text = source or block.content
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = str(block.metadata.get("table_header") or lines[0])
    if len(header) >= limit:
        raise ValueError("表格列头超过分块上限，请增大分块参数")
    rows = lines[1:] if lines[0] == header else lines
    parts: list[str] = []
    current: list[str] = [header]
    for row in rows:
        if len(header) + len(row) + 1 > limit:
            if len(current) > 1:
                parts.append("\n".join(current))
            current = [header, *_split_text(row, max(1, limit - len(header) - 1))]
            parts.extend("\n".join([header, piece]) for piece in current[1:-1])
            current = [header, current[-1]]
            continue
        candidate = "\n".join([*current, row])
        if len(candidate) > limit and len(current) > 1:
            parts.append("\n".join(current))
            current = [header, row]
        else:
            current.append(row)
    if len(current) > 1 or not parts:
        parts.append("\n".join(current))
    return parts
