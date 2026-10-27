"""调用百度千帆兼容视觉模型，识别扫描 PDF 中没有原生文字的页面。"""

from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from knowledge_service.core.config import resolve_secret
from knowledge_service.core.ocr_runtime import get_ocr_runtime, use_ocr_runtime

T = TypeVar("T")

_PADDLEOCR_ENDPOINT_PATH = "ocr/paddleocr"
_PADDLEOCR_BATCH_MAX_PAGES = 10
_PADDLEOCR_IMG_SRC_RE = re.compile(r"img_in_image_box_(\d+)_(\d+)_(\d+)_(\d+)")
_PADDLEOCR_SKIP_LABELS = {"number", "footer", "header", "vision_footnote", "page_footer"}
_PADDLEOCR_HEADING_LABELS = {"doc_title", "paragraph_title", "title"}

_OCR_MODES = {"auto", "vision", "paddleocr", "baidu_async"}

# 百度官方异步接口的限制与版面类型；超过限制需调用方先拆分文件。
_BAIDU_ASYNC_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_BAIDU_ASYNC_MAX_IMAGE_EDGE = 8192
_BAIDU_ASYNC_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
_BAIDU_ASYNC_MAX_PAGES = 500
_BAIDU_ASYNC_SKIP_TYPES = {
    "head_tail",
    "header",
    "footer",
    "page_number",
    "number",
    "footnote",
    "vision_footnote",
    "aside_text",
}
_BAIDU_ASYNC_HEADING_TYPES = {"title", "doc_title", "section_title", "paragraph_title"}
_BAIDU_ASYNC_IMAGE_TYPES = {"image", "figure", "chart", "picture"}
_BAIDU_ASYNC_OPTION_KEYS = {
    "analysis_chart",
    "merge_tables",
    "relevel_titles",
    "recognize_seal",
    "erase_seal",
    "erase_watermark",
    "unwarp",
    "need_pdffile_data",
    "need_pdffile_vis_data",
    "angle_adjust",
    "angle_mode",
}

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
class _BaiduAsyncConfig:
    """百度智能云官方异步 OCR 的连接、凭据与轮询参数。"""

    base_url: str
    task_path: str
    token_path: str
    ak: str = field(repr=False)
    sk: str = field(repr=False)
    poll_interval_seconds: float = 5
    timeout_seconds: float = 900
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _OcrConnection:
    """一次 OCR 调用使用的连接参数，来源可以是库内默认档案或环境配置。"""

    base_url: str
    chat_path: str
    model_name: str
    api_key: str = field(repr=False)
    paddleocr: bool
    paddleocr_path: str
    from_profile: bool
    baidu_async: _BaiduAsyncConfig | None = None


