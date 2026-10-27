import asyncio
from io import BytesIO

import pytest
from pydantic import ValidationError

import knowledge_service.infrastructure.object_storage.s3 as s3_module
from knowledge_service.core.config import Settings
from knowledge_service.infrastructure.object_storage.s3 import S3ObjectStorage


class FakeS3Client:
    """模拟最小 OSS S3 兼容接口，验证对象存储适配器的读写行为。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def test_s3_storage_round_trips_objects() -> None:
    """直接对预建桶写入、读取和删除文件，不再探测或创建桶。"""
    client = FakeS3Client()
    storage = S3ObjectStorage(
        "https://s3.oss-cn-hangzhou-internal.aliyuncs.com",
        "key",
        "secret",
        "knowledge-files",
        region="cn-hangzhou",
        client=client,
    )

    async def exercise_storage() -> None:
        await storage.put("project/kb/file.txt", b"content")
        assert await storage.get("project/kb/file.txt") == b"content"
        await storage.delete("project/kb/file.txt")
        assert "project/kb/file.txt" not in client.objects

    asyncio.run(exercise_storage())


def test_s3_storage_rejects_unsafe_object_keys() -> None:
    """阻止对象键逃逸到知识库约定目录之外。"""
    storage = S3ObjectStorage(
        "https://s3.oss-cn-hangzhou-internal.aliyuncs.com",
        "key",
        "secret",
        "knowledge-files",
        client=FakeS3Client(),
    )
    with pytest.raises(ValueError, match="Invalid object key"):
        asyncio.run(storage.put("../outside.txt", b"unsafe"))


def test_s3_storage_uses_oss_signature_and_addressing_style(monkeypatch) -> None:
    """OSS 需要 SigV2 + 虚拟主机寻址，参数必须透传到底层客户端。"""
    captured: dict[str, object] = {}

    def fake_client(*args, **kwargs):
        captured.update(kwargs)
        return FakeS3Client()

    monkeypatch.setattr(s3_module.boto3, "client", fake_client)
    S3ObjectStorage(
        "https://s3.oss-cn-hangzhou-internal.aliyuncs.com",
        "key",
        "secret",
        "knowledge-files",
        region="cn-hangzhou",
    )

    config = captured["config"]
    assert config.signature_version == "s3"
    assert config.s3 == {"addressing_style": "virtual"}
    assert captured["region_name"] == "cn-hangzhou"


def _oss_settings(**overrides):
    values = {
        "_env_file": None,
        "object_storage_backend": "s3",
        "object_storage_endpoint_url": "https://s3.oss-cn-hangzhou.aliyuncs.com",
        "object_storage_access_key": "key",
        "object_storage_secret_key": "secret",
        "object_storage_bucket": "knowledge-files",
        "object_storage_region": "cn-hangzhou",
    }
    values.update(overrides)
    return Settings(**values)


def test_s3_backend_requires_region() -> None:
    """地域缺失时启动即失败，避免首次上传才触发端点解析错误。"""
    with pytest.raises(ValidationError, match="OBJECT_STORAGE_REGION"):
        _oss_settings(object_storage_region="")


def test_s3_backend_rejects_endpoint_style_region() -> None:
    """region 误填成 oss-cn-hangzhou 时给出明确提示。"""
    with pytest.raises(ValidationError, match="地域 ID"):
        _oss_settings(object_storage_region="oss-cn-hangzhou")


def test_s3_backend_normalizes_oss_settings() -> None:
    """去掉配置首尾空白和 endpoint 末尾斜杠。"""
    settings = _oss_settings(
        object_storage_endpoint_url=" https://s3.oss-cn-hangzhou-internal.aliyuncs.com/ ",
        object_storage_bucket=" knowledge-files ",
        object_storage_region=" cn-hangzhou ",
    )

    assert (
        settings.object_storage_endpoint_url
        == "https://s3.oss-cn-hangzhou-internal.aliyuncs.com"
    )
    assert settings.object_storage_bucket == "knowledge-files"
    assert settings.object_storage_region == "cn-hangzhou"
