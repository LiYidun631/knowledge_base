from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.v1.schemas import (
    EmbeddingProfileCreate,
    KnowledgeBaseCreate,
    KnowledgeBaseRead,
    KnowledgeBaseUpdate,
    ModelProfileRead,
)
from knowledge_service.application.knowledge_bases import service
from knowledge_service.cli.provision_catalog import ModelDefinition
from knowledge_service.core.config import get_settings
from knowledge_service.domain.errors import ConfigurationNotFound, KnowledgeBaseNotFound
from knowledge_service.infrastructure.persistence.models import ModelProfile
from knowledge_service.infrastructure.persistence.session import get_db_session

router = APIRouter(prefix="/knowledge-bases", tags=["知识库管理"])
catalog_router = APIRouter(tags=["模型配置"])


@catalog_router.get(
    "/model-presets",
    summary="查询预置模型模板",
    description="返回可用于登记模型档案的模板，不包含 API 密钥。",
)
async def list_model_presets() -> list[dict[str, object]]:
    """供管理界面展示可用的模型接入方式。"""
    return get_settings().embedding_model_presets


@catalog_router.post(
    "/models",
    response_model=ModelProfileRead,
    status_code=status.HTTP_201_CREATED,
    summary="登记 Embedding 模型",
    description="根据预置模板登记模型；只保存密钥引用，不接收或保存明文密钥。",
)
async def add_embedding_profile(
    payload: EmbeddingProfileCreate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ModelProfileRead:
    """登记手动 Embedding 模型配置。"""
    presets = {preset["name"]: preset for preset in get_settings().embedding_model_presets}
    preset = presets.get(payload.preset_name)
    if preset is None:
        raise HTTPException(status_code=422, detail="Unknown embedding preset")
    if not preset.get("supports_embedding", True):
        raise HTTPException(status_code=422, detail="该模型模板不支持 Embedding")
    if preset.get("requires_model_name") and not payload.model_name:
        raise HTTPException(status_code=422, detail="请填写实际模型名或 Endpoint ID")

    profile = ModelProfile(
        **ModelDefinition(
            name=payload.name,
            kind="embedding",
            provider=str(preset["provider"]),
            model_name=payload.model_name or str(preset["model_name"]),
            dimensions=payload.dimensions or int(preset["dimensions"]),
            settings={
                "base_url": str(payload.base_url or preset["base_url"]).rstrip("/"),
                **{
                    key: preset[key]
                    for key in ("embedding_path", "batch_size", "request_format")
                    if key in preset
                },
            },
            secret_ref=payload.secret_ref or preset.get("secret_ref"),
        ).model_dump()
    )
    session.add(profile)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="A model profile with this name exists"
        ) from exc
    return ModelProfileRead.model_validate(profile)


@catalog_router.get(
    "/models",
    response_model=list[ModelProfileRead],
    summary="查询可用模型",
    description="列出已启用的 Embedding 和 Rerank 模型，可按类型筛选。",
)
async def list_model_profiles(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    kind: Annotated[
        str | None,
        Query(pattern="^(embedding|rerank)$", description="模型类型：embedding 或 rerank。"),
    ] = None,
) -> list[ModelProfile]:
    statement = select(ModelProfile).where(ModelProfile.is_enabled.is_(True))
    if kind:
        statement = statement.where(ModelProfile.kind == kind)
    return list(await session.scalars(statement.order_by(ModelProfile.kind, ModelProfile.name)))


@router.post(
    "",
    response_model=KnowledgeBaseRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建知识库",
    description="创建知识库并绑定 Embedding 和可选 Rerank 模型；向量数据保存在本地 Chroma。",
)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> KnowledgeBaseRead:
    try:
        knowledge_base = await service.create_knowledge_base(
            session,
            **payload.model_dump(exclude_unset=True),
        )
    except ConfigurationNotFound as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="A knowledge base with this name already exists"
        ) from exc
    return KnowledgeBaseRead.model_validate(knowledge_base)


@router.get("", response_model=list[KnowledgeBaseRead], summary="分页查询知识库")
async def list_knowledge_bases(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    offset: Annotated[int, Query(ge=0, description="跳过的记录数。")] = 0,
    limit: Annotated[int, Query(ge=1, le=200, description="每页记录数，最大 200。")] = 50,
) -> list[KnowledgeBaseRead]:
    items = await service.list_knowledge_bases(session, offset=offset, limit=limit)
    return [KnowledgeBaseRead.model_validate(item) for item in items]


@router.get("/{knowledge_base_id}", response_model=KnowledgeBaseRead, summary="查询知识库详情")
async def get_knowledge_base(
    knowledge_base_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> KnowledgeBaseRead:
    try:
        item = await service.get_knowledge_base(session, knowledge_base_id=knowledge_base_id)
    except KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=404, detail="Knowledge base not found") from exc
    return KnowledgeBaseRead.model_validate(item)


@router.patch("/{knowledge_base_id}", response_model=KnowledgeBaseRead, summary="修改知识库")
async def update_knowledge_base(
    knowledge_base_id: UUID,
    payload: KnowledgeBaseUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> KnowledgeBaseRead:
    values = payload.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=422, detail="At least one field must be provided")
    if "name" in values and values["name"] is None:
        raise HTTPException(status_code=422, detail="知识库名称不能为空")
    try:
        item = await service.update_knowledge_base(
            session,
            knowledge_base_id=knowledge_base_id,
            values=values,
        )
    except KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=404, detail="Knowledge base not found") from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="A knowledge base with this name already exists"
        ) from exc
    return KnowledgeBaseRead.model_validate(item)


@router.delete(
    "/{knowledge_base_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="归档知识库",
    description="将知识库归档，保留其文件、向量和入库记录。",
)
async def archive_knowledge_base(
    knowledge_base_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    try:
        await service.archive_knowledge_base(session, knowledge_base_id=knowledge_base_id)
    except KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=404, detail="Knowledge base not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