def _resolve_ocr_connection(settings: Any) -> _OcrConnection:
    """优先使用入库协程注入的默认档案，未注入时回落到 .env 配置。"""
    override = get_ocr_runtime()
    if override is not None:
        base_url = override.base_url.strip().rstrip("/")
        model_name = override.model_name.strip()
        chat_path = override.chat_path.strip() or "/chat/completions"
        if not chat_path.startswith("/"):
            chat_path = f"/{chat_path}"
        api_key = (override.api_key or "").strip()
        mode = override.mode if override.mode in _OCR_MODES else "auto"
        if not model_name:
            raise OcrServiceError("OCR_NOT_CONFIGURED: 默认 OCR 模型档案缺少服务地址或模型名称")
        baidu_async = None
        if mode == "baidu_async":
            baidu_async = _resolve_baidu_async_config(
                settings,
                dict(override.settings or {}),
                api_key=api_key,
                profile_base_url=base_url,
                from_profile=True,
            )
        elif not base_url:
            raise OcrServiceError("OCR_NOT_CONFIGURED: 默认 OCR 模型档案缺少服务地址或模型名称")
        elif not api_key:
            raise OcrServiceError(
                "OCR_API_KEY_MISSING: 请在模型管理中为默认 OCR 模型档案填写 API Key"
            )
        return _OcrConnection(
            base_url=base_url,
            chat_path=chat_path,
            model_name=model_name,
            api_key=api_key,
            paddleocr=mode == "paddleocr"
            or (mode == "auto" and _is_paddleocr_model(model_name)),
            paddleocr_path=chat_path
            if "paddleocr" in chat_path.lower()
            else f"/{_PADDLEOCR_ENDPOINT_PATH}",
            from_profile=True,
            baidu_async=baidu_async,
        )

    mode = str(getattr(settings, "ocr_mode", "auto")).strip() or "auto"
    if mode not in _OCR_MODES:
        mode = "auto"
    model_name = str(settings.ocr_model_name).strip()
    secret_ref = str(getattr(settings, "ocr_secret_ref", "dashscope")).strip()
    api_key = resolve_secret(secret_ref) or ""
    if mode == "baidu_async":
        baidu_async = _resolve_baidu_async_config(
            settings,
            {},
            api_key=api_key,
            profile_base_url="",
            from_profile=False,
        )
        return _OcrConnection(
            base_url=str(getattr(settings, "ocr_async_base_url", "")).strip().rstrip("/"),
            chat_path=str(getattr(settings, "ocr_async_task_path", "")).strip(),
            model_name=model_name or "PaddleOCR-VL",
            api_key=api_key,
            paddleocr=False,
            paddleocr_path=f"/{_PADDLEOCR_ENDPOINT_PATH}",
            from_profile=False,
            baidu_async=baidu_async,
        )

    base_url = str(settings.ocr_base_url).strip().rstrip("/")
    api_path = str(settings.ocr_chat_path).strip()
    if not api_path.startswith("/"):
        api_path = f"/{api_path}"
    if not base_url or not api_path or not model_name:
        raise OcrServiceError("OCR_NOT_CONFIGURED: 请配置视觉 OCR 服务地址、接口路径和模型名称")
    if not api_key:
        raise OcrServiceError(
            f"OCR_API_KEY_MISSING: 请在 KNOWLEDGE_SERVICE_MODEL_API_KEYS 中配置 {secret_ref}"
        )
    return _OcrConnection(
        base_url=base_url,
        chat_path=api_path,
        model_name=model_name,
        api_key=api_key,
        paddleocr=mode == "paddleocr"
        or (mode == "auto" and _is_paddleocr_model(model_name)),
        paddleocr_path=f"/{_PADDLEOCR_ENDPOINT_PATH}",
        from_profile=False,
    )


