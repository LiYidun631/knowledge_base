"""章节边界 + 父子聚合切分的单元测试（chunk-v3 行为）。"""

from uuid import uuid4

from knowledge_service.application.documents.chunking import (
    CHUNK_SCHEMA_VERSION,
    ChunkParams,
    build_chunks,
)
from knowledge_service.application.documents.parsing import ParsedBlock


def _text(content: str, *, section: str | None = None, **metadata) -> ParsedBlock:
    extra = {"section_path": section.split(" / ")} if section else {}
    return ParsedBlock(kind="text", content=content, section=section, metadata={**extra, **metadata})


def _parents(chunks):
    return [chunk for chunk in chunks if chunk.metadata["level"] == "parent"]


def _children(chunks):
    return [chunk for chunk in chunks if chunk.metadata["level"] == "child"]


def test_adjacent_small_sections_merge_into_one_parent() -> None:
    blocks = [
        _text("产品简介", section="产品简介"),
        _text("本产品用于测量体温。"),
        _text("适用于家庭环境。"),
        _text("操作说明", section="操作说明"),
        _text("按下电源键开始测量。"),
    ]
    chunks = build_chunks(uuid4(), blocks)
    parents = _parents(chunks)
    assert len(parents) == 1
    assert parents[0].content.startswith("产品简介")
    assert "操作说明" in parents[0].content
    assert parents[0].metadata["schema_version"] == CHUNK_SCHEMA_VERSION


def test_section_boundary_flushes_when_buffer_reaches_child_limit() -> None:
    section_a = "产品简介"
    section_b = "操作说明"
    filler_a = "本产品用于测量体温，采用红外传感器。" * 18  # 约 324 字
    filler_b = "按下电源键即可开始测量。" * 30  # 约 360 字
    blocks = [
        _text(section_a, section=section_a),
        _text(filler_a, section=section_a),
        _text(filler_a, section=section_a),
        _text(section_b, section=section_b),
        _text(filler_b, section=section_b),
    ]
    chunks = build_chunks(uuid4(), blocks)
    parents = _parents(chunks)
    assert len(parents) == 2
    assert parents[0].content.startswith(section_a)
    assert section_b not in parents[0].content
    assert parents[1].content.startswith(section_b)
    assert parents[0].metadata["section"] == section_a
    assert parents[1].metadata["section"] == section_b


def test_large_section_splits_at_parent_limit_and_preserves_text() -> None:
    sentence = "内容" * 195 + "。"
    blocks = [_text(sentence, section="规格参数") for _ in range(10)]
    chunks = build_chunks(uuid4(), blocks)
    parents = _parents(chunks)
    assert len(parents) == 3
    assert max(len(parent.content) for parent in parents) <= 1800
    joined = "".join(parent.content for parent in parents)
    assert all(sentence in joined for sentence in [block.content for block in blocks])
    assert joined.count("。") == 10


def test_heading_text_merges_into_first_content_parent() -> None:
    blocks = [
        _text("1. 产品简介", section="1. 产品简介"),
        _text("本产品为红外体温计。" * 20),
        _text("使用前请阅读说明书。" * 20),
    ]
    chunks = build_chunks(uuid4(), blocks)
    parents = _parents(chunks)
    assert parents[0].content.startswith("1. 产品简介")
    assert all(parent.metadata["block_type"] == "text" for parent in parents)


def test_asset_marker_block_merges_into_surrounding_text() -> None:
    marker = "[[KB_ASSET_KEY:assets/abc123]]"
    blocks = [
        _text("测量步骤：按下电源键。", section="使用说明"),
        _text(marker, section="使用说明", asset_marker=marker),
        _text("将探头对准额头。" * 30, section="使用说明"),
    ]
    chunks = build_chunks(uuid4(), blocks)
    parents = _parents(chunks)
    assert len(parents) == 1
    assert marker in parents[0].content
    assert "按下电源键" in parents[0].content
    assert "将探头对准额头" in parents[0].content


def test_unsectioned_document_aggregates_by_parent_limit() -> None:
    blocks = [_text("内容" * 195 + "。") for _ in range(6)]
    chunks = build_chunks(uuid4(), blocks)
    parents = _parents(chunks)
    assert len(parents) == 2
    assert max(len(parent.content) for parent in parents) <= 1800


def test_tables_stay_independent_and_text_flushes_around_them() -> None:
    blocks = [
        _text("以下为规格表。", section="规格参数"),
        ParsedBlock(
            kind="table",
            content="参数 | 数值\n测量范围 | 32-42度\n精度 | ±0.2度",
            section="规格参数",
            metadata={"table_header": "参数 | 数值"},
        ),
        _text("使用前请阅读。", section="使用说明"),
    ]
    chunks = build_chunks(uuid4(), blocks)
    parents = _parents(chunks)
    assert len(parents) == 3
    assert parents[0].content == "以下为规格表。"
    assert parents[1].metadata["block_type"] == "table"
    assert parents[2].content == "使用前请阅读。"
    table_children = [
        chunk for chunk in _children(chunks) if chunk.metadata["block_type"] == "table"
    ]
    assert table_children
    assert all(chunk.content.startswith("参数 | 数值") for chunk in table_children)


def test_smaller_per_kb_params_still_bound_parent_and_child() -> None:
    content = "知识" * 400
    chunks = build_chunks(
        uuid4(),
        [_text(content)],
        ChunkParams(parent_chars=200, child_chars=60, overlap_chars=10),
    )
    assert max(len(chunk.content) for chunk in _parents(chunks)) <= 200
    assert max(len(chunk.content) for chunk in _children(chunks)) <= 60


def test_build_chunks_is_deterministic() -> None:
    blocks = [
        _text("产品简介", section="产品简介"),
        _text("本产品用于测量体温。"),
        _text("操作说明", section="操作说明"),
        _text("按下电源键开始测量。"),
        ParsedBlock(
            kind="table",
            content="参数 | 数值\n测量范围 | 32-42度",
            metadata={"table_header": "参数 | 数值"},
        ),
    ]
    document_id = uuid4()
    assert build_chunks(document_id, blocks) == build_chunks(document_id, blocks)
