import asyncio
import socket
from collections.abc import AsyncIterable
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import httpx


class UploadLimitExceeded(ValueError):
    """上传内容超过服务端限制。"""


class SourceDownloadError(ValueError):
    """后台文件链接无法在限定大小和时间内读取。"""


_MAX_REDIRECTS = 5


def _validate_source_url(url: str) -> None:
    """只允许 http/https 且解析结果全部为公网地址，避免链接入库探测内网。"""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SourceDownloadError("文件链接必须是 http/https 地址")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SourceDownloadError("文件链接域名无法解析") from exc
    for info in infos:
        address = ip_address(info[4][0].split("%", 1)[0])
        if not address.is_global:
            raise SourceDownloadError("文件链接指向非公网地址，已拒绝下载")


async def _ensure_public_url(url: str) -> None:
    # DNS 解析是阻塞调用，放到线程池里执行，避免卡住事件循环。
    await asyncio.to_thread(_validate_source_url, url)


def normalize_filename(filename: str) -> str:
    """只保留文件名部分，避免客户端路径进入存储键或数据库。"""
    normalized = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not normalized or normalized in {".", ".."} or "\x00" in normalized:
        raise ValueError("Invalid filename")
    if len(normalized) > 500:
        raise ValueError("Filename is too long")
    return normalized


async def read_limited_upload(chunks: AsyncIterable[bytes], max_bytes: int) -> bytes:
    """按块读取请求体，超限即停止，避免无界读取上传内容。"""
    content = bytearray()
    async for chunk in chunks:
        if len(content) + len(chunk) > max_bytes:
            raise UploadLimitExceeded
        content.extend(chunk)
    return bytes(content)


async def download_limited_source(url: str, *, max_bytes: int, timeout: float) -> bytes:
    """下载后台文件链接，限制响应大小并逐跳校验公网地址，避免 SSRF。"""
    try:
        # 关闭自动跟随重定向，改为逐跳校验目标地址后再请求。
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            current = url
            for _ in range(_MAX_REDIRECTS + 1):
                await _ensure_public_url(current)
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise SourceDownloadError("文件链接重定向缺少目标地址")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if content_length is not None and int(content_length) > max_bytes:
                        raise UploadLimitExceeded
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > max_bytes:
                            raise UploadLimitExceeded
                        content.extend(chunk)
                    if not content:
                        raise SourceDownloadError("文件链接返回空内容")
                    return bytes(content)
            raise SourceDownloadError("文件链接重定向次数过多")
    except (UploadLimitExceeded, SourceDownloadError):
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceDownloadError("文件链接无法读取") from exc
