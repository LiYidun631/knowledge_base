"""调用百度千帆兼容视觉模型，识别扫描 PDF 中没有原生文字的页面。"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from bs4 import BeautifulSoup

from knowledge_service.core.config import resolve_secret

T = TypeVar("T")

_PADDLEOCR_ENDPOINT_PATH = "ocr/paddleocr"
_PADDLEOCR_BATCH_MAX_PAGES = 10
_PADDLEOCR_IMG_SRC_RE = re.compile(r"img_in_image_box_(\d+)_(\d+)_(\d+)_(\d+)")
_PADDLEOCR_SKIP_LABELS = {"number", "footer", "header", "vision_footnote", "page_footer"}
_PADDLEOCR_HEADING_LABELS = {"doc_title", "paragraph_title", "title"}

_LAYOUT_JSON_PROMPT = (
    "请识别页面版面，包括双栏/三栏、侧栏、页眉页脚、表格和图文混排。"
    "严格只返回 JSON："
    '{"blocks":[{"kind":"heading|text|table|image","text":"...",'
    '"heading_level":1,"bbox":[左,上,右,下]}],"markdown":"..."}。'
    "bbox 使用 0 到 1000 的归一化坐标 [x0,y0,x1,y1]。"
    "先按栏分组，同一栏从上到下，多栏按从左到右；跨栏标题和表格作为整体。"
    "明显章节标题使用 heading 类型并给出从 1 开始的 heading_level；"
    "普通编号步骤仍使用 text 类型。"
    "表格转 Markdown；图片的 text 写准确的图片说明，"
    "必须抄录图片中的标题、编号、标签、箭头指向和部件关系，"
    "尤其要识别工作原理图、操作示意图和带文字的图文混排内容。"
    "如果图片与文字在同一表格单元格，保留单元格文字并补充图片说明。"
)

_PLAIN_OCR_PROMPT = (
    "请按阅读顺序提取页面中的全部文字内容，保留段落换行；"
    "表格内容按行列输出为 Markdown 表格。"
)


def _default_prompt(model_name: str) -> str:
    """专用 OCR 任务模型只做文字提取；通用视觉模型才要求版面 JSON。"""
    return _PLAIN_OCR_PROMPT if "ocr" in model_name.lower() else _LAYOUT_JSON_PROMPT


def _is_paddleocr_model(model_name: str) -> bool:
    """千帆 PaddleOCR-VL 走专用 OCR 接口，与 OpenAI 兼容聊天通道不同。"""
    return "paddleocr" in model_name.lower()


class OcrServiceError(RuntimeError):
    """视觉 OCR 未配置、请求失败或返回了无效结果。"""


@dataclass(frozen=True)
class LayoutBlock:
    """千帆视觉模型返回的一个版面块。"""

    kind: str
    text: str
    bbox: tuple[float, float, float, float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LayoutResult:
    """一次视觉分析的结构化结果。"""

    blocks: tuple[LayoutBlock, ...]
    markdown: str


def analyze_image(
    content: bytes,
    *,
    settings: Any,
    filename: str = "page.png",
    prompt: str | None = None,
) -> LayoutResult:
    """调用视觉模型识别页面版面，或按调用方指定任务解释单张图片。"""
    if not content:
        raise OcrServiceError("OCR_INVALID_FILE: 图片内容为空")
    return _analyze_image(content, settings, prompt=prompt)


def recognize_image(content: bytes, *, settings: Any, filename: str = "page.png") -> str:
    """将单页图片以 Base64 图片消息发送给千帆视觉模型。"""
    if not content:
        raise OcrServiceError("OCR_INVALID_FILE: 图片内容为空")
    return analyze_image(content, settings=settings, filename=filename).markdown


def analyze_pdf_pages(
    content: bytes,
    *,
    settings: Any,
    filename: str = "document.pdf",
    page_numbers: list[int] | None = None,
) -> dict[int, LayoutResult]:
    """渲染指定 PDF 页并返回结构化版面结果。"""
    if not content:
        raise OcrServiceError("OCR_INVALID_FILE: PDF 内容为空")
    if _is_paddleocr_model(str(getattr(settings, "ocr_model_name", ""))):
        return _analyze_pdf_paddleocr(content, settings=settings, page_numbers=page_numbers)
    try:
        import fitz

        document = fitz.open(stream=content, filetype="pdf")
        try:
            selected_pages = page_numbers or list(range(1, len(document) + 1))
            if any(
                not isinstance(number, int)
                or number < 1
                or number > len(document)
                for number in selected_pages
            ):
                raise OcrServiceError("OCR_INVALID_PAGE: PDF 页码超出范围")
            scale = float(settings.ocr_render_dpi) / 72
            matrix = fitz.Matrix(scale, scale)
            results: dict[int, LayoutResult] = {}
            batch_size = _pdf_page_concurrency(settings)
            for start in range(0, len(selected_pages), batch_size):
                batch_numbers = selected_pages[start : start + batch_size]
                page_images = {
                    page_number: document[page_number - 1]
                    .get_pixmap(matrix=matrix, alpha=False)
                    .tobytes("png")
                    for page_number in batch_numbers
                }
                results.update(_run_pdf_image_batch(page_images, settings, _analyze_image))
            return {page_number: results[page_number] for page_number in selected_pages}
        finally:
            document.close()
    except OcrServiceError:
        raise
    except Exception as exc:
        raise OcrServiceError("OCR_PDF_RENDER_ERROR: PDF 页面渲染失败") from exc


def recognize_pdf_pages(
    content: bytes,
    *,
    settings: Any,
    filename: str = "document.pdf",
    page_numbers: list[int] | None = None,
) -> dict[int, str]:
    """只渲染并识别指定 PDF 页，返回从 1 开始的页码和识别文字。"""
    if not content:
        raise OcrServiceError("OCR_INVALID_FILE: PDF 内容为空")
    if _is_paddleocr_model(str(getattr(settings, "ocr_model_name", ""))):
        layouts = _analyze_pdf_paddleocr(content, settings=settings, page_numbers=page_numbers)
        return {page_number: layout.markdown for page_number, layout in layouts.items()}
    try:
        import fitz

        document = fitz.open(stream=content, filetype="pdf")
        try:
            selected_pages = page_numbers or list(range(1, len(document) + 1))
            if any(
                isinstance(number, bool)
                or not isinstance(number, int)
                or number < 1
                or number > len(document)
                for number in selected_pages
            ):
                raise OcrServiceError("OCR_INVALID_PAGE: PDF 页码超出范围")
            scale = float(settings.ocr_render_dpi) / 72
            matrix = fitz.Matrix(scale, scale)
            results: dict[int, str] = {}
            batch_size = _pdf_page_concurrency(settings)
            for start in range(0, len(selected_pages), batch_size):
                batch_numbers = selected_pages[start : start + batch_size]
                page_images = {
                    page_number: document[page_number - 1]
                    .get_pixmap(matrix=matrix, alpha=False)
                    .tobytes("png")
                    for page_number in batch_numbers
                }
                results.update(_run_pdf_image_batch(page_images, settings, _recognize_image))
            return {page_number: results[page_number] for page_number in selected_pages}
        finally:
            document.close()
    except OcrServiceError:
        raise
    except Exception as exc:
        raise OcrServiceError("OCR_PDF_RENDER_ERROR: PDF 页面渲染失败") from exc


def _pdf_page_concurrency(settings: Any) -> int:
    """读取并限制 PDF 并发数，避免异常配置把视觉服务压满。"""
    return max(1, min(int(getattr(settings, "ocr_page_concurrency", 3)), 8))


def _run_pdf_image_batch(
    page_images: dict[int, bytes],
    settings: Any,
    process: Callable[[bytes, Any], T],
) -> dict[int, T]:
    """并发处理一批已渲染页面，保留页码并优先报告页码较小的错误。"""
    with ThreadPoolExecutor(max_workers=len(page_images), thread_name_prefix="pdf-ocr") as pool:
        futures = {
            pool.submit(process, image, settings): page_number
            for page_number, image in page_images.items()
        }
        results: dict[int, T] = {}
        errors: list[tuple[int, OcrServiceError]] = []
        for future in as_completed(futures):
            page_number = futures[future]
            try:
                results[page_number] = future.result()
            except OcrServiceError as exc:
                errors.append((page_number, exc))
        if errors:
            raise min(errors, key=lambda item: item[0])[1]
        return results


def _analyze_image(
    content: bytes, settings: Any, *, prompt: str | None = None
) -> LayoutResult:
    """调用 OpenAI 兼容视觉模型接口识别图片文字和版面。"""
    base_url = str(settings.ocr_base_url).strip().rstrip("/")
    api_path = str(settings.ocr_chat_path).strip()
    if not api_path.startswith("/"):
        api_path = f"/{api_path}"
    model_name = str(settings.ocr_model_name).strip()
    secret_ref = str(getattr(settings, "ocr_secret_ref", "dashscope")).strip()
    api_key = resolve_secret(secret_ref)
    if not base_url or not api_path or not model_name:
        raise OcrServiceError("OCR_NOT_CONFIGURED: 请配置视觉 OCR 服务地址、接口路径和模型名称")
    if not api_key:
        raise OcrServiceError(
            f"OCR_API_KEY_MISSING: 请在 KNOWLEDGE_SERVICE_MODEL_API_KEYS 中配置 {secret_ref}"
        )

    if _is_paddleocr_model(model_name):
        return _analyze_image_paddleocr(
            content,
            base_url=base_url,
            model_name=model_name,
            api_key=api_key,
            settings=settings,
        )

    image_data = base64.b64encode(content).decode("ascii")
    request_body = {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt or _default_prompt(model_name),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_data}"},
                    },
                ],
            }
        ],
        "temperature": 0,
    }
    try:
        response = httpx.post(
            f"{base_url}{api_path}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=request_body,
            timeout=float(settings.ocr_timeout_seconds),
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException as exc:
        raise OcrServiceError("OCR_TIMEOUT: 视觉 OCR 请求超时") from exc
    except httpx.HTTPStatusError as exc:
        raise OcrServiceError(
            f"OCR_HTTP_ERROR: 视觉 OCR 返回 HTTP {exc.response.status_code}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise OcrServiceError("OCR_PROVIDER_ERROR: 无法读取视觉 OCR 响应") from exc

    try:
        message = payload["choices"][0]["message"]["content"]
        if isinstance(message, str):
            text = message
        elif isinstance(message, list):
            text = "\n".join(
                item["text"]
                for item in message
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
        else:
            text = ""
    except (KeyError, IndexError, TypeError):
        text = ""
    if not text.strip():
        raise OcrServiceError("OCR_INVALID_RESPONSE: 千帆 OCR 响应未包含识别文字")
    try:
        raw = text.strip().removeprefix("```json").removesuffix("```").strip()
        value = json.loads(raw)
        blocks = []
        for item in value.get("blocks", []):
            if not isinstance(item, dict) or item.get("kind") not in {
                "heading", "text", "table", "image"
            }:
                continue
            block_text = str(item.get("text") or "").strip()
            if not block_text:
                continue
            bbox_value = item.get("bbox")
            bbox = None
            if isinstance(bbox_value, list) and len(bbox_value) == 4:
                try:
                    coordinates = tuple(float(number) for number in bbox_value)
                    if all(0 <= number <= 1000 for number in coordinates):
                        x0, y0, x1, y1 = coordinates
                        if x0 < x1 and y0 < y1:
                            bbox = coordinates  # type: ignore[assignment]
                except (TypeError, ValueError):
                    bbox = None
            metadata = {}
            if item.get("heading_level") is not None:
                try:
                    metadata["heading_level"] = max(1, min(6, int(item["heading_level"])))
                except (TypeError, ValueError):
                    pass
            blocks.append(LayoutBlock(item["kind"], block_text, bbox, metadata))
        blocks = _order_layout_blocks(blocks)
        markdown = str(value.get("markdown") or "").strip()
        if not blocks and not markdown:
            raise ValueError
        if not markdown:
            markdown = "\n\n".join(block.text for block in blocks)
        return LayoutResult(tuple(blocks), markdown)
    except (json.JSONDecodeError, TypeError, AttributeError, ValueError):
        # 纯文字提取模型不返回版面 JSON，整页文本作为单个文本块入库。
        plain = (
            text.strip()
            .removeprefix("```markdown")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        return LayoutResult((LayoutBlock("text", plain),), plain)


def _recognize_image(content: bytes, settings: Any) -> str:
    """兼容旧调用方，只返回版面结果中的 Markdown 文本。"""
    return _analyze_image(content, settings).markdown


def _paddleocr_url(base_url: str) -> str:
    """拼接千帆 PaddleOCR 专用接口地址。"""
    return f"{base_url.rstrip('/')}/{_PADDLEOCR_ENDPOINT_PATH}"


def _paddleocr_payload(content: bytes, model_name: str, *, file_type: int) -> dict[str, Any]:
    """构建千帆 PaddleOCR 专用接口请求体。"""
    return {
        "model": model_name,
        "file": base64.b64encode(content).decode("ascii"),
        "fileType": file_type,
        "visualize": False,
        "useDocOrientationClassify": True,
        "useDocUnwarping": False,
        "useLayoutDetection": True,
    }


def _post_paddleocr(
    url: str, api_key: str, payload: dict[str, Any], settings: Any
) -> dict[str, Any]:
    """调用千帆 PaddleOCR 专用接口，并把网络错误转换为 OCR 领域错误。"""
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=float(settings.ocr_timeout_seconds),
        )
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException as exc:
        raise OcrServiceError("OCR_TIMEOUT: 视觉 OCR 请求超时") from exc
    except httpx.HTTPStatusError as exc:
        raise OcrServiceError(
            f"OCR_HTTP_ERROR: 视觉 OCR 返回 HTTP {exc.response.status_code}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise OcrServiceError("OCR_PROVIDER_ERROR: 无法读取视觉 OCR 响应") from exc


def _analyze_image_paddleocr(
    content: bytes,
    *,
    base_url: str,
    model_name: str,
    api_key: str,
    settings: Any,
) -> LayoutResult:
    """调用千帆 PaddleOCR 专用接口识别单张图片（如 Office 内嵌图）。"""
    body = _post_paddleocr(
        _paddleocr_url(base_url),
        api_key,
        _paddleocr_payload(content, model_name, file_type=1),
        settings,
    )
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    pages = result.get("layoutParsingResults") or []
    if not pages or not isinstance(pages[0], dict):
        raise OcrServiceError("OCR_INVALID_RESPONSE: 千帆 PaddleOCR 未返回版面结果")
    page_infos = (result.get("dataInfo") or {}).get("pages") or []
    page_info = page_infos[0] if page_infos else {}
    return _paddleocr_page_to_layout(pages[0], page_info)


def _analyze_pdf_paddleocr(
    content: bytes,
    *,
    settings: Any,
    page_numbers: list[int] | None = None,
) -> dict[int, LayoutResult]:
    """整份 PDF 按不超过 10 页分批提交千帆 PaddleOCR 专用接口。"""
    base_url = str(settings.ocr_base_url).strip().rstrip("/")
    model_name = str(settings.ocr_model_name).strip()
    secret_ref = str(getattr(settings, "ocr_secret_ref", "dashscope")).strip()
    api_key = resolve_secret(secret_ref)
    if not base_url or not model_name:
        raise OcrServiceError("OCR_NOT_CONFIGURED: 请配置视觉 OCR 服务地址和模型名称")
    if not api_key:
        raise OcrServiceError(
            f"OCR_API_KEY_MISSING: 请在 KNOWLEDGE_SERVICE_MODEL_API_KEYS 中配置 {secret_ref}"
        )
    try:
        import fitz
    except ImportError as exc:
        raise OcrServiceError("OCR_PDF_RENDER_ERROR: 缺少 PDF 渲染依赖") from exc

    document = fitz.open(stream=content, filetype="pdf")
    try:
        total_pages = len(document)
        selected_pages = page_numbers or list(range(1, total_pages + 1))
        if any(
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or number > total_pages
            for number in selected_pages
        ):
            raise OcrServiceError("OCR_INVALID_PAGE: PDF 页码超出范围")
        unique_pages = sorted(set(selected_pages))
        results: dict[int, LayoutResult] = {}
        for start in range(0, len(unique_pages), _PADDLEOCR_BATCH_MAX_PAGES):
            batch_numbers = unique_pages[start : start + _PADDLEOCR_BATCH_MAX_PAGES]
            batch_document = fitz.open()
            try:
                for page_number in batch_numbers:
                    batch_document.insert_pdf(
                        document, from_page=page_number - 1, to_page=page_number - 1
                    )
                batch_pdf = batch_document.tobytes()
            finally:
                batch_document.close()
            body = _post_paddleocr(
                _paddleocr_url(base_url),
                api_key,
                _paddleocr_payload(batch_pdf, model_name, file_type=0),
                settings,
            )
            result = body.get("result") if isinstance(body.get("result"), dict) else body
            pages = result.get("layoutParsingResults") or []
            page_infos = (result.get("dataInfo") or {}).get("pages") or []
            for index, page_number in enumerate(batch_numbers):
                if index >= len(pages) or not isinstance(pages[index], dict):
                    continue
                page_info = page_infos[index] if index < len(page_infos) else {}
                results[page_number] = _paddleocr_page_to_layout(pages[index], page_info)
        missing_pages = [number for number in unique_pages if number not in results]
        if missing_pages:
            raise OcrServiceError(
                f"OCR_INVALID_RESPONSE: 千帆 PaddleOCR 未返回第 {min(missing_pages)} 页"
            )
        return results
    finally:
        document.close()


def _paddleocr_page_to_layout(page: dict[str, Any], page_info: dict[str, Any]) -> LayoutResult:
    """把千帆 PaddleOCR 单页响应转换为 KB 版面结果。"""
    width = _paddleocr_page_dimension(page_info, "width")
    height = _paddleocr_page_dimension(page_info, "height")
    pruned = page.get("prunedResult") if isinstance(page.get("prunedResult"), dict) else page
    raw_blocks = pruned.get("parsing_res_list") or []
    items = sorted(
        (item for item in raw_blocks if isinstance(item, dict)),
        key=_paddleocr_block_order,
    )
    blocks: list[LayoutBlock] = []
    for item in items:
        label = str(item.get("block_label") or "text").strip().lower()
        if label in _PADDLEOCR_SKIP_LABELS:
            continue
        bbox = _paddleocr_bbox(item.get("block_bbox"), width, height)
        if label == "table":
            rows, cell_images = _html_table_to_markdown(
                str(item.get("block_content") or ""), width, height
            )
            if rows:
                blocks.append(LayoutBlock("table", "\n".join(rows), bbox))
            blocks.extend(cell_images)
            continue
        if label == "image":
            blocks.append(LayoutBlock("image", "图片", bbox))
            continue
        kind = "heading" if label in _PADDLEOCR_HEADING_LABELS else "text"
        text = _strip_html(str(item.get("block_content") or "")).strip()
        text = re.sub(r"^#{1,6}\s+", "", text).strip()
        if not text:
            continue
        blocks.append(LayoutBlock(kind, text, bbox))
    blocks = _order_layout_blocks(blocks)
    markdown = page.get("markdown")
    markdown_text = ""
    if isinstance(markdown, dict):
        markdown_text = _strip_html(str(markdown.get("text") or "")).strip()
    if not blocks and not markdown_text:
        raise OcrServiceError("OCR_INVALID_RESPONSE: 千帆 PaddleOCR 页结果为空")
    if not markdown_text:
        markdown_text = "\n\n".join(block.text for block in blocks)
    return LayoutResult(tuple(blocks), markdown_text)


def _paddleocr_page_dimension(page_info: dict[str, Any], key: str) -> float | None:
    """读取页面像素尺寸，用于 bbox 归一化。"""
    value = page_info.get(key) if isinstance(page_info, dict) else None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _paddleocr_block_order(item: dict[str, Any]) -> int:
    """读取版面块顺序，缺失时按响应原顺序。"""
    for key in ("block_order", "blockOrder", "order"):
        value = item.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 0


def _paddleocr_bbox(
    value: Any, width: float | None, height: float | None
) -> tuple[float, float, float, float] | None:
    """把千帆像素坐标 bbox 归一化为 0-1000。"""
    if not isinstance(value, list) or len(value) != 4 or not width or not height:
        return None
    try:
        x0, y0, x1, y1 = (float(part) for part in value)
    except (TypeError, ValueError):
        return None
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        return None
    return (
        x0 * 1000 / width,
        y0 * 1000 / height,
        x1 * 1000 / width,
        y1 * 1000 / height,
    )


def _paddleocr_img_src_bbox(
    src: str, width: float | None, height: float | None
) -> tuple[float, float, float, float] | None:
    """从千帆图片文件名 img_in_image_box_x_y_x1_y1.jpg 解析坐标。"""
    match = _PADDLEOCR_IMG_SRC_RE.search(src or "")
    if not match:
        return None
    return _paddleocr_bbox([int(group) for group in match.groups()], width, height)


def _html_table_to_markdown(
    html: str, width: float | None, height: float | None
) -> tuple[list[str], list[LayoutBlock]]:
    """把千帆 HTML 表格转成管道分隔行；单元格内图片提取为独立 image 块。"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        text = _strip_html(html).strip()
        return ([text] if text else []), []
    cell_images: list[LayoutBlock] = []
    rows_data: list[list[str]] = []
    occupancy: list[list[bool]] = []
    for row_index, tr in enumerate(table.find_all("tr")):
        # 网格行号等于 <tr> 序号；rowspan 会提前建出后续网格行，供后面的 <tr> 复用。
        while len(rows_data) <= row_index:
            rows_data.append([])
        while len(occupancy) <= row_index:
            occupancy.append([])
        column = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while column < len(occupancy[row_index]) and occupancy[row_index][column]:
                column += 1
            colspan = max(1, int(cell.get("colspan") or 1))
            rowspan = max(1, int(cell.get("rowspan") or 1))
            text = _table_cell_text(cell, cell_images, width, height)
            for span_row in range(rowspan):
                target_row = row_index + span_row
                while len(rows_data) <= target_row:
                    rows_data.append([])
                while len(occupancy) <= target_row:
                    occupancy.append([])
                for span_col in range(colspan):
                    target_col = column + span_col
                    while len(rows_data[target_row]) <= target_col:
                        rows_data[target_row].append("")
                    while len(occupancy[target_row]) <= target_col:
                        occupancy[target_row].append(False)
                    rows_data[target_row][target_col] = text if span_col == 0 else ""
                    occupancy[target_row][target_col] = True
            column += colspan
    column_count = max((len(row) for row in rows_data), default=0)
    lines: list[str] = []
    for row in rows_data:
        cells = [
            _escape_table_cell(value)
            for value in (row + [""] * (column_count - len(row)))[:column_count]
        ]
        if any(cells):
            lines.append(" | ".join(cells))
    return lines, cell_images


