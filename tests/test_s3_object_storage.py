import asyncio
from io import BytesIO

import pytest
from botocore.exceptions import ClientError

from knowledge_service.infrastructure.object_storage.s3 import S3ObjectStorage


class FakeS3Client:
    """模拟最小 S3 接口，验证对象存储适配器的生命周期。"""

    def __init__(self) -> None:
        self.bucket_exists = False
        self.objects: dict[str, bytes] = {}

    def head_bucket(self, **kwargs):
        if not self.bucket_exists:
            raise ClientError(
                {
                    "Error": {"Code": "404"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadBucket",
            )

    def create_bucket(self, **kwargs):
        self.bucket_exists = True

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def test_s3_storage_creates_bucket_and_round_trips_objects() -> None:
    """首次写入时创建桶，并可完整写入、读取和删除文件。"""
    client = FakeS3Client()
    storage = S3ObjectStorage("http://localhost:9000", "key", "secret", "files", client=client)

    async def exercise_storage() -> None:
        await storage.put("project/kb/file.txt", b"content")
        assert client.bucket_exists
        assert await storage.get("project/kb/file.txt") == b"content"
        await storage.delete("project/kb/file.txt")
        assert "project/kb/file.txt" not in client.objects

    asyncio.run(exercise_storage())


def test_s3_storage_rejects_unsafe_object_keys() -> None:
    """阻止对象键逃逸到知识库约定目录之外。"""
    storage = S3ObjectStorage(
        "http://localhost:9000", "key", "secret", "files", client=FakeS3Client()
    )
    with pytest.raises(ValueError, match="Invalid object key"):
        asyncio.run(storage.put("../outside.txt", b"unsafe"))
