"""将支持的文档格式解析为有序、带来源信息的结构块。"""

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from docx import Document as WordDocument
from pypdf import PdfReader

from knowledge_service.core.config import get_settings
from knowledge_service.infrastructure.ocr_client import (
    OcrServiceError,
    recognize_pdf_pages,
)


class DocumentParseError(ValueError):
    """文档无法安全解析，或需要尚未配置的 OCR 服务。"""


@dataclass(frozen=True)
class ParsedBlock:
    """保留页码、章节与表格结构的单个解析块。"""

    kind: str
    content: str
    page_number: int | None = None
    section: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_document(filename: str, content: bytes) -> list[ParsedBlock]:
    """按文件扩展名选择解析器，并拒绝空文档和无文本扫描件。"""
    extension = Path(filename).suffix.lower()
    parsers = {
        ".txt": _parse_text,
        ".md": _parse_markdown,
        ".html": _parse_html,
        ".htm": _parse_html,
        ".csv": _parse_csv,
        ".docx": _parse_docx,
        ".pdf": _parse_pdf,
    }
    parser = parsers.get(extension)
    if parser is None:
        raise DocumentParseError("UNSUPPORTED_FILE_TYPE: 不支持的文件类型")
    blocks = parser(content)
    blocks = [block for block in blocks if block.content.strip()]
    if not blocks:
        raise DocumentParseError("EMPTY_DOCUMENT: 文档未提取到可用文本")
    return blocks


