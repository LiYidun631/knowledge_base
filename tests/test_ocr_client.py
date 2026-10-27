"""验证千帆视觉 OCR 请求、分页渲染和错误处理。"""

import base64
import json
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from knowledge_service.core.ocr_runtime import OcrRuntimeConfig, use_ocr_runtime
from knowledge_service.infrastructure import ocr_client
from knowledge_service.infrastructure.ocr_client import (
    _LAYOUT_JSON_PROMPT,
    _PLAIN_OCR_PROMPT,
    LayoutBlock,
    OcrServiceError,
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

    layout = ocr_client.analyze_image(
        b"image", settings=make_settings(ocr_model_name="qwen-vl-ocr")
    )

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
            200,
            request=httpx.Request("POST", url),
            json=paddleocr_response([paddleocr_page("图片说明")]),
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


def _runtime_profile(**overrides):
    values = {
        "base_url": "https://profile.example/v1",
        "chat_path": "/chat/completions",
        "model_name": "profile-ocr-model",
        "api_key": "profile-secret",
        "mode": "auto",
        "profile_name": "默认 OCR 档案",
    }
    values.update(overrides)
    return OcrRuntimeConfig(**values)


def test_runtime_profile_overrides_environment_for_image(monkeypatch) -> None:
    """注入默认档案后应使用档案地址、模型和密钥，而不是 .env 配置。"""
    calls = []
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: "env-secret")

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return success_response("档案识别结果")

    monkeypatch.setattr(httpx, "post", fake_post)

    with use_ocr_runtime(_runtime_profile()):
        text = recognize_image(b"png-bytes", settings=make_settings())

    assert text == "档案识别结果"
    url, request = calls[0]
    assert url == "https://profile.example/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer profile-secret"
    assert request["json"]["model"] == "profile-ocr-model"


def test_runtime_vision_mode_overrides_paddleocr_name_detection(monkeypatch) -> None:
    """档案显式选择 vision 时，即使模型名含 paddleocr 也走聊天视觉接口。"""
    calls = []
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: (
            calls.append(url) or success_response("视觉识别结果")
        ),
    )

    with use_ocr_runtime(
        _runtime_profile(model_name="paddleocr-vl-0.9b", mode="vision")
    ):
        recognize_image(b"png-bytes", settings=make_settings())

    assert calls == ["https://profile.example/v1/chat/completions"]


def test_runtime_paddleocr_mode_uses_dedicated_endpoint(monkeypatch) -> None:
    """档案选择 paddleocr 模式时走专用接口，而不拼接 chat_path。"""
    calls = []
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: (
            calls.append(url)
            or httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json=paddleocr_response([paddleocr_page("专用接口识别")]),
            )
        ),
    )

    with use_ocr_runtime(
        _runtime_profile(model_name="paddleocr-vl-0.9b", mode="paddleocr")
    ):
        text = recognize_image(b"png-bytes", settings=make_settings())

    assert text == "专用接口识别"
    assert calls == ["https://profile.example/v1/ocr/paddleocr"]


def test_runtime_profile_reaches_pdf_worker_threads(monkeypatch) -> None:
    """PDF 页在子线程识别时仍应读到本次入库注入的 OCR 档案。"""
    calls = []
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: "env-secret")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: (
            calls.append((url, kwargs)) or success_response("线程内识别")
        ),
    )

    with use_ocr_runtime(_runtime_profile()):
        pages = recognize_pdf_pages(make_pdf(2), settings=make_settings(ocr_model_name=""))

    assert pages == {1: "线程内识别", 2: "线程内识别"}
    assert {url for url, _ in calls} == {"https://profile.example/v1/chat/completions"}
    assert all(
        request["headers"]["Authorization"] == "Bearer profile-secret"
        for _, request in calls
    )


def test_runtime_profile_without_key_raises_before_http(monkeypatch) -> None:
    """档案缺密钥时给出针对模型管理的提示，且不发网络请求。"""
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: calls.append(args))

    with use_ocr_runtime(_runtime_profile(api_key=None)):
        with pytest.raises(OcrServiceError, match="OCR_API_KEY_MISSING") as error:
            recognize_image(b"png-bytes", settings=make_settings())

    assert "模型管理" in str(error.value)
    assert calls == []