def _resolve_baidu_async_config(
    settings: Any,
    profile_settings: dict[str, Any],
    *,
    api_key: str,
    profile_base_url: str,
    from_profile: bool,
) -> _BaiduAsyncConfig:
    """解析 AK/SK 与异步任务参数；密钥只从密文或环境引用读取。"""
    ak, sk = _split_ak_sk(api_key)
    if from_profile:
        if not ak:
            ak = resolve_secret(
                str(profile_settings.get("async_ak_secret_ref") or "")
                or str(getattr(settings, "ocr_async_ak_ref", "BAIDU_OCR_AK"))
            ) or ""
        if not sk:
            sk = resolve_secret(
                str(profile_settings.get("async_sk_secret_ref") or "")
                or str(getattr(settings, "ocr_async_sk_ref", "BAIDU_OCR_SK"))
            ) or ""
        base_url = str(profile_settings.get("async_base_url") or "").strip()
        if not base_url and "aip.baidubce.com" in profile_base_url:
            base_url = profile_base_url
        task_path = str(profile_settings.get("async_task_path") or "").strip()
        token_path = str(profile_settings.get("async_token_path") or "").strip()
        raw_options = profile_settings.get("async_options")
        poll_interval = profile_settings.get("async_poll_interval_seconds")
        timeout = profile_settings.get("async_timeout_seconds")
    else:
        ak = ak or (resolve_secret(str(getattr(settings, "ocr_async_ak_ref", ""))) or "")
        sk = sk or (resolve_secret(str(getattr(settings, "ocr_async_sk_ref", ""))) or "")
        base_url = ""
        task_path = ""
        token_path = ""
        raw_options = None
        poll_interval = None
        timeout = None

    base_url = (base_url or str(getattr(settings, "ocr_async_base_url", ""))).strip().rstrip("/")
    task_path = task_path or str(
        getattr(settings, "ocr_async_task_path", "/rest/2.0/brain/online/v2/paddle-vl-parser/task")
    )
    token_path = token_path or str(
        getattr(settings, "ocr_async_token_path", "/oauth/2.0/token")
    )
    if not task_path.startswith("/"):
        task_path = f"/{task_path}"
    if not token_path.startswith("/"):
        token_path = f"/{token_path}"
    if not ak or not sk:
        raise OcrServiceError(
            "OCR_API_KEY_MISSING: 百度异步 OCR 需要 AK:SK，请在 OCR 模型档案的 API Key "
            "中填写 AK:SK，或配置 BAIDU_OCR_AK / BAIDU_OCR_SK"
        )

    options = {
        "analysis_chart": bool(getattr(settings, "ocr_async_analysis_chart", False)),
        "merge_tables": bool(getattr(settings, "ocr_async_merge_tables", True)),
        "relevel_titles": bool(getattr(settings, "ocr_async_relevel_titles", True)),
        "recognize_seal": bool(getattr(settings, "ocr_async_recognize_seal", False)),
        "erase_seal": False,
        "erase_watermark": False,
        "unwarp": False,
        "need_pdffile_data": False,
        "need_pdffile_vis_data": False,
        "angle_adjust": True,
        "angle_mode": "direction",
    }
    if isinstance(raw_options, dict):
        options.update(
            {
                key: value
                for key, value in raw_options.items()
                if key in _BAIDU_ASYNC_OPTION_KEYS
            }
        )
    return _BaiduAsyncConfig(
        base_url=base_url,
        task_path=task_path,
        token_path=token_path,
        ak=ak,
        sk=sk,
        poll_interval_seconds=_positive_float(
            poll_interval,
            float(getattr(settings, "ocr_async_poll_interval_seconds", 5)),
        ),
        timeout_seconds=_positive_float(
            timeout,
            float(getattr(settings, "ocr_async_timeout_seconds", 900)),
        ),
        options=options,
    )


def _split_ak_sk(value: str) -> tuple[str, str]:
    """支持 AK:SK、JSON 或环境变量拼接形式，解析失败时返回空串。"""
    raw = (value or "").strip()
    if not raw:
        return "", ""
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return "", ""
        if not isinstance(data, dict):
            return "", ""
        ak = str(
            data.get("ak") or data.get("AK") or data.get("client_id") or ""
        ).strip()
        sk = str(
            data.get("sk")
            or data.get("SK")
            or data.get("client_secret")
            or data.get("secret")
            or ""
        ).strip()
        return ak, sk
    for separator in (":", "|", ",", "\n", " "):
        if separator in raw:
            ak, _, sk = raw.partition(separator)
            return ak.strip(), sk.strip()
    return "", ""


def _positive_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


class _RateLimiter:
    """进程内节流，避免多份文档并发时超过百度接口 QPS 限制。"""

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval_seconds = max(0.0, min_interval_seconds)
        self._lock = threading.Lock()
        self._last_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._last_at + self._min_interval_seconds - now
            if delay > 0:
                time.sleep(delay)
            self._last_at = time.monotonic()