def _decode_text(content: bytes) -> str:
    """兼容常见中文文本编码，不静默丢弃无法解码的字节。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentParseError("TEXT_ENCODING_ERROR: 文本编码无法识别")


def _clean_text(value: str) -> str:
    """清理空字符与行尾空格，保留段落边界和原始语义。"""
    value = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in value.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _parse_text(content: bytes) -> list[ParsedBlock]:
    text = _clean_text(_decode_text(content))
    return [ParsedBlock(kind="text", content=part) for part in re.split(r"\n\s*\n", text)]


def _parse_markdown(content: bytes) -> list[ParsedBlock]:
    """保留 Markdown 标题，并把标题路径附着到后续内容块。"""
    text = _clean_text(_decode_text(content))
    headings: list[tuple[int, str]] = []
    blocks: list[ParsedBlock] = []
    for paragraph in re.split(r"\n\s*\n", text):
        lines = paragraph.splitlines()
        body_lines: list[str] = []
        for line in lines:
            match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if match and not body_lines:
                level, title = len(match.group(1)), match.group(2)
                headings = [item for item in headings if item[0] < level]
                headings.append((level, title))
            else:
                body_lines.append(line)
        body = _clean_text("\n".join(body_lines))
        if body:
            blocks.append(
                ParsedBlock(kind="text", content=body, section=" / ".join(x[1] for x in headings))
            )
    return blocks


def _parse_html(content: bytes) -> list[ParsedBlock]:
    """去除页面脚本等非正文内容，同时保留标题层级和 HTML 表格。"""
    soup = BeautifulSoup(content, "html.parser")
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    root = soup.body or soup
    blocks: list[ParsedBlock] = []
    headings: list[tuple[int, str]] = []
    for node in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table"]):
        if node.name != "table" and node.find_parent("table"):
            continue
        if node.name == "p" and node.find_parent("li"):
            continue
        if node.name.startswith("h"):
            level = int(node.name[1])
            title = _clean_text(node.get_text(" ", strip=True))
            headings = [item for item in headings if item[0] < level]
            headings.append((level, title))
        elif node.name == "table":
            rows = [
                [_clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
                for row in node.find_all("tr")
            ]
            rows = [row for row in rows if any(row)]
            if rows:
                header = rows[0]
                rendered = [" | ".join(header)]
                rendered.extend(" | ".join(row) for row in rows[1:])
                blocks.append(
                    ParsedBlock(
                        kind="table",
                        content="\n".join(rendered),
                        section=" / ".join(x[1] for x in headings) or None,
                        metadata={"table_header": " | ".join(header)},
                    )
                )
        else:
            text = _clean_text(node.get_text(" ", strip=True))
            if text:
                blocks.append(
                    ParsedBlock(
                        kind="text",
                        content=text,
                        section=" / ".join(x[1] for x in headings) or None,
                    )
                )
    if not blocks:
        return [ParsedBlock(kind="text", content=_clean_text(root.get_text("\n", strip=True)))]
    return blocks


def _parse_csv(content: bytes) -> list[ParsedBlock]:
    """将 CSV 记录转成可读行，分块时会为每段重复列名。"""
    text = _decode_text(content)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text), dialect)]
    rows = [row for row in rows if any(row)]
    if len(rows) < 2:
        return [ParsedBlock(kind="text", content="\n".join(" | ".join(row) for row in rows))]
    header = " | ".join(rows[0])
    return [
        ParsedBlock(
            kind="table",
            content="\n".join([header, *(" | ".join(row) for row in rows[1:])]),
            metadata={"table_header": header},
        )
    ]


def _parse_docx(content: bytes) -> list[ParsedBlock]:
    """按文档原始顺序读取 Word 段落和表格。"""
    try:
        document = WordDocument(io.BytesIO(content))
    except Exception as exc:
        raise DocumentParseError("DOCX_PARSE_ERROR: Word 文档无法解析") from exc
    blocks: list[ParsedBlock] = []
    headings: list[tuple[int, str]] = []
    for item in document.iter_inner_content():
        if hasattr(item, "rows"):
            rows = [[cell.text.strip() for cell in row.cells] for row in item.rows]
            if rows:
                header = " | ".join(rows[0])
                blocks.append(
                    ParsedBlock(
                        kind="table",
                        content="\n".join([header, *(" | ".join(row) for row in rows[1:])]),
                        metadata={"table_header": header},
                    )
                )
        else:
            text = _clean_text(item.text)
            if text:
                style = getattr(item.style, "name", "")
                match = re.match(r"Heading\s+(\d+)", style)
                if match:
                    level = int(match.group(1))
                    headings = [entry for entry in headings if entry[0] < level]
                    headings.append((level, text))
                section = " / ".join(entry[1] for entry in headings) or None
                blocks.append(ParsedBlock(kind="text", content=text, section=section))
    return blocks


def _parse_pdf(content: bytes) -> list[ParsedBlock]:
    """保留原生文本和页码；无文字页通过百度千帆视觉模型识别。"""
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise DocumentParseError("PDF_ENCRYPTED: 暂不支持加密 PDF")
        page_texts = [_clean_text(page.extract_text() or "") for page in reader.pages]
        missing_pages = [index for index, text in enumerate(page_texts, start=1) if not text]
        if missing_pages:
            settings = get_settings()
            if not settings.ocr_model_name.strip():
                raise DocumentParseError(
                    "OCR_REQUIRED: 请在 .env 中配置 KNOWLEDGE_SERVICE_OCR_MODEL_NAME"
                )
            recognized = recognize_pdf_pages(
                content,
                settings=settings,
                filename="document.pdf",
                page_numbers=missing_pages,
            )
            for page_number in missing_pages:
                if page_number not in recognized:
                    raise DocumentParseError(
                        f"OCR_INVALID_RESPONSE: 千帆 OCR 未返回第 {page_number} 页"
                    )
                page_texts[page_number - 1] = _clean_text(recognized[page_number])
        blocks = [
            ParsedBlock(kind="text", content=text, page_number=index)
            for index, text in enumerate(page_texts, start=1)
        ]
    except DocumentParseError:
        raise
    except OcrServiceError as exc:
        raise DocumentParseError(str(exc)) from exc
    except Exception as exc:
        raise DocumentParseError("PDF_PARSE_ERROR: PDF 文档无法解析") from exc
    if not any(block.content for block in blocks):
        raise DocumentParseError("EMPTY_DOCUMENT: PDF 页内未提取到可用文字")
    return blocks