def make_async_settings(**overrides):
    """构造百度异步 OCR 配置，默认走环境兜底而非库内档案。"""
    values = {
        "ocr_base_url": "https://qianfan.example/v2",
        "ocr_chat_path": "/chat/completions",
        "ocr_model_name": "PaddleOCR-VL",
        "ocr_timeout_seconds": 23,
        "ocr_render_dpi": 160,
        "ocr_page_concurrency": 2,
        "ocr_mode": "baidu_async",
        "ocr_async_base_url": "https://aip.example",
        "ocr_async_task_path": "/rest/2.0/brain/online/v2/paddle-vl-parser/task",
        "ocr_async_token_path": "/oauth/2.0/token",
        "ocr_async_ak_ref": "BAIDU_OCR_AK",
        "ocr_async_sk_ref": "BAIDU_OCR_SK",
        "ocr_async_poll_interval_seconds": 0.01,
        "ocr_async_timeout_seconds": 5,
        "ocr_async_merge_tables": True,
        "ocr_async_relevel_titles": True,
        "ocr_async_analysis_chart": False,
        "ocr_async_recognize_seal": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def baidu_secret(key: str) -> str | None:
    """异步 OCR 测试使用的环境密钥。"""
    return {"BAIDU_OCR_AK": "ak", "BAIDU_OCR_SK": "sk"}.get(key)


def baidu_parse_payload(pages=None):
    """构造百度异步任务下载到的解析结果。"""
    if pages is not None:
        return {"file_name": "document.pdf", "pages": pages}
    return {
        "file_name": "page.png",
        "pages": [
            {
                "page_num": 0,
                "text": "# 标题\n\n正文\n\n| a | b |\n| --- | --- |\n| 1 | 2 |",
                "meta": {"page_width": 1000, "page_height": 1000},
                "layouts": [
                    {
                        "layout_id": "l1",
                        "text": "# 标题",
                        "position": [100, 100, 300, 50],
                        "type": "title",
                    },
                    {
                        "layout_id": "l2",
                        "text": "正文",
                        "position": [100, 200, 400, 40],
                        "type": "text",
                    },
                    {
                        "layout_id": "l3",
                        "text": "",
                        "position": [100, 300, 500, 100],
                        "type": "table",
                    },
                    {
                        "layout_id": "l4",
                        "text": "第 1 页",
                        "position": [500, 950, 50, 20],
                        "type": "head_tail",
                    },
                    {
                        "layout_id": "l5",
                        "text": "",
                        "position": [100, 500, 200, 100],
                        "type": "image",
                    },
                ],
                "tables": [
                    {
                        "layout_id": "l3",
                        "markdown": "a | b\n1 | 2",
                        "position": [100, 300, 500, 100],
                    }
                ],
                "images": [
                    {
                        "layout_id": "l5",
                        "position": [100, 500, 200, 100],
                        "description": "血压计示意图",
                    }
                ],
            }
        ],
    }


@pytest.fixture
def no_baidu_rate_limit(monkeypatch):
    """异步测试跳过进程内节流，避免每个请求固定等待。"""
    monkeypatch.setattr(ocr_client, "_BAIDU_SUBMIT_LIMITER", ocr_client._RateLimiter(0))
    monkeypatch.setattr(ocr_client, "_BAIDU_QUERY_LIMITER", ocr_client._RateLimiter(0))
    ocr_client._BAIDU_TOKEN_CACHE.clear()
    yield
    ocr_client._BAIDU_TOKEN_CACHE.clear()


def baidu_response(payload, *, url="https://aip.example", method="POST", status=200):
    return httpx.Response(
        status, request=httpx.Request(method, url), json=payload
    )


def test_baidu_async_image_submits_polls_and_maps_layout(
    monkeypatch, no_baidu_rate_limit
) -> None:
    """异步模式提交 AK:SK 表单、轮询任务并转换版面块。"""
    posts = []
    query_count = 0
    monkeypatch.setattr(
        ocr_client,
        "resolve_secret",
        lambda key: {"BAIDU_OCR_AK": "test-ak", "BAIDU_OCR_SK": "test-sk"}.get(key),
    )

    def fake_post(url, **kwargs):
        nonlocal query_count
        posts.append((url, kwargs))
        if url.endswith("/oauth/2.0/token"):
            return baidu_response({"access_token": "token-1", "expires_in": 3600})
        if url.endswith("/task/query"):
            query_count += 1
            if query_count == 1:
                return baidu_response({"error_code": 0, "result": {"status": "running"}})
            return baidu_response(
                {
                    "error_code": 0,
                    "result": {
                        "status": "success",
                        "parse_result_url": "https://bos.example/result.json",
                    },
                }
            )
        return baidu_response(
            {"error_code": 0, "error_msg": "", "result": {"task_id": "task-1"}}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(
        httpx, "get", lambda url, **kwargs: baidu_response(baidu_parse_payload())
    )

    text = recognize_image(
        b"png-bytes", settings=make_async_settings(), filename="page.png"
    )

    token_call = next(item for item in posts if item[0].endswith("/oauth/2.0/token"))
    assert token_call[1]["params"]["client_id"] == "test-ak"
    assert token_call[1]["params"]["client_secret"] == "test-sk"
    submit_call = next(item for item in posts if item[0].endswith("/task"))
    form = submit_call[1]["data"]
    assert submit_call[1]["params"] == {"access_token": "token-1"}
    assert form["file_name"] == "page.png"
    assert form["file_data"] == base64.b64encode(b"png-bytes").decode()
    assert form["merge_tables"] == "true"
    assert form["relevel_titles"] == "true"
    assert form["analysis_chart"] == "false"
    assert query_count == 2
    assert text == baidu_parse_payload()["pages"][0]["text"]


def test_baidu_async_converts_titles_tables_and_images(monkeypatch, no_baidu_rate_limit) -> None:
    """标题、正文、表格、图片按类型映射，页眉页脚等辅助块丢弃。"""
    monkeypatch.setattr(ocr_client, "resolve_secret", baidu_secret)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: baidu_response(
            {"access_token": "token", "expires_in": 3600}
            if url.endswith("/oauth/2.0/token")
            else {"error_code": 0, "result": {"task_id": "task-1"}}
            if url.endswith("/task")
            else {
                "error_code": 0,
                "result": {"status": "success", "parse_result_url": "https://bos.example/r.json"},
            }
        ),
    )
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: baidu_response(baidu_parse_payload()))

    layout = ocr_client.analyze_image(
        b"png-bytes", settings=make_async_settings(), filename="page.png"
    )

    assert [block.kind for block in layout.blocks] == ["heading", "text", "table", "image"]
    assert layout.blocks[0].text == "标题"
    assert layout.blocks[0].metadata["heading_level"] == 1
    assert layout.blocks[2].text == "a | b\n1 | 2"
    assert layout.blocks[3].text == "血压计示意图"
    assert layout.blocks[0].bbox == (100.0, 100.0, 400.0, 150.0)


