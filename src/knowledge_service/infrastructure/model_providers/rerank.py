"""DashScope/OpenAI-compatible Rerank 服务适配器。"""

from typing import Any

import httpx

from knowledge_service.core.config import get_settings
from knowledge_service.core.credentials import resolve_profile_api_key
from knowledge_service.infrastructure.persistence.models import ModelProfile


class RerankProviderError(RuntimeError):
    """Rerank 调用失败或返回值违反模型档案契约。"""


async def rerank_documents(
    profile: ModelProfile,
    *,
    query: str,
    documents: list[str],
) -> list[tuple[int, float]]:
    """按候选索引和相关度分数返回服务商的重排结果。"""
    base_url = str(profile.settings.get("base_url", "")).rstrip("/")
    path = str(profile.settings.get("rerank_path", "/reranks"))
    if not base_url or not path.startswith("/"):
        raise RerankProviderError("RERANK_PROFILE_INVALID: Rerank 模型档案缺少 URL 或路径")

    headers = {"Content-Type": "application/json"}
    api_key = resolve_profile_api_key(profile)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif profile.secret_ref or profile.api_key_cipher:
        raise RerankProviderError("RERANK_SECRET_MISSING: Rerank 模型密钥未配置")
    body = {
        "model": profile.model_name,
        "query": query,
        "documents": documents,
        "top_n": len(documents),
    }
    try:
        async with httpx.AsyncClient(timeout=get_settings().rerank_timeout_seconds) as client:
            response = await client.post(f"{base_url}{path}", headers=headers, json=body)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
    except httpx.TimeoutException as exc:
        raise RerankProviderError("RERANK_TIMEOUT: Rerank 服务请求超时") from exc
    except httpx.HTTPStatusError as exc:
        raise RerankProviderError(
            f"RERANK_PROVIDER_HTTP_{exc.response.status_code}: Rerank 服务返回错误"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise RerankProviderError("RERANK_PROVIDER_ERROR: 无法读取 Rerank 服务响应") from exc

    try:
        results = payload.get("results") or payload.get("output", {}).get("results")
        ranked = [(int(item["index"]), float(item["relevance_score"])) for item in results]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise RerankProviderError("RERANK_RESPONSE_INVALID: Rerank 响应结构无效") from exc
    indexes = [index for index, _ in ranked]
    if (
        not ranked
        or len(set(indexes)) != len(indexes)
        or any(index < 0 or index >= len(documents) for index in indexes)
    ):
        raise RerankProviderError("RERANK_RESPONSE_INVALID: Rerank 返回了无效候选索引")
    return sorted(ranked, key=lambda item: item[1], reverse=True)
