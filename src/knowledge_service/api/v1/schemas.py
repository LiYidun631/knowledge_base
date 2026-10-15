from datetime import datetime
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field


class KnowledgeBaseCreate(BaseModel):
    """新建知识库的请求参数。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, max_length=160, description="知识库名称，不能重复。"
    )
    description: str | None = Field(
        default=None, max_length=1000, description="知识库用途说明，可不填写。"
    )
    embedding_model_id: UUID | None = Field(
        default=None, description="Embedding 模型档案 ID；不填写时使用服务默认模型。"
    )
    rerank_model_id: UUID | None = Field(
        default=None,
        description="Rerank 模型档案 ID；显式传 null 可关闭重排，不填写时使用服务默认配置。",
    )
class KnowledgeBaseUpdate(BaseModel):
    """知识库创建后仅允许修改名称和描述。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None, min_length=1, max_length=160, description="新的知识库名称。"
    )
    description: str | None = Field(
        default=None, max_length=1000, description="新的用途说明；传 null 可清空。"
    )
class KnowledgeBaseRead(BaseModel):
    """知识库信息及其模型绑定状态。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="知识库唯一标识。")
    name: str = Field(description="知识库名称。")
    description: str | None = Field(description="知识库用途说明。")
    embedding_model_id: UUID = Field(description="绑定的 Embedding 模型档案 ID。")
    rerank_model_id: UUID | None = Field(description="绑定的 Rerank 模型档案 ID；为空表示未启用。")
    models_locked_at: datetime = Field(description="模型绑定锁定时间。")
    status: str = Field(description="知识库状态，例如 active 或 archived。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最后更新时间。")


class ModelProfileRead(BaseModel):
    """已登记模型档案；密钥内容不会通过接口返回。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="模型档案唯一标识。")
    name: str = Field(description="模型档案名称。")
    kind: str = Field(description="模型类型，例如 embedding 或 rerank。")
    provider: str = Field(description="模型服务提供方。")
    model_name: str = Field(description="服务商侧的模型名称或 Endpoint ID。")
    dimensions: int | None = Field(description="Embedding 向量维度；Rerank 模型通常为空。")
    is_default: bool = Field(description="是否为当前类型的默认模型。")


class EmbeddingProfileCreate(BaseModel):
    """从预置模板登记手动模型；只提交密钥引用，不收集明文密钥。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120, description="模型档案名称，需在目录中唯一。")
    preset_name: str = Field(description="模型预置模板名称，可通过查询模型模板接口获取。")
    base_url: AnyHttpUrl | None = Field(
        default=None, description="模型服务 API 根地址；不填写时使用模板默认地址。"
    )
    model_name: str | None = Field(
        default=None, min_length=1, max_length=200, description="实际模型名称或平台 Endpoint ID。"
    )
    dimensions: int | None = Field(
        default=None, ge=1, le=32768, description="Embedding 向量维度；不填写时使用模板默认值。"
    )
    secret_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=".env 中模型 API Key 配置的引用名，不是密钥明文。",
    )


class DocumentRead(BaseModel):
    """逻辑文档信息及其最新版本状态。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="逻辑文档唯一标识。")
    knowledge_base_id: UUID = Field(description="所属知识库 ID。")
    document_key: str = Field(description="调用方提供的稳定文档标识。")
    filename: str = Field(description="最新上传版本的文件名。")
    latest_version_number: int = Field(description="当前最新版本号。")
    status: str = Field(description="最新版本的处理状态。")
    error_message: str | None = Field(description="最新版本的错误摘要；成功时为空。")
    created_at: datetime = Field(description="首次创建时间。")
    updated_at: datetime = Field(description="最后更新时间。")


class IngestionJobRead(BaseModel):
    """离线入库任务的当前状态与安全错误摘要。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="离线入库任务唯一标识。")
    document_id: UUID = Field(description="逻辑文档 ID。")
    document_version_id: UUID = Field(description="本次处理的文档版本 ID。")
    status: str = Field(description="任务状态，例如 queued、running、completed 或 failed。")
    stage: str | None = Field(
        description="当前处理阶段：load、parse、chunk、embed、index 或 complete。"
    )
    attempts: int = Field(description="已执行次数。")
    error_details: dict[str, object] = Field(description="经过脱敏的错误摘要。")
    started_at: datetime | None = Field(description="任务开始时间。")
    finished_at: datetime | None = Field(description="任务结束时间。")
    created_at: datetime = Field(description="任务创建时间。")
    updated_at: datetime = Field(description="任务最后更新时间。")


class DocumentVersionRead(BaseModel):
    """对外展示版本与入库状态，不暴露内部对象存储键。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="文档版本唯一标识。")
    document_id: UUID = Field(description="所属逻辑文档 ID。")
    version_number: int = Field(description="版本序号，从 1 开始递增。")
    filename: str = Field(description="该版本的文件名。")
    media_type: str | None = Field(description="文件 MIME 类型。")
    size_bytes: int = Field(description="文件大小，单位为字节。")
    content_sha256: str = Field(description="文件内容的 SHA-256 摘要。")
    status: str = Field(description="该版本的离线入库状态。")
    is_current: bool = Field(description="是否为当前可检索版本。")
    error_message: str | None = Field(description="该版本的错误摘要；成功时为空。")
    created_at: datetime = Field(description="版本创建时间。")
    updated_at: datetime = Field(description="版本最后更新时间。")
