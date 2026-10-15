"""RustFS/S3 原始文件存储适配器。"""

import asyncio
from pathlib import PurePosixPath
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError


class S3ObjectStorage:
    """通过 S3 兼容接口读写 RustFS 中的原始文件。"""

    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
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
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
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
        # 首次上传时自动创建业务桶，免去单独初始化桶的步骤。
        self._ensure_bucket()
        self.client.put_object(Bucket=self.bucket, Key=key, Body=content)

    def _get(self, key: str) -> bytes:
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def _ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            response = exc.response
            code = response.get("Error", {}).get("Code")
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code not in {"404", "NoSuchBucket", "NotFound"} and status != 404:
                raise
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except ClientError as create_exc:
                create_code = create_exc.response.get("Error", {}).get("Code")
                if create_code not in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
                    raise

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
