"""from-url 下载安全校验：拒绝内网目标与重定向绕过。"""

import asyncio

import httpx
import pytest

from knowledge_service.application.documents import upload

# upload.httpx 就是 httpx 模块本身，先在打补丁前留存真实客户端。
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_client(handler, monkeypatch) -> None:
    def factory(**kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(upload.httpx, "AsyncClient", factory)


def test_download_rejects_non_public_targets() -> None:
    async def scenario() -> None:
        for url in (
            "file:///etc/passwd",
            "http://127.0.0.1/internal",
            "http://localhost/internal",
            "http://10.0.0.5/internal",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/internal",
        ):
            with pytest.raises(upload.SourceDownloadError):
                await upload.download_limited_source(url, max_bytes=1024, timeout=1)

    asyncio.run(scenario())


def test_download_blocks_redirect_to_private_address(monkeypatch) -> None:
    _mock_client(
        lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/secret"}),
        monkeypatch,
    )

    async def scenario() -> None:
        with pytest.raises(upload.SourceDownloadError):
            await upload.download_limited_source(
                "http://93.184.216.34/file", max_bytes=1024, timeout=1
            )

    asyncio.run(scenario())


def test_download_allows_public_redirect_and_returns_bytes(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "93.184.216.34":
            return httpx.Response(302, headers={"location": "http://8.8.8.8/real-file"})
        return httpx.Response(200, content=b"file-bytes")

    _mock_client(handler, monkeypatch)

    async def scenario() -> None:
        content = await upload.download_limited_source(
            "http://93.184.216.34/file", max_bytes=1024, timeout=1
        )
        assert content == b"file-bytes"

    asyncio.run(scenario())


def test_download_rejects_redirect_loop(monkeypatch) -> None:
    _mock_client(
        lambda request: httpx.Response(302, headers={"location": "/again"}),
        monkeypatch,
    )

    async def scenario() -> None:
        with pytest.raises(upload.SourceDownloadError, match="重定向次数过多"):
            await upload.download_limited_source(
                "http://93.184.216.34/file", max_bytes=1024, timeout=1
            )

    asyncio.run(scenario())


def test_download_rejects_oversized_and_empty_content(monkeypatch) -> None:
    async def scenario() -> None:
        _mock_client(lambda request: httpx.Response(200, content=b"x" * 2048), monkeypatch)
        with pytest.raises(upload.UploadLimitExceeded):
            await upload.download_limited_source(
                "http://93.184.216.34/file", max_bytes=1024, timeout=1
            )

        _mock_client(lambda request: httpx.Response(200, content=b""), monkeypatch)
        with pytest.raises(upload.SourceDownloadError, match="空内容"):
            await upload.download_limited_source(
                "http://93.184.216.34/file", max_bytes=1024, timeout=1
            )

    asyncio.run(scenario())
