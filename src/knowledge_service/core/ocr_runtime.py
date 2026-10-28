"""入库协程向同步解析器传递 OCR 运行时配置，使库内默认档案优先于环境配置。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OcrRuntimeConfig:
    """一次入库解析使用的 OCR 连接参数；api_key 已解密，只在内存中短期存在。"""

    base_url: str
    chat_path: str
    model_name: str
    api_key: str | None = field(default=None, repr=False)
    mode: str = "auto"
    profile_name: str | None = None
    # 档案 settings 透传，异步通道从其中读取地址、轮询与版面选项。
    settings: dict[str, Any] = field(default_factory=dict)


_ocr_runtime: ContextVar[OcrRuntimeConfig | None] = ContextVar("ocr_runtime_config", default=None)


def get_ocr_runtime() -> OcrRuntimeConfig | None:
    """返回当前解析上下文的 OCR 覆盖配置；未设置时调用方回落到环境配置。"""
    return _ocr_runtime.get()


@contextmanager
def use_ocr_runtime(config: OcrRuntimeConfig | None) -> Iterator[None]:
    """在解析调用期间设置 OCR 覆盖配置，退出后恢复原值。"""
    token = _ocr_runtime.set(config)
    try:
        yield
    finally:
        _ocr_runtime.reset(token)
