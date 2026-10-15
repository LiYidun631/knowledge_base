import asyncio
import os
from pathlib import Path, PurePosixPath
from uuid import uuid4


class LocalObjectStorage:
    """用于本地开发及单机部署的原子文件存储适配器。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _resolve_key(self, key: str) -> Path:
        relative = PurePosixPath(key)
        if (
            not key
            or relative.is_absolute()
            or "\\" in key
            or any(part in {".", ".."} or ":" in part for part in relative.parts)
        ):
            raise ValueError("Invalid object key")
        target = (self.root / Path(*relative.parts)).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Object key escapes storage root") from exc
        return target

    async def put(self, key: str, content: bytes) -> None:
        await asyncio.to_thread(self._put, key, content)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._resolve_key(key).read_bytes)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._resolve_key(key).unlink, missing_ok=True)

    def _put(self, key: str, content: bytes) -> None:
        target = self._resolve_key(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as file:
                file.write(content)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