def _table_cell_text(
    cell: Any, cell_images: list[LayoutBlock], width: float | None, height: float | None
) -> str:
    """提取表格单元格文本，单元格内图片转为独立 image 块并占位。"""
    for image in cell.find_all("img"):
        bbox = _paddleocr_img_src_bbox(str(image.get("src") or ""), width, height)
        if bbox:
            cell_images.append(LayoutBlock("image", "图片", bbox))
        image.replace_with("[图片]")
    return cell.get_text(" ", strip=True)


def _escape_table_cell(value: str) -> str:
    """转义表格单元格中的管道符和换行，保持每行一个单元格串。"""
    return value.replace("|", r"\|").replace("\n", " ").strip()


def _strip_html(text: str) -> str:
    """去掉 OCR 响应中的 HTML 标签，仅保留可读文本。"""
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)


def _order_layout_blocks(blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    """按横跨整页的内容分区，再逐栏排序，减少双栏和三栏串行错乱。"""
    if len(blocks) < 2 or any(block.bbox is None for block in blocks):
        return blocks

    separators = sorted(
        (
            block
            for block in blocks
            if block.bbox is not None and block.bbox[2] - block.bbox[0] >= 650
        ),
        key=lambda block: block.bbox[1],
    )
    if not separators:
        return _order_columns(blocks)

    ordered: list[LayoutBlock] = []
    remaining = list(blocks)
    for separator in separators:
        assert separator.bbox is not None
        above = [
            block
            for block in remaining
            if block is not separator and block.bbox[1] < separator.bbox[1]
        ]
        ordered.extend(_order_columns(above))
        ordered.append(separator)
        remaining = [block for block in remaining if block is not separator and block not in above]
    ordered.extend(_order_columns(remaining))
    return ordered


def _order_columns(blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    """將同一垂直區域的版面塊按栏位 x 坐标聚类，再按栏内 y 坐标排序。"""
    if len(blocks) < 2:
        return blocks
    positioned = [block for block in blocks if block.bbox is not None]
    if len(positioned) != len(blocks):
        return blocks
    # 版面块左边界落在同一栏附近时归到一起，支持常见双栏和三栏排版。
    columns: list[list[LayoutBlock]] = []
    for block in sorted(positioned, key=lambda item: item.bbox[0]):
        assert block.bbox is not None
        if not columns:
            columns.append([block])
            continue
        last_column = columns[-1]
        last_left = sum(item.bbox[0] for item in last_column) / len(last_column)
        if block.bbox[0] - last_left > 100:
            columns.append([block])
        else:
            # 标题、缩进和列表符号会让同一栏的左边界有小幅变化。
            columns[-1].append(block)
    columns.sort(key=lambda column: min(item.bbox[0] for item in column))
    return [block for column in columns for block in sorted(column, key=lambda item: item.bbox[1])]
