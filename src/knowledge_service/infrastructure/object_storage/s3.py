"""阿里云 OSS 对象存储适配器（S3 兼容接口）。"""

import asyncio
from pathlib import PurePosixPath
from typing import Any

import boto3
from botocore.client import Config


class S3ObjectStorage:
    """通过 OSS 的 S3 兼容接口读写原始文件。"""

    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "",
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        # 延迟创建 SDK 客户端，单元测试及导入本模块时不连接外部服务。
        self.client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            # OSS 的 S3 兼容层只支持 SigV2 + 虚拟主机寻址，V4 会触发分块传输报错。
            config=Config(signature_version="s3", s3={"addressing_style": "virtual"}),
        )

    async def put(self, key: str, content: bytes) -> None:
        await asyncio.to_thread(self._put, self._validate_key(key), content)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._get, self._validate_key(key))

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(
            self.client.delete_object,
            Bucket=self.bucket,
            Key=self._validate_key(key),
        )

    def _put(self, key: str, content: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=content)

    def _get(self, key: str) -> bytes:
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        try:
            return body.read()
        finally:
            body.close()

    @staticmethod
    def _validate_key(key: str) -> str:
        relative = PurePosixPath(key)
        if (
            not key
            or relative.is_absolute()
            or "\\" in key
            or any(part in {".", ".."} or ":" in part for part in relative.parts)
        ):
            raise ValueError("Invalid object key")
        return key
