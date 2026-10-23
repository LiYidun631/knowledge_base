"""OpenAI-compatible Embedding 服务适配器。"""

import math
from typing import Any

import httpx

from knowledge_service.core.config import get_settings
from knowledge_service.core.credentials import resolve_profile_api_key
from knowledge_service.infrastructure.persistence.models import ModelProfile


class EmbeddingProviderError(RuntimeError):
    """Embedding 调用失败或返回值违反模型档案契约。"""


SUPPORTED_PROVIDERS = {
    "openai_compatible",
    "dashscope",
    "bigmodel",
    "volcengine",
    "qianfan",
}


async def embed_texts(profile: ModelProfile, texts: list[str]) -> list[list[float]]:
    """按档案中的 URL、模型名和密钥引用请求真实向量。"""
    if profile.provider not in SUPPORTED_PROVIDERS:
        raise EmbeddingProviderError("EMBEDDING_PROVIDER_UNSUPPORTED: 当前模型适配器不支持该供应商")
    base_url = str(profile.settings.get("base_url", "")).rstrip("/")
    embedding_path = str(profile.settings.get("embedding_path", "/embeddings"))
    if not base_url or profile.dimensions is None:
        raise EmbeddingProviderError("EMBEDDING_PROFILE_INVALID: 模型档案缺少 URL 或维度")
    if any(not text.strip() for text in texts):
        raise EmbeddingProviderError("EMBEDDING_INPUT_EMPTY: Embedding 输入包含空文本")

    headers = {"Content-Type": "application/json"}
    api_key = resolve_profile_api_key(profile)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif profile.secret_ref or profile.api_key_cipher:
        raise EmbeddingProviderError("EMBEDDING_SECRET_MISSING: 模型密钥未配置")

    request_body: dict[str, Any] = {"model": profile.model_name, "input": texts}
    if profile.provider == "dashscope" or (
        profile.provider == "bigmodel" and profile.model_name == "embedding-3"
    ):
        request_body["dimensions"] = profile.dimensions
    if profile.provider in {"dashscope", "qianfan"}:
        request_body["encoding_format"] = "float"
    if (
        profile.provider == "volcengine"
        or profile.settings.get("request_format") == "volcengine_multimodal"
    ):
        # 火山方舟多模态向量化接口一次只接受一段文本，返回 data.embedding。
        if len(texts) != 1:
            raise EmbeddingProviderError(
                "EMBEDDING_BATCH_UNSUPPORTED: 火山引擎模型每次只支持一段文本"
            )
        request_body["input"] = [{"type": "text", "text": texts[0]}]
        request_body["encoding_format"] = "float"
        request_body["dimensions"] = profile.dimensions
    try:
        async with httpx.AsyncClient(timeout=get_settings().embedding_timeout_seconds) as client:
            response = await client.post(
                f"{base_url}{embedding_path}",
                headers=headers,
                json=request_body,
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
    except httpx.TimeoutException as exc:
        raise EmbeddingProviderError("EMBEDDING_TIMEOUT: Embedding 服务请求超时") from exc
    except httpx.HTTPStatusError as exc:
        raise EmbeddingProviderError(
            f"EMBEDDING_PROVIDER_HTTP_{exc.response.status_code}: Embedding 服务返回错误"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise EmbeddingProviderError(
            "EMBEDDING_PROVIDER_ERROR: 无法读取 Embedding 服务响应"
        ) from exc

    try:
        if (
            profile.provider == "volcengine"
            or profile.settings.get("request_format") == "volcengine_multimodal"
        ):
            items = [{"index": 0, "embedding": payload["data"]["embedding"]}]
        else:
            items = sorted(payload["data"], key=lambda item: int(item["index"]))
        if [item["index"] for item in items] != list(range(len(texts))):
            raise EmbeddingProviderError("EMBEDDING_INDEX_MISMATCH: 返回向量的索引缺失或重复")
        vectors = [[float(value) for value in item["embedding"]] for item in items]
    except (KeyError, TypeError, ValueError) as exc:
        raise EmbeddingProviderError("EMBEDDING_RESPONSE_INVALID: Embedding 响应结构无效") from exc
    if len(vectors) != len(texts):
        raise EmbeddingProviderError("EMBEDDING_COUNT_MISMATCH: 返回向量数量与输入数量不一致")
    if any(
        len(vector) != profile.dimensions or any(not math.isfinite(value) for value in vector)
        for vector in vectors
    ):
        raise EmbeddingProviderError("EMBEDDING_DIMENSION_MISMATCH: 向量维度或数值不符合模型档案")
    return vectors
