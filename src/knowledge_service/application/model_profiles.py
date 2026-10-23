"""模型档案管理：厂商预设、档案增改、默认切换、密钥写入与删除保护。"""

from typing import Any, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, Field, TypeAdapter, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.core.credentials import encrypt_api_key
from knowledge_service.domain.errors import (
    ModelProfileInUse,
    ModelProfileNotFound,
)
from knowledge_service.infrastructure.persistence.models import (
    KnowledgeBase,
    ModelProfile,
    VectorLibrary,
)

# 厂商预设写死在代码中，界面按服务商下拉选择后补填模型名与密钥。
VENDOR_PRESETS: list[dict[str, Any]] = [
    {
        "key": "openai_compatible",
        "display_name": "OpenAI 兼容接口",
        "base_url": "https://api.openai.com/v1",
        "embedding_path": "/embeddings",
        "supports_chat": True,
        "chat_path": "/chat/completions",
        "known_chat_models": ["gpt-4o-mini", "gpt-4o"],
        "supports_embedding": True,
        "note": "适用于 OpenAI 及任何兼容 /v1 的服务商",
        "known_models": ["text-embedding-3-small", "text-embedding-3-large"],
    },
    {
        "key": "dashscope",
        "display_name": "阿里云百炼 DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "embedding_path": "/embeddings",
        "supports_chat": True,
        "chat_path": "/chat/completions",
        "known_chat_models": ["qwen-flash", "qwen-plus", "qwen-max"],
        "supports_embedding": True,
        "note": "支持 embedding-v4、qwen3 系列与 text-rerank",
        "known_models": ["text-embedding-v4", "qwen3.7-text-rerank"],
    },
    {
        "key": "bigmodel",
        "display_name": "智谱 BigModel",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "embedding_path": "/embeddings",
        "supports_chat": True,
        "chat_path": "/chat/completions",
        "known_chat_models": ["glm-4-flash", "glm-4-plus"],
        "supports_embedding": True,
        "note": "支持 embedding-3",
        "known_models": ["embedding-3"],
    },
    {
        "key": "volcengine",
        "display_name": "火山引擎方舟",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "embedding_path": "/embeddings/multimodal",
        "supports_chat": True,
        "chat_path": "/chat/completions",
        "known_chat_models": [],
        "supports_embedding": True,
        "note": "需填写已部署模型的 Endpoint ID，每次只处理一段文本",
        "known_models": [],
    },
    {
        "key": "qianfan",
        "display_name": "百度智能云千帆",
        "base_url": "https://qianfan.baidubce.com/v2",
        "embedding_path": "/embeddings",
        "supports_chat": True,
        "chat_path": "/chat/completions",
        "known_chat_models": ["ernie-4.0-8k"],
        "supports_embedding": True,
        "note": "支持 Embedding-V1",
        "known_models": ["Embedding-V1"],
    },
    {
        "key": "deepseek",
        "display_name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "embedding_path": "/embeddings",
        "supports_chat": True,
        "chat_path": "/chat/completions",
        "known_chat_models": ["deepseek-chat"],
        "supports_embedding": False,
        "note": "暂不提供 Embedding，仅作对话预留",
        "known_models": [],
    },
]

EMBEDDING_PROVIDERS = {"openai_compatible", "dashscope", "bigmodel", "volcengine", "qianfan"}


class ModelProfileSpec(BaseModel):
    """创建与修改模型档案的公共字段和校验规则。"""

    name: str = Field(min_length=1, max_length=120)
    kind: Literal["embedding", "rerank", "chat"]
    provider: str = Field(min_length=1, max_length=80)
    model_name: str = Field(min_length=1, max_length=200)
    dimensions: int | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None
    is_enabled: bool = True

    @model_validator(mode="after")
    def validate_profile(self) -> "ModelProfileSpec":
        if self.kind == "embedding":
            if self.dimensions is None or self.dimensions <= 0:
                raise ValueError("Embedding 模型档案必须填写正整数维度")
            if self.provider not in EMBEDDING_PROVIDERS:
                raise ValueError("当前 Embedding 适配器不支持该模型供应商")
            TypeAdapter(AnyHttpUrl).validate_python(self.settings.get("base_url"))
            if int(self.settings.get("batch_size", 1)) < 1:
                raise ValueError("模型 batch_size 必须为正整数")
            if not str(self.settings.get("embedding_path", "/embeddings")).startswith("/"):
                raise ValueError("embedding_path 必须以 / 开头")
            if self.provider == "volcengine":
                self.settings.setdefault("embedding_path", "/embeddings/multimodal")
                self.settings["batch_size"] = 1
            elif self.provider in {"dashscope", "bigmodel", "qianfan"}:
                # 手动档案采取单条请求，避免超过平台批量限制。
                self.settings.setdefault("batch_size", 1)
            if self.model_name.startswith("YOUR_"):
                raise ValueError("请填写平台实际部署的 Embedding 模型名或 Endpoint ID")
        elif self.kind == "chat":
            if self.dimensions is not None:
                raise ValueError("Chat 模型档案不需要填写向量维度")
            TypeAdapter(AnyHttpUrl).validate_python(self.settings.get("base_url"))
            if not str(self.settings.get("chat_path", "/chat/completions")).startswith("/"):
                raise ValueError("chat_path 必须以 / 开头")
            if self.model_name.startswith("YOUR_"):
                raise ValueError("请填写平台实际的生成模型名")
        else:
            TypeAdapter(AnyHttpUrl).validate_python(self.settings.get("base_url"))
            if not str(self.settings.get("rerank_path", "/reranks")).startswith("/"):
                raise ValueError("rerank_path 必须以 / 开头")
        return self


