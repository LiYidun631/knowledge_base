"""结构化文本切分与可重复生成的父子分块标识。"""

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from knowledge_service.application.documents.parsing import ParsedBlock
from knowledge_service.core.config import get_settings

CHUNK_SCHEMA_VERSION = "chunk-v3"
_IMAGE_REFERENCE = re.compile(r"\[\[KB_ASSET(?:_KEY)?:[^\]]+\]\]|!\[[^\]]*\]\([^\n]*?\)")


@dataclass(frozen=True)
class Chunk:
    """供关系库保存并写入向量库的分块记录。"""

    id: UUID
    ordinal: int
    content: str
    page_number: int | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ChunkParams:
    """生效的分块参数；per-KB 配置与全局默认合并后的最终值。"""

    parent_chars: int
    child_chars: int
    overlap_chars: int

    def __post_init__(self) -> None:
        if self.overlap_chars >= self.child_chars:
            raise ValueError("子块重叠长度必须小于子块长度")
        if self.parent_chars < self.child_chars:
            raise ValueError("父块长度不能小于子块长度")


def resolve_chunk_params(
    *,
    parent: int | None,
    child: int | None,
    overlap: int | None,
) -> ChunkParams | None:
    """合并 per-KB 参数与全局默认；全部未配置时返回 None 表示跟随全局。"""
    if parent is None and child is None and overlap is None:
        return None
    settings = get_settings()
    return ChunkParams(
        parent_chars=parent or settings.parent_chunk_chars,
        child_chars=child or settings.child_chunk_chars,
        overlap_chars=overlap if overlap is not None else settings.child_chunk_overlap_chars,
    )


def _section_key(block: ParsedBlock) -> tuple[str, ...]:
    """块的章节路径键：跨块聚合只在同一章节内发生。"""
    path = block.metadata.get("section_path")
    if isinstance(path, (list, tuple)):
        key = tuple(str(part).strip() for part in path if str(part).strip())
        if key:
            return key
    section = str(block.section or "").strip()
    return (section,) if section else ()


def build_chunks(
    document_id: UUID,
    blocks: list[ParsedBlock],
    params: ChunkParams | None = None,
) -> list[Chunk]:
    """按结构块生成 parent/child；表格子块始终携带列头。

    文本块按章节聚合：相邻同章节块流式累积到父块上限才切分，
    章节变化只在缓冲区达到子块下限时才落盘，标题与图片占位块
    因此并入相邻正文，不再产生独立碎片块。无章节文档退化为
    纯按上限聚合。表格块保持独立的行分组切分，不参与文本聚合。
    """
    if params is None:
        settings = get_settings()
        params = ChunkParams(
            parent_chars=settings.parent_chunk_chars,
            child_chars=settings.child_chunk_chars,
            overlap_chars=settings.child_chunk_overlap_chars,
        )
    parent_limit = params.parent_chars
    child_limit = params.child_chars
    overlap = params.overlap_chars

    chunks: list[Chunk] = []
    ordinal = 0
    buffer: list[ParsedBlock] = []
    buffer_start_index = 0
    buffer_chars = 0

    def emit_parent(
        parent_id: UUID,
        parent_text: str,
        base_metadata: dict[str, Any],
        page_number: int | None,
        *,
        child_parts: list[str],
    ) -> None:
        nonlocal ordinal
        parent_metadata = base_metadata | {"level": "parent"}
        chunks.append(Chunk(parent_id, ordinal, parent_text, page_number, parent_metadata))
        ordinal += 1
        for child_index, child_text in enumerate(child_parts):
            child_id = _stable_id(parent_id, "child", child_index, 0, child_text)
            child_metadata = base_metadata | {
                "level": "child",
                "parent_chunk_id": str(parent_id),
            }
            chunks.append(Chunk(child_id, ordinal, child_text, page_number, child_metadata))
            ordinal += 1

    def flush_text() -> None:
        """把累积的文本块按父块上限切分落盘；空缓冲直接返回。"""
        nonlocal buffer, buffer_start_index, buffer_chars
        if not buffer:
            return
        text = "\n".join(block.content for block in buffer)
        first = buffer[0]
        base_metadata = first.metadata | {
            "schema_version": CHUNK_SCHEMA_VERSION,
            "block_type": first.kind,
            "section": first.section,
            "block_index": buffer_start_index,
        }
        parts = _split_text(text, parent_limit)
        for part_index, parent_text in enumerate(parts):
            parent_id = _stable_id(
                document_id, "parent", buffer_start_index, part_index, parent_text
            )
            emit_parent(
                parent_id,
                parent_text,
                base_metadata,
                first.page_number,
                child_parts=_split_with_overlap(parent_text, child_limit, overlap),
            )
        buffer = []
        buffer_chars = 0

    for block_index, block in enumerate(blocks):
        if block.kind == "table":
            # 表格保持行分组+重复表头的独立切分，不并入文本聚合。
            flush_text()
            base_metadata = block.metadata | {
                "schema_version": CHUNK_SCHEMA_VERSION,
                "block_type": block.kind,
                "section": block.section,
                "block_index": block_index,
            }
            for parent_index, parent_text in enumerate(_table_parts(block, parent_limit)):
                parent_id = _stable_id(
                    document_id, "parent", block_index, parent_index, parent_text
                )
                emit_parent(
                    parent_id,
                    parent_text,
                    base_metadata,
                    block.page_number,
                    child_parts=_table_parts(block, child_limit, source=parent_text),
                )
            continue
        # 章节变化且已积累足够内容时落盘；标题块自带新章节路径，
        # 因此标题文字会并入其后的正文父块开头。
        if (
            buffer
            and buffer_chars >= child_limit
            and _section_key(block) != _section_key(buffer[-1])
        ):
            flush_text()
        if not buffer:
            buffer_start_index = block_index
        buffer.append(block)
        buffer_chars += len(block.content)
    flush_text()
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
        # 图片引用是不可拆分的原子，不能按字符截断 URL 或占位符。
        for match in _IMAGE_REFERENCE.finditer(text):
            if match.start() < cut < match.end():
                cut = match.start() or match.end()
                break
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
        previous = output[-1]
        start = max(0, len(previous) - overlap)
        for match in _IMAGE_REFERENCE.finditer(previous):
            if match.start() < start < match.end():
                start = match.end()
                break
        prefix = previous[start:]
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
