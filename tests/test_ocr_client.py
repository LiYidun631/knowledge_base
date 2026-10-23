"""验证千帆视觉 OCR 请求、分页渲染和错误处理。"""

import base64
import json
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from knowledge_service.infrastructure import ocr_client
from knowledge_service.infrastructure.ocr_client import (
    LayoutBlock,
    OcrServiceError,
    _LAYOUT_JSON_PROMPT,
    _PLAIN_OCR_PROMPT,
    _order_layout_blocks,
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
        "ocr_page_concurrency": 2,
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


def test_image_layout_accepts_explicit_heading_level(monkeypatch) -> None:
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: "test-secret")
    response_text = json.dumps(
        {
            "blocks": [
                {"kind": "heading", "heading_level": 2, "text": "3.1 Operation"},
                {"kind": "text", "text": "Press the button."},
            ],
            "markdown": "3.1 Operation\n\nPress the button.",
        }
    )
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: success_response(response_text))

    layout = ocr_client.analyze_image(b"image-bytes", settings=make_settings())

    assert layout.blocks[0].kind == "heading"
    assert layout.blocks[0].metadata["heading_level"] == 2


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
    sent_images = []
    lock = threading.Lock()
    active_requests = 0
    max_active_requests = 0

    def fake_recognize(image, _settings):
        nonlocal active_requests, max_active_requests
        with lock:
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)
            sent_images.append(image)
        time.sleep(0.05)
        with lock:
            active_requests -= 1
        return f"识别 {image.decode()}"

    monkeypatch.setattr(ocr_client, "_recognize_image", fake_recognize)

    pages = recognize_pdf_pages(
        b"pdf-bytes", settings=make_settings(), page_numbers=[3, 1]
    )

    assert pages == {3: "识别 page-3", 1: "识别 page-1"}
    assert set(sent_images) == {b"page-3", b"page-1"}
    assert max_active_requests == 2
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


def test_layout_blocks_are_ordered_by_columns() -> None:
    """双栏内容应先读左栏，再读右栏，而不是按页面横向交错。"""
    blocks = [
        LayoutBlock("text", "右栏下", (550, 500, 950, 650)),
        LayoutBlock("text", "左栏下", (50, 500, 450, 650)),
        LayoutBlock("text", "右栏上", (550, 100, 950, 250)),
        LayoutBlock("text", "左栏上", (50, 100, 450, 250)),
    ]

    ordered = _order_layout_blocks(blocks)

    assert [item.text for item in ordered] == ["左栏上", "左栏下", "右栏上", "右栏下"]


def _sent_prompt(monkeypatch, model_name: str) -> str:
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: "test-secret")
    calls = []
    monkeypatch.setattr(
        httpx, "post", lambda url, **kwargs: calls.append(kwargs) or success_response()
    )
    ocr_client.analyze_image(
        b"image-bytes", settings=make_settings(ocr_model_name=model_name)
    )
    return calls[0]["json"]["messages"][0]["content"][0]["text"]


def test_ocr_named_model_uses_plain_extraction_prompt(monkeypatch) -> None:
    """专用 OCR 模型（如 qwen-vl-ocr）只要求文字提取，不要求版面 JSON。"""
    assert _sent_prompt(monkeypatch, "qwen-vl-ocr") == _PLAIN_OCR_PROMPT


def test_vision_model_uses_layout_json_prompt(monkeypatch) -> None:
    """通用视觉模型继续使用版面 JSON 提示词。"""
    assert _sent_prompt(monkeypatch, "qwen-vl-plus") == _LAYOUT_JSON_PROMPT


def test_plain_text_response_falls_back_to_single_text_block(monkeypatch) -> None:
    """纯文字提取模型的响应不带 JSON，兜底为整页文本块并去掉代码围栏。"""
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: "test-secret")
    monkeypatch.setattr(
        httpx, "post", lambda *args, **kwargs: success_response("```markdown\n血压 120/80\n```")
    )

    layout = ocr_client.analyze_image(b"image", settings=make_settings(ocr_model_name="qwen-vl-ocr"))

    assert len(layout.blocks) == 1
    assert layout.blocks[0].kind == "text"
    assert layout.blocks[0].text == "血压 120/80"
    assert layout.markdown == "血压 120/80"


def paddleocr_page(text="第 1 页"):
    """构造千帆 PaddleOCR 单页响应结构。"""
    return {
        "prunedResult": {
            "parsing_res_list": [
                {"block_label": "text", "block_content": text, "block_order": 1}
            ]
        },
        "markdown": {"text": text},
    }


def paddleocr_response(pages):
    """构造千帆 PaddleOCR 多页响应体。"""
    return {
        "result": {
            "layoutParsingResults": pages,
            "dataInfo": {"pages": [{"width": 1000, "height": 1000} for _ in pages]},
        }
    }


def make_pdf(page_count):
    """用 fitz 生成指定页数的空白 PDF 字节。"""
    import fitz

    document = fitz.open()
    for _ in range(page_count):
        document.new_page()
    content = document.tobytes()
    document.close()
    return content


def test_paddleocr_image_uses_dedicated_endpoint(monkeypatch) -> None:
    """PaddleOCR 图片识别走专用 ocr/paddleocr 接口，fileType=1。"""
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: "test-secret")
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200, request=httpx.Request("POST", url), json=paddleocr_response([paddleocr_page("图片说明")])
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    layout = ocr_client.analyze_image(
        b"png-bytes", settings=make_settings(ocr_model_name="paddleocr-vl-0.9b")
    )

    url, request = calls[0]
    assert url == "https://qianfan.example/v2/ocr/paddleocr"
    assert request["headers"]["Authorization"] == "Bearer test-secret"
    assert request["json"]["model"] == "paddleocr-vl-0.9b"
    assert request["json"]["fileType"] == 1
    assert request["json"]["file"] == base64.b64encode(b"png-bytes").decode()
    assert layout.blocks[0].text == "图片说明"


