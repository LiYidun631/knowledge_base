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
    # 图片资源对调用方可访问的服务基址；生产环境应填写公网域名或网关地址。
    public_base_url: str = "http://127.0.0.1:8000"
    log_level: str = "INFO"
    # 管理员凭据仅放部署配置；公司凭据由管理员签发，数据库仅存哈希。
    auth_admin_token: SecretStr = SecretStr("")
    # 管理页写入的模型 API Key 用 Fernet 加密后入库；此密钥仅放部署配置，丢失则库内密钥无法解密。
    credential_encryption_key: SecretStr = SecretStr("")
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
    # 旧版 DOC/PPT 等二进制 Office 文件通过 LibreOffice 转换后再解析。
    office_converter_path: str = ""
    office_converter_timeout_seconds: float = Field(default=120, gt=0)
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
    # 在线混合检索参数：候选数控制召回范围，RRF 权重控制向量与 BM25 的排序贡献。
    search_candidate_k: int = Field(default=40, ge=1, le=500)
    search_version_filter_batch_size: int = Field(default=500, ge=1, le=500)
    search_max_top_k: int = Field(default=20, ge=1, le=100)
    search_rrf_k: int = Field(default=60, ge=1)
    search_vector_weight: float = Field(default=0.7, gt=0)
    search_bm25_weight: float = Field(default=0.3, gt=0)
    search_max_query_terms: int = Field(default=256, ge=1, le=2048)
    bm25_k1: float = Field(default=1.5, gt=0)
    bm25_b: float = Field(default=0.75, ge=0, le=1)
    rerank_timeout_seconds: float = Field(default=30, gt=0)
    # 在线问答生成模型；默认使用百炼兼容接口，密钥仍从 MODEL_API_KEYS 读取。
    chat_provider: str = "dashscope"
    chat_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    chat_path: str = "/chat/completions"
    chat_model_name: str = "qwen-plus"
    chat_secret_ref: str = "dashscope"
    chat_timeout_seconds: float = Field(default=60, gt=0)
    chat_temperature: float = Field(default=0.2, ge=0, le=2)
    # 问答复核：成功问答的最佳 rerank 分数低于该值时自动进入待补资料列表。
    qa_badcase_rerank_threshold: float = Field(default=0.6, gt=0, le=1)
    # FAQ 前置匹配：query 与 FAQ 问题的余弦相似度达到该值时原样直出，不走完整检索。
    faq_match_threshold: float = Field(default=0.85, gt=0, le=1)
    # FAQ 自动生成：入库完成后按文档提炼的问答对数量；文档超过大字数量阈值时生成更多。
    faq_generation_count: int = Field(default=3, ge=1, le=20)
    faq_generation_large_chars: int = Field(default=10000, ge=100)
    faq_generation_large_count: int = Field(default=5, ge=1, le=20)
    # FAQ 生成时送入模型的文档上下文字符上限，控制生成成本。
    faq_generation_context_chars: int = Field(default=15000, ge=500, le=100000)
    # 客服品牌或产品名称，会替换提示词文件中的 {{brand_name}} 占位符。
    chat_brand_name: str = "知识库产品"
    # 生成阶段的系统提示词文件；提示词正文单独维护，便于阅读和修改。
    chat_system_prompt_file: Path = Path("prompts/knowledge_answer_system.txt")
    # 在线检索命中父块后，最多向前后扩展的父块数量和上下文字符数。
    search_context_before: int = Field(default=1, ge=0, le=5)
    search_context_after: int = Field(default=1, ge=0, le=5)
    search_max_context_chars: int = Field(default=12000, ge=100, le=100000)
    max_upload_bytes: int = 50 * 1024 * 1024
    # 后台只传文件链接时，知识库服务下载原文件的最长等待时间。
    source_download_timeout_seconds: float = Field(default=120, gt=0)
    allowed_document_extensions: list[str] = Field(
        default_factory=lambda: [
            ".txt",
            ".md",
            ".pdf",
            ".doc",
            ".docx",
            ".docm",
            ".rtf",
            ".xls",
            ".xlsx",
            ".xlsm",
            ".ppt",
            ".pptx",
            ".pptm",
            ".odt",
            ".ods",
            ".odp",
            ".epub",
            ".html",
            ".htm",
            ".xml",
            ".csv",
            ".json",
            ".log",
            ".bmp",
            ".gif",
            ".jpeg",
            ".jpg",
            ".png",
            ".tif",
            ".tiff",
            ".webp",
        ]
    )
    # OCR 使用支持图片输入的 OpenAI 兼容视觉模型；默认接入百炼通义千问视觉模型。
    ocr_secret_ref: str = "dashscope"
    ocr_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ocr_chat_path: str = "/chat/completions"
    ocr_model_name: str = "qwen-vl-plus"
    ocr_timeout_seconds: float = Field(default=180, gt=0)
    ocr_render_dpi: int = Field(default=160, ge=72, le=300)
    # PDF 扫描页使用小批量并发，避免逐页等待且降低视觉服务限流风险。
    ocr_page_concurrency: int = Field(default=3, ge=1, le=8)
    # 是否启用 PDF 版面分析；启用后仍由下方模式决定分析全部页面还是仅扫描页。
    ocr_layout_analysis_enabled: bool = True
    # auto 遵循 WeKnora 的本地优先策略；all 适合确实需要视觉重排的复杂 PDF。
    ocr_layout_analysis_mode: Literal["auto", "all"] = "auto"
    # Office 文档中的嵌入图片是否提取并交给视觉模型生成可检索说明。
    ocr_embedded_images_enabled: bool = True
    # Office 图片通常包含图中文字和结构关系，允许比普通请求更充分的识别时间。
    ocr_embedded_timeout_seconds: float = Field(default=60, gt=0)
    # 同一份 Office 文档中图片按小批量并发识别，避免逐张串行等待。
    ocr_embedded_concurrency: int = Field(default=3, ge=1, le=8)
    # 小图标、装饰线和低分辨率图片只保存原图，不调用视觉模型。
    ocr_embedded_image_min_bytes: int = Field(default=512, ge=0)
    ocr_embedded_image_min_width: int = Field(default=64, ge=1)
    ocr_embedded_image_min_height: int = Field(default=48, ge=1)
    model_config = SettingsConfigDict(
        env_prefix="KNOWLEDGE_SERVICE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    @model_validator(mode="after")
    def validate_auth_credentials(self) -> "Settings":
        admin_token = self.auth_admin_token.get_secret_value().strip()
        if admin_token and (
            not admin_token.isascii() or any(char.isspace() for char in admin_token)
        ):
            raise ValueError("访问凭据必须是不含空白的 ASCII 字符串")
        self.auth_admin_token = SecretStr(admin_token)
        encryption_key = self.credential_encryption_key.get_secret_value().strip()
        if encryption_key:
            try:
                from cryptography.fernet import Fernet
            except ImportError as exc:  # pragma: no cover - 依赖固定存在
                raise ValueError("缺少 cryptography 依赖，无法校验 API Key 加密密钥") from exc
            try:
                Fernet(encryption_key.encode("ascii"))
            except (ValueError, UnicodeEncodeError) as exc:
                raise ValueError(
                    "KNOWLEDGE_SERVICE_CREDENTIAL_ENCRYPTION_KEY 必须是 "
                    "Fernet.generate_key() 生成的 urlsafe base64 密钥"
                ) from exc
            self.credential_encryption_key = SecretStr(encryption_key)
        return self

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
        if self.search_vector_weight + self.search_bm25_weight <= 0:
            raise ValueError("向量检索和 BM25 检索权重不能同时为 0")
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
