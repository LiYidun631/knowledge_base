"""通过 HTTP mock 验证真实 Embedding 协议与错误边界。"""

import asyncio
import json

import httpx
import pytest

from knowledge_service.infrastructure.model_providers import embeddings
from knowledge_service.infrastructure.persistence.models import ModelProfile


def _profile() -> ModelProfile:
    return ModelProfile(
        name="test-openai-embedding",
        kind="embedding",
        provider="openai_compatible",
        model_name="text-embedding-3-small",
        dimensions=2,
        settings={"base_url": "https://embedding.example/v1"},
        secret_ref="TEST_EMBEDDING_KEY",
    )


def test_embedding_sends_configured_model_url_key_and_reorders_results(monkeypatch) -> None:
    """供应商可返回乱序结果，适配器必须按输入索引恢复顺序。"""
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "test-key")

    def response(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://embedding.example/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {
            "model": "text-embedding-3-small",
            "input": ["甲", "乙"],
        }
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.1, 0.9]},
                    {"index": 0, "embedding": [0.9, 0.1]},
                ]
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        embeddings.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs),
    )
    assert asyncio.run(embeddings.embed_texts(_profile(), ["甲", "乙"])) == [[0.9, 0.1], [0.1, 0.9]]


@pytest.mark.parametrize(
    "data, expected",
    [
        ([{"index": 0, "embedding": [1.0]}], "DIMENSION_MISMATCH"),
        ([{"index": 0, "embedding": [float("nan"), 1.0]}], "DIMENSION_MISMATCH"),
        ([{"index": 1, "embedding": [1.0, 0.0]}], "INDEX_MISMATCH"),
    ],
)
def test_invalid_provider_vectors_rejected(monkeypatch, data, expected) -> None:
    """拒绝错误维度、非有限数值和不匹配的向量索引。"""
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "test-key")
    real_client = httpx.AsyncClient
    # NaN 无法作为标准 JSON 编码，模拟供应商可能返回的非标准响应。
    body = json.dumps({"data": data}).encode()
    monkeypatch.setattr(
        embeddings.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)), **kwargs
        ),
    )
    with pytest.raises(embeddings.EmbeddingProviderError, match=expected):
        asyncio.run(embeddings.embed_texts(_profile(), ["内容"]))


def test_missing_key_fails_before_provider_call(monkeypatch) -> None:
    """密钥引用不存在时不得发起无鉴权模型调用。"""
    monkeypatch.delenv("TEST_EMBEDDING_KEY", raising=False)
    with pytest.raises(embeddings.EmbeddingProviderError, match="SECRET_MISSING"):
        asyncio.run(embeddings.embed_texts(_profile(), ["内容"]))


@pytest.mark.parametrize("provider", ["dashscope", "bigmodel", "volcengine", "qianfan"])
def test_vendor_request_protocol(monkeypatch, provider) -> None:
    """厂商预设必须对应可执行的请求协议，而不仅是展示名称。"""
    from knowledge_service.cli.provision_catalog import ModelDefinition

    monkeypatch.setenv("TEST_EMBEDDING_KEY", "test-key")
    definition = ModelDefinition(
        name="vendor-test",
        kind="embedding",
        provider=provider,
        model_name="embedding-3" if provider == "bigmodel" else "deployed-embedding",
        dimensions=2,
        settings={"base_url": "https://vendor.example/v1"},
        secret_ref="TEST_EMBEDDING_KEY",
    )
    profile = ModelProfile(**definition.model_dump())

    def response(request):
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer test-key"
        if provider == "volcengine":
            assert request.url.path == "/v1/embeddings/multimodal"
            assert body["input"] == [{"type": "text", "text": "医学资料"}]
            assert body["dimensions"] == 2
            payload = {"data": {"embedding": [0.9, 0.1]}}
        else:
            assert request.url.path == "/v1/embeddings"
            assert body["input"] == ["医学资料"]
            if provider in {"dashscope", "bigmodel"}:
                assert body["dimensions"] == 2
            if provider in {"dashscope", "qianfan"}:
                assert body["encoding_format"] == "float"
            payload = {"data": [{"index": 0, "embedding": [0.9, 0.1]}]}
        return httpx.Response(200, json=payload)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        embeddings.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs),
    )
    assert asyncio.run(embeddings.embed_texts(profile, ["医学资料"])) == [[0.9, 0.1]]


def test_deepseek_cannot_be_registered_as_embedding() -> None:
    """不允许把只提供对话的 DeepSeek 官方档案误登记成向量模型。"""
    from knowledge_service.cli.provision_catalog import ModelDefinition

    with pytest.raises(ValueError, match="不支持"):
        ModelDefinition(
            name="deepseek",
            kind="embedding",
            provider="deepseek",
            model_name="deepseek-chat",
            dimensions=1024,
            settings={"base_url": "https://api.deepseek.com/v1"},
        )


def test_volcengine_requires_real_model_identity() -> None:
    """模板中的 Endpoint 占位符不能变成可选的已登记模型。"""
    from knowledge_service.cli.provision_catalog import ModelDefinition

    with pytest.raises(ValueError, match="Endpoint ID"):
        ModelDefinition(
            name="volcengine",
            kind="embedding",
            provider="volcengine",
            model_name="YOUR_VOLCENGINE_EMBEDDING_ENDPOINT_ID",
            dimensions=2048,
            settings={"base_url": "https://ark.cn-beijing.volces.com/api/v3"},
        )