_BAIDU_SUBMIT_LIMITER = _RateLimiter(0.55)
_BAIDU_QUERY_LIMITER = _RateLimiter(0.22)
_BAIDU_TOKEN_LOCK = threading.Lock()
_BAIDU_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}


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
    return _analyze_image(content, settings, prompt=prompt, filename=filename)


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
    connection = _resolve_ocr_connection(settings)
    if connection.baidu_async is not None:
        return _analyze_pdf_baidu_async(
            content, settings=settings, page_numbers=page_numbers, connection=connection
        )
    if connection.paddleocr:
        return _analyze_pdf_paddleocr(
            content, settings=settings, page_numbers=page_numbers, connection=connection
        )
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
    connection = _resolve_ocr_connection(settings)
    if connection.baidu_async is not None:
        layouts = _analyze_pdf_baidu_async(
            content, settings=settings, page_numbers=page_numbers, connection=connection
        )
        return {page_number: layout.markdown for page_number, layout in layouts.items()}
    if connection.paddleocr:
        layouts = _analyze_pdf_paddleocr(
            content, settings=settings, page_numbers=page_numbers, connection=connection
        )
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
    runtime = get_ocr_runtime()

    def worker(image: bytes) -> T:
        # ThreadPoolExecutor 不复制上下文，需在子线程恢复本次入库的 OCR 档案。
        with use_ocr_runtime(runtime):
            return process(image, settings)

    with ThreadPoolExecutor(max_workers=len(page_images), thread_name_prefix="pdf-ocr") as pool:
        futures = {
            pool.submit(worker, image): page_number
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
    content: bytes,
    settings: Any,
    *,
    prompt: str | None = None,
    filename: str = "page.png",
) -> LayoutResult:
    """调用 OpenAI 兼容视觉模型接口识别图片文字和版面。"""
    connection = _resolve_ocr_connection(settings)
    if connection.baidu_async is not None:
        return _analyze_image_baidu_async(
            content,
            connection=connection,
            settings=settings,
            filename=filename,
        )
    if connection.paddleocr:
        return _analyze_image_paddleocr(
            content,
            connection=connection,
            settings=settings,
        )

    image_data = base64.b64encode(content).decode("ascii")
    request_body = {
        "model": connection.model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt or _default_prompt(connection.model_name),
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
            f"{connection.base_url}{connection.chat_path}",
            headers={
                "Authorization": f"Bearer {connection.api_key}",
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


def _paddleocr_url(base_url: str, path: str = f"/{_PADDLEOCR_ENDPOINT_PATH}") -> str:
    """拼接 PaddleOCR 专用接口地址，档案可覆盖默认路径。"""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


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
    connection: _OcrConnection,
    settings: Any,
) -> LayoutResult:
    """调用千帆 PaddleOCR 专用接口识别单张图片（如 Office 内嵌图）。"""
    body = _post_paddleocr(
        _paddleocr_url(connection.base_url, connection.paddleocr_path),
        connection.api_key,
        _paddleocr_payload(content, connection.model_name, file_type=1),
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
    connection: _OcrConnection | None = None,
) -> dict[int, LayoutResult]:
    """整份 PDF 按不超过 10 页分批提交 PaddleOCR 专用接口。"""
    connection = connection or _resolve_ocr_connection(settings)
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
                _paddleocr_url(connection.base_url, connection.paddleocr_path),
                connection.api_key,
                _paddleocr_payload(batch_pdf, connection.model_name, file_type=0),
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


def _analyze_image_baidu_async(
    content: bytes,
    *,
    connection: _OcrConnection,
    settings: Any,
    filename: str,
) -> LayoutResult:
    """提交单张图片到百度官方异步接口，并转换第一页结果。"""
    config = connection.baidu_async
    assert config is not None
    _validate_baidu_async_image(content)
    payload = _run_baidu_async_task(
        content,
        file_name=_baidu_file_name(filename, "page.png"),
        config=config,
        settings=settings,
    )
    pages = payload.get("pages") or []
    if not pages or not isinstance(pages[0], dict):
        raise OcrServiceError("OCR_INVALID_RESPONSE: 百度异步 OCR 未返回页面结果")
    return _baidu_async_page_to_layout(pages[0])


def _analyze_pdf_baidu_async(
    content: bytes,
    *,
    settings: Any,
    page_numbers: list[int] | None = None,
    connection: _OcrConnection | None = None,
) -> dict[int, LayoutResult]:
    """把 PDF 提交到百度官方异步接口，按页映射解析结果。"""
    connection = connection or _resolve_ocr_connection(settings)
    config = connection.baidu_async
    if config is None:
        raise OcrServiceError("OCR_NOT_CONFIGURED: 当前 OCR 档案未启用百度异步模式")
    if len(content) > _BAIDU_ASYNC_MAX_DOCUMENT_BYTES:
        raise OcrServiceError("OCR_INVALID_FILE: 百度异步 OCR 单次提交不能超过 50 MB")
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
        for start in range(0, len(unique_pages), _BAIDU_ASYNC_MAX_PAGES):
            batch_numbers = unique_pages[start : start + _BAIDU_ASYNC_MAX_PAGES]
            batch_pdf = _baidu_batch_pdf(document, batch_numbers, content, total_pages)
            if len(batch_pdf) > _BAIDU_ASYNC_MAX_DOCUMENT_BYTES:
                raise OcrServiceError(
                    "OCR_INVALID_FILE: 百度异步 OCR 单次提交不能超过 50 MB，请缩小页范围"
                )
            payload = _run_baidu_async_task(
                batch_pdf,
                file_name="document.pdf",
                config=config,
                settings=settings,
            )
            for fallback_index, page in enumerate(payload.get("pages") or []):
                if not isinstance(page, dict):
                    continue
                page_index = _baidu_page_index(page, fallback_index, len(batch_numbers))
                results[batch_numbers[page_index]] = _baidu_async_page_to_layout(page)
        missing_pages = [number for number in unique_pages if number not in results]
        if missing_pages:
            raise OcrServiceError(
                f"OCR_INVALID_RESPONSE: 百度异步 OCR 未返回第 {min(missing_pages)} 页"
            )
        return results
    finally:
        document.close()


def _baidu_batch_pdf(
    document: Any,
    page_numbers: list[int],
    content: bytes,
    total_pages: int,
) -> bytes:
    """选页提交时重建 PDF；整份提交直接复用原始字节。"""
    if page_numbers == list(range(1, total_pages + 1)):
        return content
    import fitz

    batch_document = fitz.open()
    try:
        for page_number in page_numbers:
            batch_document.insert_pdf(
                document, from_page=page_number - 1, to_page=page_number - 1
            )
        return batch_document.tobytes()
    finally:
        batch_document.close()


def _run_baidu_async_task(
    content: bytes,
    *,
    file_name: str,
    config: _BaiduAsyncConfig,
    settings: Any,
) -> dict[str, Any]:
    """完成一次提交、轮询和结果下载。"""
    token = _get_baidu_access_token(config, settings)
    task_id = _submit_baidu_task(
        content, file_name=file_name, config=config, token=token, settings=settings
    )
    result = _wait_baidu_task(config, token, task_id, settings)
    return _download_baidu_parse_result(result, settings)


def _get_baidu_access_token(config: _BaiduAsyncConfig, settings: Any) -> str:
    """用 AK/SK 换取 access_token，并在有效期内复用。"""
    cache_key = (config.ak, config.sk)
    now = time.monotonic()
    with _BAIDU_TOKEN_LOCK:
        cached = _BAIDU_TOKEN_CACHE.get(cache_key)
    if cached is not None and cached[1] > now:
        return cached[0]

    _BAIDU_SUBMIT_LIMITER.wait()
    try:
        response = httpx.post(
            f"{config.base_url}{config.token_path}",
            params={
                "grant_type": "client_credentials",
                "client_id": config.ak,
                "client_secret": config.sk,
            },
            timeout=min(30.0, float(getattr(settings, "ocr_timeout_seconds", 30))),
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException as exc:
        raise OcrServiceError("OCR_TIMEOUT: 百度异步 OCR 鉴权请求超时") from exc
    except httpx.HTTPStatusError as exc:
        raise OcrServiceError(
            f"OCR_HTTP_ERROR: 百度异步 OCR 鉴权返回 HTTP {exc.response.status_code}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise OcrServiceError("OCR_PROVIDER_ERROR: 无法读取百度异步 OCR 鉴权响应") from exc
    if not isinstance(payload, dict):
        raise OcrServiceError("OCR_INVALID_RESPONSE: 百度异步 OCR 鉴权响应格式无效")
    if payload.get("error"):
        description = str(
            payload.get("error_description") or payload.get("error") or ""
        ).strip()
        raise OcrServiceError(
            "OCR_AUTH_ERROR: 百度异步 OCR 鉴权失败"
            + (f"（{description[:200]}）" if description else "")
        )
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise OcrServiceError("OCR_INVALID_RESPONSE: 百度异步 OCR 鉴权未返回 access_token")
    expires_in = _positive_float(payload.get("expires_in"), 3600)
    # 提前 5 分钟失效，避免长任务轮询途中 token 过期。
    with _BAIDU_TOKEN_LOCK:
        _BAIDU_TOKEN_CACHE[cache_key] = (token, now + max(60.0, expires_in - 300))
    return token


def _submit_baidu_task(
    content: bytes,
    *,
    file_name: str,
    config: _BaiduAsyncConfig,
    token: str,
    settings: Any,
) -> str:
    """提交异步任务并返回 task_id。"""
    data = {
        "file_data": base64.b64encode(content).decode("ascii"),
        "file_name": file_name,
        **_baidu_form_fields(config.options),
    }
    body = _check_baidu_response(
        _post_baidu_request(
            f"{config.base_url}{config.task_path}",
            token=token,
            data=data,
            settings=settings,
            limiter=_BAIDU_SUBMIT_LIMITER,
        )
    )
    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    task_id = str(result.get("task_id") or "").strip()
    if not task_id:
        raise OcrServiceError("OCR_INVALID_RESPONSE: 百度异步 OCR 未返回 task_id")
    return task_id


def _wait_baidu_task(
    config: _BaiduAsyncConfig, token: str, task_id: str, settings: Any
) -> dict[str, Any]:
    """按官方建议间隔轮询，直到成功、失败或超时。"""
    query_url = f"{config.base_url}{config.task_path.rstrip('/')}/query"
    deadline = time.monotonic() + config.timeout_seconds
    while True:
        body = _check_baidu_response(
            _post_baidu_request(
                query_url,
                token=token,
                data={"task_id": task_id},
                settings=settings,
                limiter=_BAIDU_QUERY_LIMITER,
            )
        )
        result = body.get("result") if isinstance(body.get("result"), dict) else {}
        status = str(result.get("status") or "").strip().lower()
        if status == "success":
            return result
        if status == "failed":
            raise OcrServiceError(
                "OCR_PROVIDER_ERROR: 百度异步 OCR 任务失败："
                + _short_error(result.get("task_error"))
            )
        if status not in {"pending", "running"}:
            raise OcrServiceError("OCR_INVALID_RESPONSE: 百度异步 OCR 返回未知任务状态")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OcrServiceError("OCR_TIMEOUT: 百度异步 OCR 等待超时")
        time.sleep(min(config.poll_interval_seconds, remaining))


def _download_baidu_parse_result(
    result: dict[str, Any], settings: Any
) -> dict[str, Any]:
    """下载解析结果 JSON；地址来自百度响应，仅接受 HTTP(S)。"""
    url = str(result.get("parse_result_url") or "").strip()
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise OcrServiceError("OCR_INVALID_RESPONSE: 百度异步 OCR 未返回有效的结果地址")
    try:
        response = httpx.get(
            url,
            timeout=float(settings.ocr_timeout_seconds),
            follow_redirects=True,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException as exc:
        raise OcrServiceError("OCR_TIMEOUT: 下载百度异步 OCR 结果超时") from exc
    except httpx.HTTPStatusError as exc:
        raise OcrServiceError(
            f"OCR_HTTP_ERROR: 下载百度异步 OCR 结果返回 HTTP {exc.response.status_code}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise OcrServiceError("OCR_PROVIDER_ERROR: 无法下载百度异步 OCR 结果") from exc
    if not isinstance(payload, dict) or not payload.get("pages"):
        raise OcrServiceError("OCR_INVALID_RESPONSE: 百度异步 OCR 解析结果为空")
    return payload


def _post_baidu_request(
    url: str,
    *,
    token: str,
    data: dict[str, str],
    settings: Any,
    limiter: _RateLimiter,
) -> dict[str, Any]:
    """发送表单请求并统一转换网络错误。"""
    limiter.wait()
    try:
        response = httpx.post(
            url,
            params={"access_token": token},
            data=data,
            timeout=float(settings.ocr_timeout_seconds),
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException as exc:
        raise OcrServiceError("OCR_TIMEOUT: 百度异步 OCR 请求超时") from exc
    except httpx.HTTPStatusError as exc:
        raise OcrServiceError(
            f"OCR_HTTP_ERROR: 百度异步 OCR 返回 HTTP {exc.response.status_code}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise OcrServiceError("OCR_PROVIDER_ERROR: 无法读取百度异步 OCR 响应") from exc
    if not isinstance(payload, dict):
        raise OcrServiceError("OCR_INVALID_RESPONSE: 百度异步 OCR 响应格式无效")
    return payload


def _check_baidu_response(body: dict[str, Any]) -> dict[str, Any]:
    """HTTP 200 也可能带业务错误码，需单独检查。"""
    code = body.get("error_code")
    if code in (None, 0, "0"):
        return body
    message = _short_error(body.get("error_msg"))
    raise OcrServiceError(
        f"OCR_PROVIDER_ERROR: 百度异步 OCR 返回 error_code={code}"
        + (f"（{message}）" if message and message != "未知错误" else "")
    )


def _baidu_form_fields(options: dict[str, Any]) -> dict[str, str]:
    """把布尔与整数选项转换成百度接口要求的表单字符串。"""
    fields: dict[str, str] = {}
    for key, value in options.items():
        if isinstance(value, bool):
            fields[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            fields[key] = json.dumps(list(value))
        else:
            fields[key] = str(value)
    return fields


def _baidu_file_name(filename: str, default: str) -> str:
    """保留真实扩展名；缺失时补默认文件的扩展名。"""
    name = Path(str(filename or "")).name.strip()
    if not name:
        return default
    if not Path(name).suffix:
        name = f"{name}{Path(default).suffix}"
    return name


def _validate_baidu_async_image(content: bytes) -> None:
    """按官方限制拦截过大的图片，避免提交后在服务端失败。"""
    if len(content) > _BAIDU_ASYNC_MAX_IMAGE_BYTES:
        raise OcrServiceError("OCR_INVALID_FILE: 百度异步 OCR 单张图片不能超过 10 MB")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as image:
            if max(image.size) > _BAIDU_ASYNC_MAX_IMAGE_EDGE:
                raise OcrServiceError(
                    "OCR_INVALID_FILE: 百度异步 OCR 图片最长边不能超过 8192 像素"
                )
    except OcrServiceError:
        raise
    except Exception:
        # 无法读取尺寸时交给上游接口判断，避免误拒有效图片。
        pass


def _baidu_page_index(page: dict[str, Any], fallback: int, page_count: int) -> int:
    """优先使用响应中的 page_num，越界或缺失时按顺序回退。"""
    try:
        index = int(page.get("page_num"))
    except (TypeError, ValueError):
        return fallback
    return index if 0 <= index < page_count else fallback


def _baidu_async_bbox(
    value: Any, width: float | None, height: float | None
) -> tuple[float, float, float, float] | None:
    """百度异步结果使用 [x, y, 宽, 高]，转换为归一化 0-1000 的边框。"""
    if not isinstance(value, list) or len(value) != 4 or not width or not height:
        return None
    try:
        x, y, box_width, box_height = (float(part) for part in value)
    except (TypeError, ValueError):
        return None
    if box_width <= 0 or box_height <= 0:
        return None
    x0 = max(0.0, x)
    y0 = max(0.0, y)
    x1 = min(float(width), x + box_width)
    y1 = min(float(height), y + box_height)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0 * 1000 / width, y0 * 1000 / height, x1 * 1000 / width, y1 * 1000 / height)


def _baidu_async_page_to_layout(page: dict[str, Any]) -> LayoutResult:
    """把百度异步解析结果的一页转换成 KB 版面块。"""
    meta = page.get("meta") if isinstance(page.get("meta"), dict) else {}
    width = _positive_or_none(meta.get("page_width"))
    height = _positive_or_none(meta.get("page_height"))
    tables = {
        str(item.get("layout_id")): item
        for item in page.get("tables") or []
        if isinstance(item, dict) and item.get("layout_id")
    }
    images = {
        str(item.get("layout_id")): item
        for item in page.get("images") or []
        if isinstance(item, dict) and item.get("layout_id")
    }
    blocks: list[LayoutBlock] = []
    layout_ids: set[str] = set()
    for item in page.get("layouts") or []:
        if not isinstance(item, dict):
            continue
        layout_id = str(item.get("layout_id") or "")
        if layout_id:
            layout_ids.add(layout_id)
        label = str(item.get("type") or "text").strip().lower()
        if label in _BAIDU_ASYNC_SKIP_TYPES:
            continue
        bbox = _baidu_async_bbox(item.get("position"), width, height)
        if label == "table":
            table = tables.get(layout_id, {})
            markdown = str(table.get("markdown") or item.get("text") or "").strip()
            if markdown:
                blocks.append(LayoutBlock("table", markdown, bbox))
            continue
        if label in _BAIDU_ASYNC_IMAGE_TYPES:
            image = images.get(layout_id, {})
            description = str(
                image.get("description")
                or image.get("image_description")
                or image.get("text")
                or item.get("text")
                or ""
            ).strip()
            blocks.append(LayoutBlock("image", description or "图片", bbox))
            continue
        text = str(item.get("text") or "").strip()
        metadata: dict[str, Any] = {}
        if label in _BAIDU_ASYNC_HEADING_TYPES:
            match = re.match(r"^(#{1,6})\s+", text)
            if match:
                metadata["heading_level"] = len(match.group(1))
        text = re.sub(r"^#{1,6}\s+", "", text).strip()
        if not text:
            continue
        kind = "heading" if label in _BAIDU_ASYNC_HEADING_TYPES else "text"
        blocks.append(LayoutBlock(kind, text, bbox, metadata))

    # 少量结果只把表格/图片放在独立数组里，补成版面块避免内容丢失。
    for layout_id, table in tables.items():
        if layout_id in layout_ids:
            continue
        markdown = str(table.get("markdown") or "").strip()
        if markdown:
            blocks.append(
                LayoutBlock(
                    "table", markdown, _baidu_async_bbox(table.get("position"), width, height)
                )
            )
    for layout_id, image in images.items():
        if layout_id in layout_ids:
            continue
        description = str(
            image.get("description") or image.get("image_description") or image.get("text") or ""
        ).strip()
        blocks.append(
            LayoutBlock(
                "image",
                description or "图片",
                _baidu_async_bbox(image.get("position"), width, height),
            )
        )

    markdown_text = str(page.get("text") or "").strip()
    if not blocks and not markdown_text:
        raise OcrServiceError("OCR_INVALID_RESPONSE: 百度异步 OCR 页结果为空")
    if not markdown_text:
        markdown_text = "\n\n".join(block.text for block in blocks)
    return LayoutResult(tuple(blocks), markdown_text)


def _positive_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _short_error(value: Any) -> str:
    """截断服务端错误描述，避免把超长正文带进业务错误。"""
    if isinstance(value, dict):
        text = str(
            value.get("error_msg")
            or value.get("message")
            or value.get("error")
            or value.get("error_code")
            or ""
        )
    else:
        text = str(value or "")
    text = text.strip()
    return text[:200] or "未知错误"


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
