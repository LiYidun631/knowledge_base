from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.object_storage.local import LocalObjectStorage
from knowledge_service.infrastructure.object_storage.s3 import S3ObjectStorage
from knowledge_service.ports.object_store import ObjectStorage


def get_object_storage(settings: Settings | None = None) -> ObjectStorage:
    """按统一配置选择本地目录或阿里云 OSS 适配器。"""
    settings = settings or get_settings()
    if settings.object_storage_backend == "local":
        return LocalObjectStorage(settings.storage_root)
    return S3ObjectStorage(
        endpoint_url=settings.object_storage_endpoint_url,
        access_key=settings.object_storage_access_key.get_secret_value(),
        secret_key=settings.object_storage_secret_key.get_secret_value(),
        bucket=settings.object_storage_bucket,
        region=settings.object_storage_region,
    )
