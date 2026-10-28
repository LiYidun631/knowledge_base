"""原文件在线预览：把浏览器不能直接渲染的格式转换为可内嵌内容。"""

from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image

from knowledge_service.application.documents.parsing import (
    DocumentParseError,
    convert_office_document,
)

# Office 与开放文档格式统一转 PDF；前端无法渲染时也回落到这里。
_OFFICE_EXTENSIONS = frozenset(
    {
        ".doc",
        ".docx",
        ".docm",
        ".rtf",
        ".ppt",
        ".pptx",
        ".pptm",
        ".xls",
        ".xlsx",
        ".xlsm",
        ".ods",
        ".odt",
        ".odp",
    }
)
_TIFF_EXTENSIONS = frozenset({".tif", ".tiff"})

# 转换结果按版本 ID 缓存；版本内容不可变，所以无需失效逻辑，交给系统临时目录清理。
_CACHE_DIR = Path(tempfile.gettempdir()) / "knowledge-service-previews"


class PreviewFormatUnsupported(Exception):
    """该格式没有服务端预览转换。"""


class PreviewConverterUnavailable(Exception):
    """缺少 LibreOffice 等转换程序。"""


class PreviewConversionFailed(Exception):
    """文件损坏或转换失败。"""


@dataclass(frozen=True)
class PreviewPayload:
    content: bytes
    media_type: str


def build_preview(version_id: UUID, filename: str, content: bytes) -> PreviewPayload:
    """按扩展名生成可直接嵌入浏览器的预览内容。"""
    extension = Path(filename).suffix.lower()
    if extension in _TIFF_EXTENSIONS:
        data = _cached(version_id, ".png", lambda: _tiff_to_png(content))
        return PreviewPayload(data, "image/png")
    if extension in _OFFICE_EXTENSIONS:
        data = _cached(version_id, ".pdf", lambda: _office_to_pdf(filename, content))
        return PreviewPayload(data, "application/pdf")
    raise PreviewFormatUnsupported("该格式无需服务端转换")


def _cached(version_id: UUID, suffix: str, producer) -> bytes:
    """优先复用已转换文件；写入失败时仍返回本次结果。"""
    path = _CACHE_DIR / f"{version_id}{suffix}"
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path.read_bytes()
    except OSError:
        pass
    data = producer()
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(data)
        os.replace(temp_path, path)
    except OSError:
        temp_path.unlink(missing_ok=True)
    return data


def _office_to_pdf(filename: str, content: bytes) -> bytes:
    try:
        return convert_office_document(filename, content, ".pdf")
    except DocumentParseError as exc:
        message = str(exc)
        if message.startswith("OFFICE_CONVERTER_NOT_CONFIGURED"):
            raise PreviewConverterUnavailable(
                "当前服务端未安装 LibreOffice，暂不支持该格式在线预览"
            ) from exc
        if message.startswith("OFFICE_CONVERSION_TIMEOUT"):
            raise PreviewConversionFailed("文件转换超时，请下载后查看") from exc
        raise PreviewConversionFailed("原文件转换失败，请下载后查看") from exc


def _tiff_to_png(content: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.seek(0)
            output = io.BytesIO()
            image.convert("RGB").save(output, format="PNG", optimize=True)
            return output.getvalue()
    except Exception as exc:
        raise PreviewConversionFailed("TIFF 图片转换失败，请下载后查看") from exc
