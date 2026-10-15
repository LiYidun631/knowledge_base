import asyncio

import pytest

from knowledge_service.application.documents.upload import (
    UploadLimitExceeded,
    normalize_filename,
    read_limited_upload,
)
from knowledge_service.infrastructure.object_storage.local import LocalObjectStorage


async def byte_stream(*parts: bytes):
    for part in parts:
        yield part


def test_normalize_filename_removes_client_path() -> None:
    # Windows 和 Linux 客户端都不能把本地路径写入服务端文件名。
    assert normalize_filename(r"C:\\private\\健康指南.pdf") == "健康指南.pdf"
    assert normalize_filename("../../manual.txt") == "manual.txt"


def test_normalize_filename_rejects_empty_name() -> None:
    with pytest.raises(ValueError):
        normalize_filename("../")


def test_read_limited_upload_collects_chunks() -> None:
    content = asyncio.run(read_limited_upload(byte_stream(b"abc", b"def"), 6))
    assert content == b"abcdef"


def test_read_limited_upload_stops_when_limit_is_exceeded() -> None:
    with pytest.raises(UploadLimitExceeded):
        asyncio.run(read_limited_upload(byte_stream(b"abc", b"def"), 5))


def test_local_object_storage_round_trip_and_rejects_traversal(tmp_path) -> None:
    storage = LocalObjectStorage(tmp_path / "objects")

    async def exercise_storage() -> None:
        await storage.put("project/kb/file.txt", b"content")
        assert await storage.get("project/kb/file.txt") == b"content"
        await storage.delete("project/kb/file.txt")
        with pytest.raises(FileNotFoundError):
            await storage.get("project/kb/file.txt")
        with pytest.raises(ValueError):
            await storage.put("../outside.txt", b"unsafe")

    asyncio.run(exercise_storage())
