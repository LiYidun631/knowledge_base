"""验证在线问答提示词的事实约束、资料边界和模型请求参数。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from knowledge_service.api.v1 import answer as answer_module
from knowledge_service.api.v1.answer import (
    _answer_context,
    _ensure_answer_images,
    _exact_answer_with_images,
)
from knowledge_service.api.v1.schemas import KnowledgeAnswerRequest
from knowledge_service.api.v1.search import _result_to_schema
from knowledge_service.application.search.service import KnowledgeSearchNotFound
from knowledge_service.core.config import Settings
from knowledge_service.infrastructure.model_providers import chat


def test_normalize_answer_expands_wide_table_and_cleans_html_breaks() -> None:
    """宽表格应转换为带列名的列表，便于 Apifox 和普通文本界面阅读。"""

    content = """说明：\\n
| 型号 | 使用场景 | 注意事项 |
| --- | --- | --- |
| A 型 | 室内<br>家庭使用 | 请勿进水 |
| B 型 | 室外 | 需要校准 |

下一步：\\n请确认电量。"""

    assert chat.normalize_answer(content) == (
        "说明：\n\n"
        "- **型号：** A 型\n"
        "  - **使用场景：** 室内；家庭使用\n"
        "  - **注意事项：** 请勿进水\n"
        "- **型号：** B 型\n"
        "  - **使用场景：** 室外\n"
        "  - **注意事项：** 需要校准\n\n"
        "下一步：\n请确认电量。"
    )


def test_normalize_answer_preserves_non_table_text() -> None:
    """普通文本和非表格竖线不能被误删。"""

    assert chat.normalize_answer("A | B\\n普通文本") == "A | B\n普通文本"


def test_answer_context_and_search_result_include_absolute_image_url(monkeypatch) -> None:
    """问答上下文应把占位符原位换成可访问图片地址，检索响应使用同一定址。"""

    settings = Settings(
        public_base_url="https://kb.example.com",
        api_prefix="/api/v1",
    )
    monkeypatch.setattr(
        "knowledge_service.application.documents.rendering.get_settings", lambda: settings
    )
    monkeypatch.setattr("knowledge_service.api.v1.search.get_settings", lambda: settings)
    asset_id = "00000000-0000-0000-0000-000000000001"
    marker = "[[KB_ASSET_KEY:assets/doc/1.png]]"
    result = SimpleNamespace(
        content=f"工作原理示意图{marker}",
        context=None,
        assets=[
            SimpleNamespace(
                id=asset_id,
                media_type="image/png",
                description="工作原理图",
                page_number=1,
                bbox=None,
                marker=marker,
            )
        ],
        chunk_id="00000000-0000-0000-0000-000000000002",
        document_id="00000000-0000-0000-0000-000000000003",
        document_version_id="00000000-0000-0000-0000-000000000004",
        version_number=1,
        filename="说明书.docx",
        file_id="file-1",
        page_number=1,
        section="工作原理",
        cosine_distance=None,
        bm25_score=None,
        rrf_score=None,
        rerank_score=None,
        context_sources=[],
    )

    expected_url = f"https://kb.example.com/api/v1/document-assets/{asset_id}"
    assert f"工作原理示意图![]({expected_url})" in _answer_context(1, result)
    assert _result_to_schema(1, result).assets[0].url == expected_url


def test_answer_context_renders_marker_images_in_position(monkeypatch) -> None:
    """已知占位符原位换成真实图片地址，未知占位符退化为位置提示且不泄露内部路径。"""

    settings = Settings(
        public_base_url="https://kb.example.com",
        api_prefix="/api/v1",
    )
    monkeypatch.setattr(
        "knowledge_service.application.documents.rendering.get_settings", lambda: settings
    )
    marker = "[[KB_ASSET_KEY:assets/doc-1/1.png]]"
    asset = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        marker=marker,
    )
    result = SimpleNamespace(
        content=(
            "步骤一：安装电池。"
            f"{marker}"
            " 步骤二：开机。[[KB_ASSET_KEY:assets/doc-1/missing.png]]"
        ),
        context=None,
        assets=[asset],
    )

    context = _answer_context(1, result)

    image_url = "https://kb.example.com/api/v1/document-assets/00000000-0000-0000-0000-000000000001"
    assert f"步骤一：安装电池。![]({image_url}) 步骤二：开机。[图片]" in context
    assert "[[KB_ASSET" not in context


def test_answer_images_keep_model_omitted_assets_out(monkeypatch) -> None:
    """模型没有保留的图片不再补到答案末尾，避免二维码等无关图片污染事实型答案。"""

    settings = Settings(
        public_base_url="https://kb.example.com",
        api_prefix="/api/v1",
    )
    monkeypatch.setattr(
        "knowledge_service.application.documents.rendering.get_settings", lambda: settings
    )
    asset = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        marker="[[KB_ASSET_KEY:assets/doc/qr.png]]",
    )
    result = SimpleNamespace(assets=[asset])

    answer = _ensure_answer_images("住所：徐州高新技术产业开发区银山路19号", [result])

    assert "document-assets" not in answer
    assert answer == "住所：徐州高新技术产业开发区银山路19号"


def test_answer_images_render_marker_preserved_by_model(monkeypatch) -> None:
    """模型原样保留占位符时，最终答案应在原位置渲染真实图片地址。"""

    settings = Settings(
        public_base_url="https://kb.example.com",
        api_prefix="/api/v1",
    )
    monkeypatch.setattr(
        "knowledge_service.application.documents.rendering.get_settings", lambda: settings
    )
    marker = "[[KB_ASSET_KEY:assets/doc/1.png]]"
    asset = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000099",
        marker=marker,
    )
    result = SimpleNamespace(assets=[asset])

    answer = _ensure_answer_images(f"步骤一：安装电池。{marker} 步骤二：开机。", [result])

    image_url = "https://kb.example.com/api/v1/document-assets/00000000-0000-0000-0000-000000000099"
    assert answer == f"步骤一：安装电池。![]({image_url}) 步骤二：开机。"


def test_answer_images_strip_fake_links_without_appending_assets(monkeypatch) -> None:
    """模型把位置提示写成假链接时应剔除，未渲染的占位符资产不追加。"""

    settings = Settings(
        public_base_url="https://kb.example.com",
        api_prefix="/api/v1",
    )
    monkeypatch.setattr(
        "knowledge_service.application.documents.rendering.get_settings", lambda: settings
    )
    asset = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000099",
        marker="[[KB_ASSET_KEY:assets/doc/1.png]]",
    )
    result = SimpleNamespace(assets=[asset])

    answer = _ensure_answer_images("步骤一：安装电池。![](图片地址) 步骤二：开机。", [result])

    assert "![](图片地址)" not in answer
    assert "document-assets" not in answer
    assert answer == "步骤一：安装电池。 步骤二：开机。"


def test_exact_image_answer_merges_plain_chunks_in_source_order(monkeypatch) -> None:
    """图文原文回答要合并同章节的纯文字块，并按原文顺序只渲染一次图片。"""
    settings = Settings(
        public_base_url="https://kb.example.com",
        api_prefix="/api/v1",
    )
    monkeypatch.setattr(
        "knowledge_service.application.documents.rendering.get_settings",
        lambda: settings,
    )
    marker = "[[KB_ASSET:ear-illustration]]"
    asset_id = "00000000-0000-0000-0000-000000000099"
    version_id = "00000000-0000-0000-0000-000000000098"

    def source(chunk_id: int, ordinal: int, content: str):
        return SimpleNamespace(chunk_id=chunk_id, ordinal=ordinal, content=content)

    image_result = SimpleNamespace(
        content=f"步骤二：调整耳朵。{marker}",
        context=f"步骤一：取下额温帽。\n\n步骤二：调整耳朵。{marker}",
        document_id="document-1",
        document_version_id=version_id,
        section="3 正确使用方法 / 3.1 耳温测量",
        context_ordinal=1,
        context_sources=[
            source("chunk-1", 1, "步骤一：取下额温帽。"),
            source("chunk-2", 3, f"步骤二：调整耳朵。{marker}"),
        ],
        assets=[SimpleNamespace(id=asset_id, marker=marker)],
    )
    text_result = SimpleNamespace(
        content="步骤三：完成测量。",
        context="步骤二：调整耳朵。\n\n步骤三：完成测量。",
        document_id="document-1",
        document_version_id=version_id,
        section="3 正确使用方法 / 3.1 耳温测量",
        context_ordinal=3,
        context_sources=[
            source("chunk-2", 3, f"步骤二：调整耳朵。{marker}"),
            source("chunk-3", 5, "步骤三：完成测量。"),
        ],
        assets=[],
    )
    unrelated = SimpleNamespace(
        content="无关章节",
        context="无关章节",
        document_id="document-1",
        document_version_id=version_id,
        section="4 清洁保养",
        context_sources=[source("chunk-4", 7, "无关章节")],
        assets=[],
    )

    answer = _exact_answer_with_images([text_result, unrelated, image_result])

    image_url = f"https://kb.example.com/api/v1/document-assets/{asset_id}"
    assert answer.index("步骤一") < answer.index("步骤二")
    assert answer.index(f"![]({image_url})") < answer.index("步骤三")
    assert answer.count(image_url) == 1
    assert "无关章节" not in answer


def test_generate_answer_preserves_context_boundary_and_factual_temperature(monkeypatch) -> None:
    """聊天请求应明确标记资料边界，并使用较低温度减少事实改写。"""

    settings = Settings(
        chat_base_url="https://chat.example/v1",
        chat_path="/chat/completions",
        chat_model_name="qwen-plus",
        chat_secret_ref="test-chat-key",
        chat_temperature=0.0,
        chat_system_prompt_file=Path("prompts/knowledge_answer_system.txt"),
    )
    captured: dict[str, object] = {}

    def response(request: httpx.Request) -> httpx.Response:
        """检查发送给模型的提示词和请求体。"""
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "整理后的答案"}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(chat, "get_settings", lambda: settings)
    monkeypatch.setattr(chat, "resolve_secret", lambda _secret_ref: "test-key")
    monkeypatch.setattr(
        chat.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs),
    )

    usage: dict[str, int] = {}
    answer = asyncio.run(
        chat.generate_answer(
            query="退款期限是多少？",
            context="退款规则：订单完成后七天内可以申请退款。",
            usage_sink=usage,
        )
    )

    assert answer == "整理后的答案"
    assert usage == {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
    assert captured["temperature"] == 0.0
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert "knowledge_context" not in messages[0]["content"]
    assert "<knowledge_context>" in messages[1]["content"]
    assert "</knowledge_context>" in messages[1]["content"]
    assert "<user_query>" in messages[1]["content"]
    assert "退款规则：订单完成后七天内可以申请退款。" in messages[1]["content"]
    assert "标准 Markdown" in messages[0]["content"]


def test_generate_answer_uses_chat_profile_settings(monkeypatch) -> None:
    """传入生成模型档案时，地址、模型名与密钥都应来自档案而不是 .env。"""

    settings = Settings(
        chat_base_url="https://unused-env.example/v1",
        chat_path="/chat/completions",
        chat_model_name="env-model",
        chat_secret_ref="test-chat-key",
        chat_temperature=0.0,
        chat_system_prompt_file=Path("prompts/knowledge_answer_system.txt"),
    )
    captured: dict[str, object] = {}

    def response(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "档案答案"}}]},
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(chat, "get_settings", lambda: settings)
    monkeypatch.setattr(
        chat,
        "resolve_profile_api_key",
        lambda _profile: "profile-key",
    )
    monkeypatch.setattr(
        chat.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs),
    )
    profile = SimpleNamespace(
        model_name="qwen-plus",
        settings={
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "chat_path": "/chat/completions",
        },
        api_key_cipher=None,
        secret_ref=None,
    )

    answer = asyncio.run(
        chat.generate_answer(
            query="怎么开机？", context="开机：长按电源键三秒。", profile=profile
        )
    )

    assert answer == "档案答案"
    assert captured["model"] == "qwen-plus"
    assert captured["url"] == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
    assert captured["temperature"] == 0.0
    assert "<knowledge_context>" in captured["messages"][1]["content"]


def test_generate_answer_rejects_chat_profile_without_secret(monkeypatch) -> None:
    """档案缺少库内密钥且无环境引用时，应明确报密钥未配置。"""

    settings = Settings(
        chat_temperature=0.0,
        chat_system_prompt_file=Path("prompts/knowledge_answer_system.txt"),
    )
    monkeypatch.setattr(chat, "get_settings", lambda: settings)
    monkeypatch.setattr(chat, "resolve_profile_api_key", lambda _profile: None)
    profile = SimpleNamespace(
        model_name="qwen-plus",
        settings={"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
        api_key_cipher=None,
        secret_ref=None,
    )

    with pytest.raises(chat.ChatProviderError, match="CHAT_SECRET_MISSING"):
        asyncio.run(
            chat.generate_answer(query="问题", context="资料", profile=profile)
        )


def test_generate_answer_stream_yields_delta_chunks(monkeypatch) -> None:
    """流式请求应设置 stream 开关，并按顺序产出每个增量文本。"""

    settings = Settings(
        chat_base_url="https://chat.example/v1",
        chat_path="/chat/completions",
        chat_model_name="qwen-plus",
        chat_secret_ref="test-chat-key",
        chat_temperature=0.0,
        chat_system_prompt_file=Path("prompts/knowledge_answer_system.txt"),
    )
    captured: dict[str, object] = {}

    sse_body = (
        'data: {"choices":[{"delta":{"content":"第一步"}}]}\n\n'
        'data: {"choices":[{"delta":{}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"第二步"}}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":80,"completion_tokens":12,"total_tokens":92}}\n\n'
        "data: [DONE]\n\n"
    ).encode()

    def response(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=sse_body)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(chat, "get_settings", lambda: settings)
    monkeypatch.setattr(chat, "resolve_secret", lambda _secret_ref: "test-key")
    monkeypatch.setattr(
        chat.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs),
    )

    usage: dict[str, int] = {}

    async def collect() -> list[str]:
        return [
            delta
            async for delta in chat.generate_answer_stream(
                query="怎么开机？", context="开机：长按电源键三秒。", usage_sink=usage
            )
        ]

    assert asyncio.run(collect()) == ["第一步", "第二步"]
    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}
    assert usage == {"prompt_tokens": 80, "completion_tokens": 12, "total_tokens": 92}
    assert captured["model"] == "qwen-plus"
    assert "<knowledge_context>" in captured["messages"][1]["content"]


def _stream_result() -> SimpleNamespace:
    """构造一份能通过 _result_to_schema 的假检索结果。"""
    asset_id = "00000000-0000-0000-0000-000000000001"
    marker = "[[KB_ASSET_KEY:assets/doc/1.png]]"
    return SimpleNamespace(
        content=f"步骤一：安装电池。{marker}",
        context=None,
        assets=[
            SimpleNamespace(
                id=asset_id,
                media_type="image/png",
                description="安装示意图",
                page_number=1,
                bbox=None,
                marker=marker,
            )
        ],
        chunk_id="00000000-0000-0000-0000-000000000002",
        document_id="00000000-0000-0000-0000-000000000003",
        document_version_id="00000000-0000-0000-0000-000000000004",
        version_number=1,
        filename="说明书.docx",
        file_id="file-1",
        page_number=1,
        section="使用方法",
        cosine_distance=None,
        bm25_score=None,
        rrf_score=None,
        rerank_score=None,
        context_sources=[],
        model_name="A 型",
        model_names=[],
    )


async def fake_stream_missing(*_args, **_kwargs):
    raise AssertionError("不应调用模型生成")
    yield  # pragma: no cover


def _collect_stream_events(
    monkeypatch,
    *,
    search,
    stream=None,
    finished: dict,
    query: str = "退款期限是多少？",
    faq=None,
) -> list[tuple[str, dict]]:
    """驱动 _stream_qa_events 并解析出 (事件名, 数据) 序列。"""
    settings = Settings(
        public_base_url="https://kb.example.com",
        api_prefix="/api/v1",
    )
    monkeypatch.setattr(
        "knowledge_service.application.documents.rendering.get_settings", lambda: settings
    )
    monkeypatch.setattr("knowledge_service.api.v1.search.get_settings", lambda: settings)

    record = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-0000000000aa"),
        request_id="req-stream-1",
    )

    class FakeFailureSession:
        """失败落库改走独立会话后，测试里给一个不连库的替身。"""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, *_args, **_kwargs):
            return SimpleNamespace()

        async def commit(self):
            return None

    monkeypatch.setattr(answer_module, "get_session_factory", lambda: FakeFailureSession)

    async def fake_scalar(*_args, **_kwargs):
        return None

    async def fake_finish_success(_session, _record, **kwargs):
        finished["success"] = kwargs

    async def fake_finish_failure(_session, _record, **kwargs):
        finished["failure"] = kwargs

    def fake_add_event(*_args, **_kwargs):
        return None

    async def fake_finish_no_results(*_args, **_kwargs):
        finished["no_results"] = True

    async def fake_match_faq(*_args, **_kwargs):
        return faq

    monkeypatch.setattr(answer_module, "search_knowledge_base", search)
    monkeypatch.setattr(
        answer_module,
        "generate_answer_stream",
        stream if stream is not None else fake_stream_missing,
    )
    monkeypatch.setattr(answer_module, "finish_qa_success", fake_finish_success)
    monkeypatch.setattr(answer_module, "finish_qa_failure", fake_finish_failure)
    monkeypatch.setattr(answer_module, "add_qa_event", fake_add_event)
    monkeypatch.setattr(answer_module, "finish_qa_no_results", fake_finish_no_results)
    monkeypatch.setattr(answer_module, "match_faq", fake_match_faq)

    payload = KnowledgeAnswerRequest(
        query=query, top_k=5, retrieval_mode="hybrid", include_context=True
    )

    async def collect() -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        async for frame in answer_module._stream_qa_events(
            session=SimpleNamespace(scalar=fake_scalar),
            record=record,
            knowledge_base=SimpleNamespace(id=UUID("00000000-0000-0000-0000-0000000000bb")),
            knowledge_base_id=UUID("00000000-0000-0000-0000-0000000000bb"),
            company_id=UUID("00000000-0000-0000-0000-0000000000cc"),
            payload=payload,
        ):
            lines = frame.splitlines()
            name = lines[0].split(":", 1)[1].strip()
            data = json.loads(lines[1].split(":", 1)[1].strip())
            events.append((name, data))
        return events

    return asyncio.run(collect())


def test_stream_qa_events_emit_meta_deltas_and_done(monkeypatch) -> None:
    """流式问答应按 meta → delta* → done 下发，最终答案还原图片并与 /ask 结构一致。"""

    marker = "[[KB_ASSET_KEY:assets/doc/1.png]]"

    async def fake_search(*_args, **_kwargs):
        return [_stream_result()]

    async def fake_stream(*_args, **_kwargs):
        yield "步骤一：安装电池。"
        yield marker
        yield " 步骤二：开机。"

    finished: dict[str, object] = {}
    events = _collect_stream_events(
        monkeypatch, search=fake_search, stream=fake_stream, finished=finished
    )

    names = [name for name, _ in events]
    assert names == ["meta", "delta", "delta", "delta", "done"]
    assert events[0][1]["qa_record_id"] == "00000000-0000-0000-0000-0000000000aa"
    assert events[0][1]["request_id"] == "req-stream-1"
    assert "".join(data["text"] for name, data in events if name == "delta") == (
        f"步骤一：安装电池。{marker} 步骤二：开机。"
    )
    done = events[-1][1]
    image_url = "https://kb.example.com/api/v1/document-assets/00000000-0000-0000-0000-000000000001"
    assert done["answer"] == f"步骤一：安装电池。![]({image_url}) 步骤二：开机。"
    assert done["clarification_required"] is False
    assert done["sources"][0]["source"]["filename"] == "说明书.docx"
    assert finished["success"]["answer"] == done["answer"]
    assert "failure" not in finished


def test_stream_qa_events_emit_error_when_search_fails(monkeypatch) -> None:
    """检索失败时应下发 error 事件并落失败记录，不产生 delta 或 done。"""

    async def fake_search(*_args, **_kwargs):
        raise KnowledgeSearchNotFound("知识库不存在或已归档")

    finished: dict[str, object] = {}
    events = _collect_stream_events(monkeypatch, search=fake_search, finished=finished)

    names = [name for name, _ in events]
    assert names == ["meta", "error"]
    assert events[-1][1]["detail"] == "知识库不存在或已归档"
    assert finished["failure"]["stage"] == "retrieval"
    assert "success" not in finished


def test_stream_qa_events_emit_clarification_done_without_model(monkeypatch) -> None:
    """多型号未指明时应直接下发澄清 done，不调用模型。"""

    result_a = _stream_result()
    result_b = SimpleNamespace(**{**vars(_stream_result()), "model_name": "B 型"})

    async def fake_search(*_args, **_kwargs):
        return [result_a, result_b]

    finished: dict[str, object] = {}
    events = _collect_stream_events(
        monkeypatch, search=fake_search, finished=finished, query="怎么清洁产品？"
    )

    names = [name for name, _ in events]
    assert names == ["meta", "done"]
    done = events[-1][1]
    assert done["clarification_required"] is True
    assert done["clarification_options"] == ["A 型", "B 型"]
    assert "success" in finished


def test_stream_qa_events_faq_direct_answer_skips_search_and_model(monkeypatch) -> None:
    """FAQ 命中时原样直出：不检索、不生成，done 带 faq_matched 标记。"""

    async def fake_search(*_args, **_kwargs):
        raise AssertionError("FAQ 命中后不应再检索")

    faq_entry = SimpleNamespace(id=UUID("00000000-0000-0000-0000-0000000000dd"))
    faq_entry.answer = "长按电源键三秒即可开机。"

    finished: dict[str, object] = {}
    events = _collect_stream_events(
        monkeypatch,
        search=fake_search,
        finished=finished,
        query="怎么开机？",
        faq=faq_entry,
    )

    names = [name for name, _ in events]
    assert names == ["meta", "done"]
    done = events[-1][1]
    assert done["answer"] == "长按电源键三秒即可开机。"
    assert done["faq_matched"] is True
    assert done["sources"] == []
    assert finished["success"]["answer"] == done["answer"]
    # 流式 FAQ 直出同样落库标记，与 /ask 保持一致。
    assert finished["success"]["faq_matched"] is True
    assert "failure" not in finished


def test_stream_qa_events_faq_miss_falls_through_to_search(monkeypatch) -> None:
    """FAQ 未命中时继续走检索生成流程，done 不带 faq_matched 标记。"""

    async def fake_search(*_args, **_kwargs):
        return [_stream_result()]

    async def fake_stream(*_args, **_kwargs):
        yield "普通生成答案。"

    finished: dict[str, object] = {}
    events = _collect_stream_events(
        monkeypatch, search=fake_search, stream=fake_stream, finished=finished
    )

    names = [name for name, _ in events]
    assert names == ["meta", "delta", "done"]
    assert events[-1][1]["faq_matched"] is False
    assert "success" in finished