async def list_model_profiles(
    session: AsyncSession, *, kind: Literal["embedding", "rerank", "chat"] | None = None
) -> list[ModelProfile]:
    """列出模型档案；界面按类型分组展示。"""
    statement = select(ModelProfile).order_by(
        ModelProfile.kind, ModelProfile.is_default.desc(), ModelProfile.created_at
    )
    if kind is not None:
        statement = statement.where(ModelProfile.kind == kind)
    return list(await session.scalars(statement))


async def get_model_profile(session: AsyncSession, *, model_profile_id: UUID) -> ModelProfile:
    profile = await session.get(ModelProfile, model_profile_id)
    if profile is None:
        raise ModelProfileNotFound("Model profile not found")
    return profile


async def create_model_profile(session: AsyncSession, spec: ModelProfileSpec) -> ModelProfile:
    """按校验后的规格创建模型档案；同一类型下名称不能重复。"""
    profile = ModelProfile(**spec.model_dump())
    session.add(profile)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ModelProfileInUse("同名模型档案已存在") from exc
    return profile


# 绑定到知识库后，这些字段的修改会改变向量空间，导致已入库向量无法正确检索。
_VECTOR_CONTRACT_FIELDS = {"provider", "model_name", "dimensions", "settings"}


async def update_model_profile(
    session: AsyncSession, *, model_profile_id: UUID, spec: ModelProfileSpec
) -> ModelProfile:
    """按合并后的完整规格修改档案；类型、默认标记和密钥由专用接口维护。"""
    profile = await get_model_profile(session, model_profile_id=model_profile_id)
    changes = {
        field: value
        for field, value in spec.model_dump().items()
        if getattr(profile, field) != value
    }
    if profile.kind == "embedding" and _VECTOR_CONTRACT_FIELDS & changes.keys():
        bound_library = await session.scalar(
            select(VectorLibrary.id).where(VectorLibrary.embedding_model_id == profile.id)
        )
        bound_base = await session.scalar(
            select(KnowledgeBase.id).where(KnowledgeBase.embedding_model_id == profile.id)
        )
        if bound_library is not None or bound_base is not None:
            raise ModelProfileInUse(
                "模型档案已被知识库使用，不能修改供应商、模型名、维度或调用参数；"
                "请先删除或重建相关库"
            )
    for field, value in spec.model_dump().items():
        setattr(profile, field, value)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ModelProfileInUse("同名模型档案已存在") from exc
    return profile


async def set_default_model_profile(
    session: AsyncSession, *, model_profile_id: UUID
) -> ModelProfile:
    """把档案设为同类默认，同时取消同类其他默认标记。"""
    profile = await get_model_profile(session, model_profile_id=model_profile_id)
    await session.execute(
        update(ModelProfile)
        .where(ModelProfile.kind == profile.kind, ModelProfile.id != profile.id)
        .values(is_default=False)
    )
    profile.is_default = True
    await session.flush()
    return profile


async def clear_default_model_profile(session: AsyncSession, *, model_profile_id: UUID) -> None:
    profile = await get_model_profile(session, model_profile_id=model_profile_id)
    profile.is_default = False
    await session.flush()


async def delete_model_profile(session: AsyncSession, *, model_profile_id: UUID) -> None:
    """删除档案；仍被大库或子库引用的档案返回冲突，避免破坏既有绑定。"""
    profile = await get_model_profile(session, model_profile_id=model_profile_id)
    bound_library = await session.scalar(
        select(VectorLibrary.id).where(
            (VectorLibrary.embedding_model_id == profile.id)
            | (VectorLibrary.rerank_model_id == profile.id)
        )
    )
    bound_base = await session.scalar(
        select(KnowledgeBase.id).where(
            (KnowledgeBase.embedding_model_id == profile.id)
            | (KnowledgeBase.rerank_model_id == profile.id)
        )
    )
    if bound_library is not None or bound_base is not None:
        raise ModelProfileInUse("模型档案仍被知识库引用，请先删除或重建相关库")
    await session.delete(profile)
    await session.flush()


async def set_model_api_key(
    session: AsyncSession, *, model_profile_id: UUID, plaintext: str
) -> None:
    """写入 API Key；仅保存 Fernet 密文，任何读取接口都不回读明文。"""
    profile = await get_model_profile(session, model_profile_id=model_profile_id)
    profile.api_key_cipher = encrypt_api_key(plaintext)
    await session.flush()


async def clear_model_api_key(session: AsyncSession, *, model_profile_id: UUID) -> None:
    profile = await get_model_profile(session, model_profile_id=model_profile_id)
    profile.api_key_cipher = None
    await session.flush()