def test_baidu_async_token_is_cached(monkeypatch, no_baidu_rate_limit) -> None:
    """同一 AK/SK 在有效期内只换取一次 access_token。"""
    token_calls = 0
    monkeypatch.setattr(ocr_client, "resolve_secret", baidu_secret)

    def fake_post(url, **kwargs):
        nonlocal token_calls
        if url.endswith("/oauth/2.0/token"):
            token_calls += 1
            return baidu_response({"access_token": "token", "expires_in": 3600})
        if url.endswith("/task/query"):
            return baidu_response(
                {
                    "error_code": 0,
                    "result": {"status": "success", "parse_result_url": "https://bos.example/r.json"},
                }
            )
        return baidu_response({"error_code": 0, "result": {"task_id": "task-1"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: baidu_response(baidu_parse_payload()))

    for _ in range(2):
        recognize_image(b"png-bytes", settings=make_async_settings(), filename="page.png")

    assert token_calls == 1


def test_baidu_async_requires_ak_and_sk_before_http(monkeypatch) -> None:
    """缺少 AK/SK 时提示配置方式，不发起网络请求。"""
    calls = []
    monkeypatch.setattr(ocr_client, "resolve_secret", lambda key: None)
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(OcrServiceError, match="AK:SK") as error:
        recognize_image(b"png-bytes", settings=make_async_settings())

    assert "BAIDU_OCR_AK" in str(error.value)
    assert calls == []


def test_baidu_async_task_failure_surfaces_provider_error(monkeypatch, no_baidu_rate_limit) -> None:
    """任务失败时返回任务错误描述，不下载结果。"""
    monkeypatch.setattr(ocr_client, "resolve_secret", baidu_secret)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: baidu_response(
            {"access_token": "token", "expires_in": 3600}
            if url.endswith("/oauth/2.0/token")
            else {"error_code": 0, "result": {"task_id": "task-1"}}
            if url.endswith("/task")
            else {
                "error_code": 0,
                "result": {
                    "status": "failed",
                    "task_error": {"error_msg": "文件解析失败"},
                },
            }
        ),
    )
    monkeypatch.setattr(
        httpx, "get", lambda *args, **kwargs: pytest.fail("失败任务不应下载结果")
    )

    with pytest.raises(OcrServiceError, match="文件解析失败"):
        recognize_image(b"png-bytes", settings=make_async_settings())


def test_baidu_async_business_error_code_is_rejected(monkeypatch, no_baidu_rate_limit) -> None:
    """HTTP 200 但带业务错误码时不能当作成功。"""
    monkeypatch.setattr(ocr_client, "resolve_secret", baidu_secret)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: baidu_response(
            {"access_token": "token", "expires_in": 3600}
            if url.endswith("/oauth/2.0/token")
            else {"error_code": 282003, "error_msg": "missing parameter"}
        ),
    )

    with pytest.raises(OcrServiceError, match="error_code=282003"):
        recognize_image(b"png-bytes", settings=make_async_settings())


