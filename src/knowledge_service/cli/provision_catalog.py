import argparse
import asyncio
from pathlib import Path
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import select, update

from knowledge_service.core.config import get_settings
from knowledge_service.infrastructure.persistence.models import ModelProfile
from knowledge_service.infrastructure.persistence.session import get_session_factory


class ModelDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["embedding", "rerank"]
    provider: str
    model_name: str
    dimensions: int | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None
    is_enabled: bool = True
    is_default: bool = False

    @model_validator(mode="after")
    def validate_embedding_dimensions(self) -> "ModelDefinition":
        if self.kind == "embedding" and (self.dimensions is None or self.dimensions <= 0):
            raise ValueError("Embedding model profiles require a positive dimensions value")
        if self.kind == "embedding":
            supported = {"openai_compatible", "dashscope", "bigmodel", "volcengine", "qianfan"}
            if self.provider not in supported:
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
                # 未使用模板的手动档案采取单条请求，避免超过平台批量限制。
                self.settings.setdefault("batch_size", 1)
            if self.model_name.startswith("YOUR_"):
                raise ValueError("请填写平台实际部署的 Embedding 模型名或 Endpoint ID")
        return self


class CatalogDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[ModelDefinition] = Field(default_factory=list)


def _ensure_no_secret_values(value: Any, path: str = "settings") -> None:
    secret_terms = {"api_key", "apikey", "password", "passwd", "secret", "token", "credential"}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in secret_terms:
                raise ValueError(f"Do not store secrets in {path}.{key}; use secret_ref")
            _ensure_no_secret_values(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_no_secret_values(item, f"{path}[{index}]")


async def provision_catalog(catalog: CatalogDefinition) -> None:
    keys = [(model.kind, model.name) for model in catalog.models]
    if len(keys) != len(set(keys)):
        raise ValueError("模型目录中包含重复的模型档案")
    for model in catalog.models:
        _ensure_no_secret_values(model.settings, f"models.{model.name}.settings")
    defaults = {
        kind: [model for model in catalog.models if model.kind == kind and model.is_default]
        for kind in ("embedding", "rerank")
    }
    for kind, profiles in defaults.items():
        if len(profiles) > 1:
            raise ValueError(f"模型目录中只能有一个默认 {kind} 模型")

    async with get_session_factory()() as session:
        async with session.begin():
            for kind, profiles in defaults.items():
                if not profiles:
                    continue
                # 按模型类型切换默认标记，保留旧档案及其配置供已有知识库使用。
                await session.execute(
                    update(ModelProfile)
                    .where(
                        ModelProfile.kind == kind,
                        ModelProfile.is_default.is_(True),
                        ModelProfile.name != profiles[0].name,
                    )
                    .values(is_default=False)
                )
                await session.flush()

            for definition in catalog.models:
                existing = await session.scalar(
                    select(ModelProfile).where(
                        ModelProfile.kind == definition.kind,
                        ModelProfile.name == definition.name,
                    )
                )
                values = definition.model_dump()
                if existing is not None:
                    actual = {key: getattr(existing, key) for key in values if key != "is_default"}
                    expected = {key: value for key, value in values.items() if key != "is_default"}
                    if actual != expected:
                        raise ValueError(
                            f"Model profile {definition.kind}/{definition.name} already exists "
                            "with different settings; create a new versioned profile name"
                        )
                    existing.is_default = definition.is_default
                    continue
                session.add(ModelProfile(**values))

def _embedding_definition_from_preset(
    preset: dict[str, Any], *, is_default: bool = False
) -> ModelDefinition:
    """把 Embedding 模板转换成不包含密钥明文的模型档案。"""
    if not preset.get("supports_embedding", True):
        raise ValueError("该平台模板不提供 Embedding，不能用于建库")
    return ModelDefinition(
        name=str(preset["name"]),
        kind="embedding",
        provider=str(preset["provider"]),
        model_name=str(preset["model_name"]),
        dimensions=int(preset["dimensions"]),
        secret_ref=preset.get("secret_ref"),
        is_default=is_default,
        settings={
            "base_url": preset["base_url"],
            **{
                key: preset[key]
                for key in ("embedding_path", "batch_size", "request_format")
                if key in preset
            },
        },
    )


def default_catalog() -> CatalogDefinition:
    """根据环境配置生成默认 Embedding 与 Rerank 模型档案。"""
    settings = get_settings()
    preset = next(
        (
            item
            for item in settings.embedding_model_presets
            if item.get("name") == settings.default_embedding_profile_name
        ),
        None,
    )
    if preset is None:
        raise ValueError(
            f"配置中缺少默认 Embedding 模型模板: {settings.default_embedding_profile_name}"
        )
    embedding = _embedding_definition_from_preset(preset, is_default=True)

    reranker = ModelDefinition(
        name=settings.default_rerank_profile_name,
        kind="rerank",
        provider=settings.default_rerank_provider,
        model_name=settings.default_rerank_model_name,
        settings={
            "base_url": settings.default_rerank_base_url,
            "rerank_path": settings.default_rerank_path,
        },
        secret_ref=settings.default_rerank_secret_ref,
        is_default=True,
    )

    return CatalogDefinition(models=[embedding, reranker])


def main() -> None:
    parser = argparse.ArgumentParser(description="登记开发配置指定的默认及备用模型档案。")
    parser.add_argument("config", type=Path, nargs="?", help="模型目录 JSON 配置文件")
    parser.add_argument(
        "--defaults", action="store_true", help="初始化默认 Embedding 与 Rerank 档案"
    )
    parser.add_argument("--env", action="store_true", help="从 .env 初始化默认和手动模型档案")
    args = parser.parse_args()
    if sum([args.defaults, args.env, bool(args.config)]) != 1:
        parser.error("请选择 JSON 配置文件、--defaults 或 --env 中的一种方式")
    catalog = (
        default_catalog()
        if args.defaults or args.env
        else CatalogDefinition.model_validate_json(args.config.read_text(encoding="utf-8"))
    )
    if args.env:
        settings = get_settings()
        for preset in settings.embedding_model_presets:
            if preset.get("enabled", False):
                profile = _embedding_definition_from_preset(preset)
                if not any(
                    item.kind == profile.kind and item.name == profile.name
                    for item in catalog.models
                ):
                    catalog.models.append(profile)
        catalog.models.extend(
            ModelDefinition.model_validate(value) for value in settings.additional_model_profiles
        )
    asyncio.run(provision_catalog(catalog))
    print("Catalog profiles are ready.")


if __name__ == "__main__":
    main()
