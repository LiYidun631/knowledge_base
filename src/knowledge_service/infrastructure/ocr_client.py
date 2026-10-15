"""调用百度千帆兼容视觉模型，识别扫描 PDF 中没有原生文字的页面。"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from knowledge_service.core.config import resolve_secret


class OcrServiceError(RuntimeError):
    """千帆 OCR 未配置、请求失败或返回了无效结果。"""


def recognize_image(content: bytes, *, settings: Any, filename: str = "page.png") -> str:
    """将单页图片以 Base64 图片消息发送给千帆视觉模型。"""
    if not content:
        raise OcrServiceError("OCR_INVALID_FILE: 图片内容为空")
    return _recognize_image(content, settings)


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
            results = {}
            for page_number in selected_pages:
                pixmap = document[page_number - 1].get_pixmap(matrix=matrix, alpha=False)
                results[page_number] = _recognize_image(pixmap.tobytes("png"), settings)
            return results
        finally:
            document.close()
    except OcrServiceError:
        raise
    except Exception as exc:
        raise OcrServiceError("OCR_PDF_RENDER_ERROR: PDF 页面渲染失败") from exc


def _recognize_image(content: bytes, settings: Any) -> str:
    """调用千帆 Chat Completions 接口识别图片文字。"""
    base_url = str(settings.ocr_base_url).strip().rstrip("/")
    api_path = str(settings.ocr_chat_path).strip()
    if not api_path.startswith("/"):
        api_path = f"/{api_path}"
    model_name = str(settings.ocr_model_name).strip()
    api_key = resolve_secret("qianfan")
    if not base_url or not api_path or not model_name:
        raise OcrServiceError("OCR_NOT_CONFIGURED: 请配置千帆 OCR 服务地址、接口路径和模型名称")
    if not api_key:
        raise OcrServiceError(
            "OCR_API_KEY_MISSING: 请在 KNOWLEDGE_SERVICE_MODEL_API_KEYS 中配置 qianfan"
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
                        "text": (
                            "请完整识别图片中的文字，按原有阅读顺序输出。"
                            "保留段落和表格结构；只输出识别结果，不要解释或补充。"
                        ),
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
        raise OcrServiceError("OCR_TIMEOUT: 千帆 OCR 请求超时") from exc
    except httpx.HTTPStatusError as exc:
        raise OcrServiceError(
            f"OCR_HTTP_ERROR: 千帆 OCR 返回 HTTP {exc.response.status_code}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise OcrServiceError("OCR_PROVIDER_ERROR: 无法读取千帆 OCR 响应") from exc

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
    return text.strip()
