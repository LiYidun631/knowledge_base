"""按原文位置渲染图片引用，不用视觉描述替换原图。"""

import re
from collections.abc import Iterable
from typing import Any

from knowledge_service.core.config import get_settings

ASSET_PATTERN = re.compile(r"\[\[KB_ASSET(?:_KEY)?:[^\]]+\]\]")


def render_images(content: str, assets: Iterable[Any]) -> str:
    """图片 URL 按当前公开基址生成，正文不存储部署域名。"""
    settings = get_settings()
    base = f"{settings.public_base_url.rstrip('/')}{settings.api_prefix}/document-assets"
    for asset in assets:
        marker = getattr(asset, "marker", None)
        if marker:
            content = content.replace(marker, f"![]({base}/{asset.id})")
    return content


def has_images(content: str) -> bool:
    """识别内部图片引用和 HTML/Markdown 已保留的图片链接。"""
    return bool(ASSET_PATTERN.search(content) or re.search(r"!\[[^\]]*\]\(", content))
