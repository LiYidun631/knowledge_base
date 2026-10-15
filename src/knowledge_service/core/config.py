import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


# 参数在项目根目录 .env 中维护；此处仅定义类型、校验规则与缺省值。
class Settings(BaseSettings):
    name: str = "knowledge-service"
    env: str = "local"
    debug: bool = False
    # API 监听地址和端口；修改 .env 后需要重启 API 服务才能生效。
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    # 独立开发时可填写此连接；嵌入 test_agent 后留空并自动复用聊天数据库配置。
    database_url: str | None = None
    # 默认将原始文件存到本地目录；服务器可按需切换到 RustFS/S3。
    object_storage_backend: Literal["local", "s3"] = "local"
    object_storage_endpoint_url: str = "http://localhost:9000"
    object_storage_access_key: SecretStr = Field(default=SecretStr("knowledge-service"))
    object_storage_secret_key: SecretStr = Field(default=SecretStr("knowledge-service-secret"))
    object_storage_bucket: str = "knowledge-files"
    object_storage_region: str = "us-east-1"
    storage_root: Path = Path("data/knowledge_files")
    # 默认 Embedding 档案名与默认 Rerank 一起从 .env 读取；模型档案由 catalog CLI 初始化。
    default_embedding_profile_name: str = "aliyun-dashscope-embedding-v4"
    # 默认 Rerank 也使用百炼，模型档案仍只保存密钥引用。
    default_rerank_profile_name: str = "aliyun-dashscope-qwen3.7-text-rerank"
    default_rerank_provider: str = "dashscope"
    default_rerank_model_name: str = "qwen3.7-text-rerank"
    default_rerank_base_url: str = "https://dashscope.aliyuncs.com/compatible-api/v1"
    default_rerank_path: str = "/reranks"
    default_rerank_secret_ref: str = "dashscope"
    # 手动模型与凭据也可统一写在 .env，不必另外维护 catalog JSON 文件。
    additional_model_profiles: list[dict[str, object]] = Field(default_factory=list)
    model_api_keys: dict[str, SecretStr] = Field(default_factory=dict)
    # 可扩展的 OpenAI 兼容 Embedding 预设；每个档案可覆盖模型名、地址和密钥引用。
    embedding_model_presets: list[dict[str, object]] = Field(
        default_factory=lambda: [
            {
                "name": "openai-text-embedding-3-small",
                "provider": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "model_name": "text-embedding-3-small",
                "dimensions": 1536,
            },
            {
                "name": "aliyun-dashscope-embedding-v4",
                "secret_ref": "dashscope",
                "provider": "dashscope",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model_name": "text-embedding-v4",
                "dimensions": 1024,
                "embedding_path": "/embeddings",
                "batch_size": 10,
            },
            {
                "name": "zhipu-bigmodel-embedding-3",
                "secret_ref": "bigmodel",
                "provider": "bigmodel",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model_name": "embedding-3",
                "dimensions": 2048,
                "embedding_path": "/embeddings",
                "batch_size": 16,
            },
            {
                "name": "volcengine-embedding",
                "secret_ref": "volcengine",
                "requires_model_name": True,
                "provider": "volcengine",
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "model_name": "YOUR_VOLCENGINE_EMBEDDING_ENDPOINT_ID",
                "dimensions": 2048,
                "embedding_path": "/embeddings/multimodal",
                "batch_size": 1,
                "request_format": "volcengine_multimodal",
            },
            {
                "name": "baidu-qianfan-embedding-v1",
                "secret_ref": "qianfan",
                "provider": "qianfan",
                "base_url": "https://qianfan.baidubce.com/v2",
                "model_name": "Embedding-V1",
                "dimensions": 384,
                "embedding_path": "/embeddings",
                "batch_size": 16,
            },
            {
                "name": "deepseek-chat-reserved",
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "model_name": "deepseek-chat",
                "secret_ref": "deepseek",
                "supports_embedding": False,
                "note": "仅预留对话配置，本服务暂不实现生成；不能作为建库 Embedding",
            },
        ]
    )
    # Chroma 数据持久化在本地目录，不依赖独立向量数据库服务。
    chroma_persist_directory: Path = Path("data/knowledge_chroma")
    # 入库队列与应用共进程运行；Outbox 和任务状态仍由 MySQL 持久化。
    recovery_batch_size: int = Field(default=100, ge=1)
    embedding_timeout_seconds: float = 60.0
    embedding_batch_size: int = Field(default=64, ge=1)
    parent_chunk_chars: int = Field(default=1800, ge=1)
    child_chunk_chars: int = Field(default=600, ge=1)
    child_chunk_overlap_chars: int = Field(default=80, ge=0)
    max_ingestion_attempts: int = Field(default=3, ge=1)
    ingestion_retry_base_seconds: int = Field(default=15, ge=1)
    stale_job_timeout_seconds: int = Field(default=1800, ge=1)
    ingestion_poll_seconds: float = Field(default=2.0, gt=0)
    max_upload_bytes: int = 50 * 1024 * 1024
    allowed_document_extensions: list[str] = Field(
        default_factory=lambda: [".txt", ".md", ".pdf", ".docx", ".html", ".htm", ".csv"]
    )
    # 扫描 PDF 通过百度千帆 OpenAI 兼容视觉模型接口识别，密钥复用 qianfan 模型密钥。
    ocr_base_url: str = "https://qianfan.baidubce.com/v2"
    ocr_chat_path: str = "/chat/completions"
    ocr_model_name: str = ""
    ocr_timeout_seconds: float = Field(default=180, gt=0)
    ocr_render_dpi: int = Field(default=160, ge=72, le=300)
    model_config = SettingsConfigDict(
        env_prefix="KNOWLEDGE_SERVICE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("chroma_persist_directory", "storage_root", mode="before")
    @classmethod
    def resolve_project_path(cls, value: str | Path) -> Path:
        """把相对存储路径固定解析到宿主项目根目录，避免启动目录影响数据位置。"""
        path = Path(value)
        if path.is_absolute():
            return path
        try:
            from app.core.config import PROJECT_ROOT
        except ImportError:
            return (Path.cwd() / path).resolve()
        return (PROJECT_ROOT / path).resolve()

    @model_validator(mode="after")
    def validate_chunk_limits(self) -> "Settings":
        """防止分块参数导致文本丢失或循环无法推进。"""
        if self.child_chunk_overlap_chars >= self.child_chunk_chars:
            raise ValueError("子块重叠长度必须小于子块长度")
        if self.parent_chunk_chars < self.child_chunk_chars:
            raise ValueError("父块长度不能小于子块长度")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_database_url() -> str:
    """优先使用模块专属 DSN；嵌入宿主时复用 test_agent 的 MySQL 配置。"""
    settings = get_settings()
    if settings.database_url:
        return settings.database_url
    try:
        from app.core.config import settings as host_settings
    except ImportError as exc:
        raise RuntimeError(
            "请配置 KNOWLEDGE_SERVICE_DATABASE_URL，或在 test_agent 中启用聊天数据库"
        ) from exc
    if not host_settings.chat_db_enabled:
        raise RuntimeError("知识库模块需要启用 test_agent 的 MySQL 聊天数据库")
    return URL.create(
        "mysql+asyncmy",
        username=host_settings.chat_db_user,
        password=host_settings.chat_db_password.get_secret_value(),
        host=host_settings.chat_db_host,
        port=host_settings.chat_db_port,
        database=host_settings.chat_db_name,
        query={"charset": "utf8mb4"},
    ).render_as_string(hide_password=False)


def resolve_secret(secret_ref: str) -> str | None:
    """优先读取 .env 配置中的密钥，兼容已有部署环境变量引用。"""
    settings = get_settings()
    if secret_ref in settings.model_api_keys:
        return settings.model_api_keys[secret_ref].get_secret_value() or None
    return os.getenv(secret_ref)