def test_baidu_async_pdf_maps_selected_pages(monkeypatch, no_baidu_rate_limit) -> None:
    """只提交请求页，并把返回的 0 基页序映射回原始页码。"""
    monkeypatch.setattr(ocr_client, "resolve_secret", baidu_secret)
    submitted_pages = []

    def fake_post(url, **kwargs):
        if url.endswith("/oauth/2.0/token"):
            return baidu_response({"access_token": "token", "expires_in": 3600})
        if url.endswith("/task/query"):
            return baidu_response(
                {
                    "error_code": 0,
                    "result": {"status": "success", "parse_result_url": "https://bos.example/r.json"},
                }
            )
        import fitz

        document = fitz.open(
            stream=base64.b64decode(kwargs["data"]["file_data"]), filetype="pdf"
        )
        try:
            submitted_pages.append(len(document))
        finally:
            document.close()
        return baidu_response({"error_code": 0, "result": {"task_id": "task-1"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kwargs: baidu_response(
            baidu_parse_payload(
                [
                    {
                        "page_num": 0,
                        "text": "第二页内容",
                        "meta": {"page_width": 1000, "page_height": 1000},
                        "layouts": [
                            {
                                "layout_id": "p2",
                                "text": "第二页内容",
                                "position": [10, 10, 100, 20],
                                "type": "text",
                            }
                        ],
                    },
                    {
                        "page_num": 1,
                        "text": "第三页内容",
                        "meta": {"page_width": 1000, "page_height": 1000},
                        "layouts": [
                            {
                                "layout_id": "p3",
                                "text": "第三页内容",
                                "position": [10, 10, 100, 20],
                                "type": "text",
                            }
                        ],
                    },
                ]
            )
        ),
    )

    results = ocr_client.analyze_pdf_pages(
        make_pdf(3), settings=make_async_settings(), page_numbers=[2, 3]
    )

    assert submitted_pages == [2]
    assert results[2].markdown == "第二页内容"
    assert results[3].markdown == "第三页内容"


def test_baidu_async_runtime_profile_options(
    monkeypatch, no_baidu_rate_limit
) -> None:
    """档案可覆盖 AIP 基址、轮询参数与版面选项。"""
    posts = []
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: posts.append((url, kwargs))
        or baidu_response(
            {"access_token": "token", "expires_in": 3600}
            if url.endswith("/oauth/2.0/token")
            else {"error_code": 0, "result": {"task_id": "task-1"}}
            if url.endswith("/task")
            else {
                "error_code": 0,
                "result": {"status": "success", "parse_result_url": "https://bos.example/r.json"},
            }
        ),
    )
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: baidu_response(baidu_parse_payload()))
    runtime = _runtime_profile(
        api_key="AK:SK",
        mode="baidu_async",
        settings={
            "async_base_url": "https://aip.profile",
            "async_options": {"merge_tables": False, "analysis_chart": True},
        },
    )

    with use_ocr_runtime(runtime):
        recognize_image(b"png-bytes", settings=make_async_settings())

    submit_call = next(item for item in posts if item[0].endswith("/task"))
    assert submit_call[0] == (
        "https://aip.profile/rest/2.0/brain/online/v2/paddle-vl-parser/task"
    )
    assert submit_call[1]["data"]["merge_tables"] == "false"
    assert submit_call[1]["data"]["analysis_chart"] == "true"
