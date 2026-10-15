from typing import Protocol


class ObjectStorage(Protocol):
    """原始文件存储端口，供本地目录和 RustFS 共用。"""

    async def put(self, key: str, content: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...