def test_paddleocr_pdf_batches_over_ten_pages(monkeypatch) -> None:
    """PaddleOCR 每批不超过 10 页，结果映射回原始页码。"""
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: "test-secret")
    batch_sizes = []
    file_types = []

    def fake_post(url, **kwargs):
        payload = kwargs["json"]
        file_types.append(payload["fileType"])
        import fitz

        document = fitz.open(stream=base64.b64decode(payload["file"]), filetype="pdf")
        try:
            batch_sizes.append(len(document))
        finally:
            document.close()
        pages = [paddleocr_page(f"批次第 {index + 1} 页") for index in range(batch_sizes[-1])]
        return httpx.Response(
            200, request=httpx.Request("POST", url), json=paddleocr_response(pages)
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    results = ocr_client.analyze_pdf_pages(
        make_pdf(15),
        settings=make_settings(ocr_model_name="paddleocr-vl-0.9b"),
    )

    assert batch_sizes == [10, 5]
    assert file_types == [0, 0]
    assert set(results) == set(range(1, 16))
    assert results[1].blocks[0].text == "批次第 1 页"
    assert results[10].blocks[0].text == "批次第 10 页"
    assert results[11].blocks[0].text == "批次第 1 页"
    assert results[15].blocks[0].text == "批次第 5 页"


def test_paddleocr_recognize_pdf_pages_returns_markdown(monkeypatch) -> None:
    """PaddleOCR 模式下逐页识别返回 Markdown 文本。"""
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: "test-secret")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json=paddleocr_response([paddleocr_page("第一页"), paddleocr_page("第二页")]),
        ),
    )

    pages = ocr_client.recognize_pdf_pages(
        make_pdf(2), settings=make_settings(ocr_model_name="paddleocr-vl-0.9b")
    )

    assert pages == {1: "第一页", 2: "第二页"}


def test_paddleocr_pdf_missing_pages_raises(monkeypatch) -> None:
    """服务端少返回页时按最小缺失页码报错，而不是静默丢弃。"""
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: "test-secret")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json=paddleocr_response([paddleocr_page("第 1 页")]),
        ),
    )

    with pytest.raises(OcrServiceError, match="未返回第 2 页"):
        ocr_client.analyze_pdf_pages(
            make_pdf(3),
            settings=make_settings(ocr_model_name="paddleocr-vl-0.9b"),
        )


def test_paddleocr_page_converts_labels_and_bboxes() -> None:
    """标题/正文/表格/图片转换，页码等辅助标签丢弃，bbox 归一化到 0-1000。"""
    page = {
        "prunedResult": {
            "parsing_res_list": [
                {
                    "block_label": "doc_title",
                    "block_content": "## 3.5 常见故障",
                    "block_bbox": [100, 100, 400, 150],
                    "block_order": 1,
                },
                {
                    "block_label": "text",
                    "block_content": "<p>按下按钮开机。</p>",
                    "block_bbox": [100, 200, 400, 250],
                    "block_order": 2,
                },
                {
                    "block_label": "table",
                    "block_content": (
                        "<table><tr><th>指标</th><th>值</th></tr>"
                        "<tr><td>收缩压</td><td>120</td></tr></table>"
                    ),
                    "block_bbox": [100, 300, 500, 500],
                    "block_order": 3,
                },
                {
                    "block_label": "image",
                    "block_content": (
                        '<div><img src="imgs/img_in_image_box_100_550_300_700.jpg"></div>'
                    ),
                    "block_bbox": [100, 550, 300, 700],
                    "block_order": 4,
                },
                {
                    "block_label": "number",
                    "block_content": "12",
                    "block_bbox": [100, 750, 150, 780],
                    "block_order": 5,
                },
            ]
        },
        "markdown": {"text": "<p>页面 Markdown</p>"},
    }

    layout = ocr_client._paddleocr_page_to_layout(page, {"width": 1000, "height": 1000})

    assert [block.kind for block in layout.blocks] == ["heading", "text", "table", "image"]
    assert layout.blocks[0].text == "3.5 常见故障"
    assert layout.blocks[1].text == "按下按钮开机。"
    assert layout.blocks[2].text == "指标 | 值\n收缩压 | 120"
    assert layout.blocks[3].bbox == (100.0, 550.0, 300.0, 700.0)
    assert layout.markdown == "页面 Markdown"


def test_paddleocr_table_cell_images_become_image_blocks() -> None:
    """表格单元格内图片转为独立 image 块，坐标缺失时只占位不提取。"""
    html = (
        '<table><tr><td>印章<img src="imgs/img_in_image_box_20_30_40_50.jpg"></td>'
        '<td>盖章<img src="imgs/other.jpg"></td></tr></table>'
    )

    rows, images = ocr_client._html_table_to_markdown(html, 100, 100)

    assert rows == ["印章 [图片] | 盖章 [图片]"]
    assert len(images) == 1
    assert images[0].kind == "image"
    assert images[0].bbox == (200.0, 300.0, 400.0, 500.0)


def test_paddleocr_table_rowspan_copies_text() -> None:
    """跨行单元格文字复制到后续行，避免列对齐错位。"""
    html = (
        '<table><tr><td rowspan="2">部件名称</td><td>铅</td></tr>'
        "<tr><td>汞</td></tr></table>"
    )

    rows, images = ocr_client._html_table_to_markdown(html, 1000, 1000)

    assert rows == ["部件名称 | 铅", "部件名称 | 汞"]
    assert images == []
