from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.v1.schemas import (
    KnowledgeBaseCreate,
    KnowledgeBaseRead,
    KnowledgeBaseUpdate,
)
from knowledge_service.application.knowledge_bases import service
from knowledge_service.domain.errors import ConfigurationNotFound, KnowledgeBaseNotFound
from knowledge_service.infrastructure.persistence.session import get_db_session

router = APIRouter(prefix="/knowledge-bases", tags=["知识库管理"])


@router.post(
    "",
    response_model=KnowledgeBaseRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建知识库",
    deprecated=True,
    description=(
        "兼容旧版单层知识库接口。新项目请先创建公司级向量大库，"
        "再通过 /companies/{company_id}/vector-library/knowledge-bases 创建子库；"
        "该兼容接口不会建立公司和大库归属，不建议用于新数据。"
    ),
)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
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


@router.get("", response_model=list[KnowledgeBaseRead], summary="按公司分页查询知识库")
async def list_knowledge_bases(
    company_id: Annotated[UUID, Query(description="公司租户 ID；只返回该公司的知识库。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    offset: Annotated[int, Query(ge=0, description="跳过的记录数。")] = 0,
    limit: Annotated[int, Query(ge=1, le=200, description="每页记录数，最大 200。")] = 50,
    include_archived: Annotated[bool, Query(description="为 true 时同时返回已归档知识库。")] = False,
) -> list[KnowledgeBaseRead]:
    items = await service.list_knowledge_bases(
        session,
        company_id=company_id,
        offset=offset,
        limit=limit,
        include_archived=include_archived,
    )
    return [KnowledgeBaseRead.model_validate(item) for item in items]


@router.get("/{knowledge_base_id}", response_model=KnowledgeBaseRead, summary="查询知识库详情")
async def get_knowledge_base(
    knowledge_base_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
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
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
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
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    try:
        await service.archive_knowledge_base(session, knowledge_base_id=knowledge_base_id)
    except KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=404, detail="Knowledge base not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{knowledge_base_id}/restore",
    response_model=KnowledgeBaseRead,
    summary="恢复归档知识库",
    description="将已归档知识库恢复为启用状态，文件、向量和入库记录保持不变。",
)
async def restore_knowledge_base(
    knowledge_base_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> KnowledgeBaseRead:
    try:
        item = await service.restore_knowledge_base(session, knowledge_base_id=knowledge_base_id)
    except KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=404, detail="Knowledge base not found") from exc
    return KnowledgeBaseRead.model_validate(item)
