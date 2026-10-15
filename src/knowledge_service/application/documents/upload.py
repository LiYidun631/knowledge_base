from collections.abc import AsyncIterable


class UploadLimitExceeded(ValueError):
    """上传内容超过服务端限制。"""


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
