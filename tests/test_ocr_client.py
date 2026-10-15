"""验证千帆视觉 OCR 请求、分页渲染和错误处理。"""

import base64
import sys
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from knowledge_service.infrastructure import ocr_client
from knowledge_service.infrastructure.ocr_client import (
    OcrServiceError,
    recognize_image,
    recognize_pdf_pages,
)


def make_settings(**overrides):
    """构造不依赖真实环境变量的 OCR 配置。"""
    values = {
        "ocr_base_url": "https://qianfan.example/v2",
        "ocr_chat_path": "/chat/completions",
        "ocr_model_name": "ocr-model-id",
        "ocr_timeout_seconds": 23,
        "ocr_render_dpi": 160,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def success_response(text="血压 120/80"):
    return httpx.Response(
        200,
        request=httpx.Request("POST", "https://qianfan.example/v2/chat/completions"),
        json={"choices": [{"message": {"content": text}}]},
    )


def test_image_request_uses_qianfan_chat_completions(monkeypatch) -> None:
    """图片以 Base64 视觉消息发送，密钥通过千帆密钥引用读取。"""
    calls = []
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: "test-secret")

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return success_response()

    monkeypatch.setattr(httpx, "post", fake_post)

    text = recognize_image(b"png-bytes", settings=make_settings(), filename="page.png")

    assert text == "血压 120/80"
    url, request = calls[0]
    assert url == "https://qianfan.example/v2/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer test-secret"
    assert request["json"]["model"] == "ocr-model-id"
    image_url = request["json"]["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url == "data:image/png;base64," + base64.b64encode(b"png-bytes").decode()
    assert request["timeout"] == 23


def test_pdf_renders_only_requested_pages_and_keeps_page_numbers(monkeypatch) -> None:
    """PDF 仅渲染指定页并逐页调用模型，结果保留原始页码。"""
    class Pixmap:
        def __init__(self, page_number):
            self.page_number = page_number

        def tobytes(self, image_format):
            assert image_format == "png"
            return f"page-{self.page_number}".encode()

    class Page:
        def __init__(self, number):
            self.number = number

        def get_pixmap(self, *, matrix, alpha):
            assert matrix.scale == pytest.approx(160 / 72)
            assert alpha is False
            return Pixmap(self.number)

    class Pdf:
        def __init__(self):
            self.pages = [Page(1), Page(2), Page(3)]
            self.closed = False

        def __len__(self):
            return len(self.pages)

        def __getitem__(self, index):
            return self.pages[index]

        def close(self):
            self.closed = True

    class Matrix:
        def __init__(self, scale, _y):
            self.scale = scale

    pdf = Pdf()
    fitz = ModuleType("fitz")
    fitz.Matrix = Matrix
    fitz.open = lambda **kwargs: pdf
    monkeypatch.setitem(sys.modules, "fitz", fitz)
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: "test-secret")
    sent_images = []

    def fake_post(url, **kwargs):
        image = kwargs["json"]["messages"][0]["content"][1]["image_url"]["url"]
        sent_images.append(base64.b64decode(image.split(",", 1)[1]))
        return success_response(f"文字 {len(sent_images)}")

    monkeypatch.setattr(httpx, "post", fake_post)

    pages = recognize_pdf_pages(
        b"pdf-bytes", settings=make_settings(), page_numbers=[3, 1]
    )

    assert pages == {3: "文字 1", 1: "文字 2"}
    assert sent_images == [b"page-3", b"page-1"]
    assert pdf.closed


def test_model_or_api_key_must_be_configured_before_http(monkeypatch) -> None:
    """模型名或 API Key 缺失时不发网络请求。"""
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: None)

    with pytest.raises(OcrServiceError, match="OCR_NOT_CONFIGURED"):
        recognize_image(b"image", settings=make_settings(ocr_model_name=""))
    with pytest.raises(OcrServiceError, match="OCR_API_KEY_MISSING"):
        recognize_image(b"image", settings=make_settings())
    assert calls == []


def test_http_error_does_not_expose_response_body_or_api_key(monkeypatch) -> None:
    """HTTP 失败只暴露状态码，不把密钥或服务端正文带给调用方。"""
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: "test-secret")
    request = httpx.Request("POST", "https://qianfan.example/v2/chat/completions")
    response = httpx.Response(503, request=request, text="private response")
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response)

    with pytest.raises(OcrServiceError, match="HTTP 503") as error:
        recognize_image(b"image", settings=make_settings())
    assert "private response" not in str(error.value)
    assert "test-secret" not in str(error.value)


def test_empty_model_response_is_rejected(monkeypatch) -> None:
    """成功状态但没有识别文字的响应不能当作 OCR 成功。"""
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: "test-secret")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            200,
            request=httpx.Request("POST", "https://qianfan.example/v2/chat/completions"),
            json={"choices": [{"message": {"content": " "}}]},
        ),
    )

    with pytest.raises(OcrServiceError, match="未包含识别文字"):
        recognize_image(b"image", settings=make_settings())