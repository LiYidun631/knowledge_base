"""验证解析和父子切分保持正文、结构与稳定标识。"""

import io
from types import SimpleNamespace
from uuid import uuid4

import pytest
from docx import Document
from pypdf import PdfWriter

from knowledge_service.application.documents import parsing
from knowledge_service.application.documents.chunking import build_chunks
from knowledge_service.application.documents.parsing import (
    DocumentParseError,
    ParsedBlock,
    parse_document,
)
from knowledge_service.core.config import Settings


def test_child_overlap_preserves_all_source_text() -> None:
    """超长文本跨子块时不能因添加重叠而丢掉任何字符。"""
    text = "".join(chr(0x4E00 + index) for index in range(1600))
    document_id = uuid4()
    chunks = build_chunks(document_id, [ParsedBlock(kind="text", content=text, page_number=3)])
    children = [chunk for chunk in chunks if chunk.metadata["level"] == "child"]
    assert all(len(chunk.content) <= 600 for chunk in children)
    assert all(chunk.page_number == 3 for chunk in children)
    assert all(character in "".join(chunk.content for chunk in children) for character in text)
    assert chunks == build_chunks(
        document_id, [ParsedBlock(kind="text", content=text, page_number=3)]
    )


def test_csv_split_keeps_header_and_every_row() -> None:
    """所有表格子块应带列头，完整保留每个数据行。"""
    rows = [f"patient-{index:04d},诊断内容{index}" for index in range(100)]
    blocks = parse_document("data.csv", ("姓名,诊断\n" + "\n".join(rows)).encode())
    children = [
        chunk for chunk in build_chunks(uuid4(), blocks) if chunk.metadata["level"] == "child"
    ]
    assert len(children) > 1
    assert all(chunk.content.startswith("姓名 | 诊断\n") for chunk in children)
    assert all(len(chunk.content) <= 600 for chunk in children)
    joined = "\n".join(chunk.content for chunk in children)
    assert all(f"patient-{index:04d}" in joined for index in range(100))


def test_docx_preserves_paragraph_table_order_and_section() -> None:
    """Word 表格前后的段落应保持原始顺序与章节来源。"""
    word = Document()
    word.add_heading("诊疗方案", level=1)
    word.add_paragraph("表格前的说明")
    table = word.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "结果"
    table.cell(1, 0).text = "血压"
    table.cell(1, 1).text = "正常"
    word.add_paragraph("表格后的说明")
    stream = io.BytesIO()
    word.save(stream)
    blocks = parse_document("说明.docx", stream.getvalue())
    assert [block.kind for block in blocks] == ["text", "text", "table", "text"]
    assert blocks[-1].section == "诊疗方案"
    assert "项目 | 结果" in blocks[2].content


def test_pdf_without_text_requires_ocr(monkeypatch) -> None:
    """无文字的 PDF 不应当被误判为已经完成入库。"""
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)
    monkeypatch.setattr(
        parsing,
        "get_settings",
        lambda: SimpleNamespace(ocr_model_name=""),
    )
    with pytest.raises(DocumentParseError, match="OCR_REQUIRED"):
        parse_document("scan.pdf", stream.getvalue())


def test_scanned_pdf_uses_qianfan_and_keeps_page_number(monkeypatch) -> None:
    """扫描 PDF 只将无文字页交给千帆识别，并保留原始页码。"""
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)
    pdf_bytes = stream.getvalue()
    settings = SimpleNamespace(ocr_model_name="qianfan-ocr")
    calls = []

    def fake_recognize(content, *, settings, filename, page_numbers):
        calls.append((content, settings, filename, page_numbers))
        return {1: "扫描报告正文"}

    monkeypatch.setattr(parsing, "get_settings", lambda: settings)
    monkeypatch.setattr(parsing, "recognize_pdf_pages", fake_recognize)

    blocks = parse_document("scan.pdf", pdf_bytes)

    assert [(block.content, block.page_number) for block in blocks] == [("扫描报告正文", 1)]
    assert calls == [(pdf_bytes, settings, "document.pdf", [1])]


def test_html_removes_scripts_and_avoids_duplicate_table_cells() -> None:
    """HTML 的脚本和表格内部段落不能重复进入正文。"""
    source = (
        "<h1>指南</h1><script>secret</script><p>正文</p>"
        "<table><tr><th>列名</th></tr><tr><td><p>数据</p></td></tr></table>"
    )
    blocks = parse_document("guide.html", source.encode())
    assert [block.kind for block in blocks] == ["text", "table"]
    assert all("secret" not in block.content for block in blocks)
    assert blocks[0].section == "指南"


def test_invalid_chunk_parameters_rejected() -> None:
    """阻止无效的重叠与分块长度配置。"""
    with pytest.raises(ValueError, match="子块重叠"):
        Settings(child_chunk_chars=100, child_chunk_overlap_chars=100)
