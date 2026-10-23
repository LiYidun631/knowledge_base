"""公共知识库服务的公司、大库和子库管理接口。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.auth import require_admin_service_auth
from knowledge_service.api.v1.documents import _ensure_documents_idle, delete_documents
from knowledge_service.api.v1.schemas import (
    BatchDeleteResponse,
    BatchSubLibraryDeleteRequest,
    CompanyCreate,
    CompanyCredentialCreate,
    CompanyCredentialIssued,
    CompanyCredentialRead,
    CompanyRead,
    KnowledgeBaseRead,
    KnowledgeSubLibraryCreate,
    VectorLibraryCreate,
    VectorLibraryRead,
)
from knowledge_service.application import companies as service
from knowledge_service.application import company_credentials
from knowledge_service.domain.errors import (
    CompanyCredentialAlreadyExists,
    CompanyCredentialNotFound,
    ConfigurationNotFound,
    KnowledgeBaseNotFound,
)
from knowledge_service.infrastructure.persistence.models import (
    Company,
    CompanyCredential,
    Document,
    KnowledgeBase,
    VectorLibrary,
)
from knowledge_service.infrastructure.persistence.session import get_db_session

router = APIRouter(prefix="/companies", tags=["公司与向量大库"])


async def _get_company_library(session: AsyncSession, company_id: UUID) -> VectorLibrary:
    """读取公司级大库，删除操作允许清理已归档的大库。"""
    library = await session.scalar(
        select(VectorLibrary)
        .join(Company, Company.id == VectorLibrary.company_id)
        .where(
            VectorLibrary.company_id == company_id,
            Company.id == company_id,
        )
    )
    if library is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="公司级向量大库不存在")
    return library


async def _get_sub_library(
    session: AsyncSession, *, company_id: UUID, knowledge_base_id: UUID
) -> KnowledgeBase:
    """读取属于指定公司的子库，避免跨公司删除。"""
    library = await _get_company_library(session, company_id)
    sub_library = await session.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.company_id == company_id,
            KnowledgeBase.vector_library_id == library.id,
        )
    )
    if sub_library is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="子库不存在")
    return sub_library


async def _delete_sub_library_data(
    session: AsyncSession, *, company_id: UUID, knowledge_base_id: UUID
) -> None:
    """删除子库下的全部向量数据，再删除子库元数据。"""
    sub_library = await _get_sub_library(
        session, company_id=company_id, knowledge_base_id=knowledge_base_id
    )
    document_ids = list(
        await session.scalars(
            select(Document.id).where(
                Document.knowledge_base_id == sub_library.id,
            )
        )
    )
    await delete_documents(session, document_ids)
    await session.delete(sub_library)
    await session.commit()


@router.get(
    "",
    response_model=list[CompanyRead],
    dependencies=[Depends(require_admin_service_auth)],
    summary="列出全部公司（仅管理员）",
    description="返回全部公司租户，含已归档；公司 Token 调用返回 403。",
)
async def list_companies(
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> list[CompanyRead]:
    """管理端公司选择器和公司管理页共用的公司列表。"""
    companies = await service.list_companies(session)
    credentials = list(
        await session.scalars(
            select(CompanyCredential).where(
                CompanyCredential.company_id.in_([company.id for company in companies])
            )
        )
    )
    credential_counts: dict[UUID, list[int]] = {}
    for credential in credentials:
        counts = credential_counts.setdefault(credential.company_id, [0, 0])
        counts[0] += 1
        if credential.is_enabled:
            counts[1] += 1
    library_company_ids = set(
        (
            await session.scalars(
                select(VectorLibrary.company_id).where(
                    VectorLibrary.company_id.in_([company.id for company in companies])
                )
            )
        ).all()
    )
    items: list[CompanyRead] = []
    for company in companies:
        read = CompanyRead.model_validate(company)
        # 列表只暴露数量，凭证明细由单独的管理接口读取。
        read.credential_count, read.active_credential_count = credential_counts.get(
            company.id, [0, 0]
        )
        read.has_vector_library = company.id in library_company_ids
        items.append(read)
    return items


@router.post(
    "",
    response_model=CompanyRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建公司",
    description="创建公共知识库服务的公司租户；公司编码由服务端自动生成，同一公司名称不能重复。",
)
async def create_company(
    payload: CompanyCreate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> CompanyRead:
    """创建公司租户。"""
    try:
        company = await service.create_company(session, **payload.model_dump())
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="公司名称已存在") from exc
    return CompanyRead.model_validate(company)


@router.get(
    "/{company_id}/tokens",
    response_model=list[CompanyCredentialRead],
    dependencies=[Depends(require_admin_service_auth)],
    summary="列出公司全部访问凭据（仅管理员）",
    description="返回公司已签发的全部凭据，含已停用项；不含任何哈希或明文。",
)
async def list_company_tokens(
    company_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> list[CompanyCredentialRead]:
    try:
        credentials = await company_credentials.list_company_credentials(
            session, company_id=company_id
        )
    except KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=404, detail="公司不存在") from exc
    return [CompanyCredentialRead.model_validate(item) for item in credentials]


@router.post(
    "/{company_id}/tokens",
    response_model=CompanyCredentialIssued,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_service_auth)],
    summary="管理员为公司签发新凭据",
    description=(
        "每个项目签发一枚独立凭据，名称在同一公司内唯一；只保存哈希，明文仅本次返回。"
        "凭据权限仍为公司级，不按项目隔离数据。"
    ),
)
async def issue_company_token(
    company_id: UUID,
    payload: CompanyCredentialCreate,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> CompanyCredentialIssued:
    try:
        credential, token = await company_credentials.issue_company_token(
            session, company_id=company_id, name=payload.name
        )
        # 明文仅返回一次，必须先提交成功，不能依赖响应发送后的会话清理提交。
        await session.commit()
    except KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=404, detail="公司不存在或已归档") from exc
    except (CompanyCredentialAlreadyExists, IntegrityError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="该公司下已存在同名凭据") from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return CompanyCredentialIssued(
        company_id=company_id,
        company_credential_id=credential.id,
        name=credential.name,
        token=token,
    )


@router.post(
    "/{company_id}/tokens/{credential_id}/reset",
    response_model=CompanyCredentialIssued,
    dependencies=[Depends(require_admin_service_auth)],
    summary="管理员重置单枚公司凭据",
    description=(
        "替换指定凭据的 Token 并重新启用；同公司其他项目凭据不受影响。"
        "旧 Token 对新请求立即失效，新明文仅本次返回。"
    ),
)
async def reset_company_token(
    company_id: UUID,
    credential_id: UUID,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> CompanyCredentialIssued:
    try:
        credential, token = await company_credentials.reset_company_token(
            session, company_id=company_id, credential_id=credential_id
        )
        await session.commit()
    except KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=404, detail="公司不存在或已归档") from exc
    except CompanyCredentialNotFound as exc:
        raise HTTPException(status_code=404, detail="凭据不存在") from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return CompanyCredentialIssued(
        company_id=company_id,
        company_credential_id=credential.id,
        name=credential.name,
        token=token,
    )


@router.delete(
    "/{company_id}/tokens/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin_service_auth)],
    summary="管理员停用单枚公司凭据",
    description=(
        "保留哈希并停用指定凭据，其他项目凭据不受影响；重复停用返回 204。"
        "公司归档后仍允许撤销凭据。"
    ),
)
async def disable_company_token(
    company_id: UUID,
    credential_id: UUID,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    try:
        await company_credentials.disable_company_token(
            session, company_id=company_id, credential_id=credential_id
        )
        await session.commit()
    except KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=404, detail="公司不存在") from exc
    except CompanyCredentialNotFound as exc:
        raise HTTPException(status_code=404, detail="凭据不存在") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{company_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin_service_auth)],
    summary="删除公司（仅管理员）",
    description=(
        "删除公司租户及其凭据。公司下仍存在向量大库时拒绝删除（409），"
        "需先删除全部子库，再删除公司级大库，最后才能删除公司，防止误删整条数据链。"
    ),
)
async def delete_company(
    company_id: Annotated[UUID, Path(description="公司 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    """逐级保护删除公司：任何大库残留都会阻止删除。"""
    company = await session.scalar(select(Company).where(Company.id == company_id))
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="公司不存在")
    library_id = await session.scalar(
        select(VectorLibrary.id).where(VectorLibrary.company_id == company_id).limit(1)
    )
    if library_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="公司下仍有向量大库，请先删除全部子库和公司级大库",
        )
    await session.execute(
        delete(CompanyCredential).where(CompanyCredential.company_id == company_id)
    )
    await session.delete(company)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="公司仍被其他数据引用，暂时无法删除",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{company_id}/vector-library",
    response_model=VectorLibraryRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建公司级向量大库",
    description="每个公司只能创建一个 active 大库；Embedding 和 Rerank 在此时锁定。",
)
async def create_vector_library(
    company_id: Annotated[UUID, Path(description="公司 ID。")],
    payload: VectorLibraryCreate,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> VectorLibraryRead:
    """为公司创建逻辑上的向量大库。"""
    try:
        library = await service.create_vector_library(
            session,
            company_id=company_id,
            **payload.model_dump(exclude_unset=True),
        )
    except KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=404, detail="公司不存在或没有可用大库") from exc
    except ConfigurationNotFound as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="该公司已经存在向量大库") from exc
    return VectorLibraryRead.model_validate(library)


@router.get(
    "/{company_id}/vector-library",
    response_model=VectorLibraryRead,
    summary="查询公司级向量大库",
    description="返回公司的 active 大库及锁定模型；公司还没有大库时返回 404，"
    "管理页据此决定是否展示模型选择步骤。",
)
async def get_vector_library(
    company_id: Annotated[UUID, Path(description="公司 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> VectorLibraryRead:
    try:
        library = await service.get_vector_library(session, company_id=company_id)
    except KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=404, detail="公司级向量大库不存在") from exc
    return VectorLibraryRead.model_validate(library)


@router.post(
    "/{company_id}/vector-library/knowledge-bases",
    response_model=KnowledgeBaseRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建子库",
    description="在公司的向量大库下创建子库；子库继承大库模型，不单独配置向量模型。",
)
async def create_sub_library(
    company_id: Annotated[UUID, Path(description="公司 ID。")],
    payload: KnowledgeSubLibraryCreate,
    # 响应前提交，避免前端随后的列表查询读到尚未提交的旧数据。
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> KnowledgeBaseRead:
    """创建公司级大库下的子库。"""
    try:
        sub_library = await service.create_sub_library(
            session,
            company_id=company_id,
            **payload.model_dump(),
        )
    except KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=404, detail="公司或向量大库不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="同一公司下的子库名称已存在") from exc
    return KnowledgeBaseRead.model_validate(sub_library)


@router.delete(
    "/{company_id}/vector-library",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除公司级向量大库",
    description=(
        "删除公司的向量大库。大库下存在任何子库时拒绝删除；只有先删除全部子库后才允许删除。"
    ),
)
async def delete_vector_library(
    company_id: Annotated[UUID, Path(description="公司 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    """删除空的公司级向量大库，保留公司租户本身。"""
    library = await _get_company_library(session, company_id)
    sub_library_id = await session.scalar(
        select(KnowledgeBase.id)
        .where(KnowledgeBase.vector_library_id == library.id)
        .limit(1)
    )
    if sub_library_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="公司级大库下仍有子库，请先删除全部子库",
        )
    try:
        await session.delete(library)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="公司级大库仍被其他数据引用，暂时无法删除",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{company_id}/vector-library/knowledge-bases/{knowledge_base_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除子库",
    description=(
        "删除子库及其全部向量、分块、BM25 词项、图片资产和入库任务。"
        "后台文件系统中的原文件不受影响。"
    ),
)
async def delete_sub_library(
    company_id: Annotated[UUID, Path(description="公司 ID。")],
    knowledge_base_id: Annotated[UUID, Path(description="要删除的子库 ID。")],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> Response:
    """删除公司级大库下的一个子库及其知识库侧索引。"""
    await _delete_sub_library_data(
        session, company_id=company_id, knowledge_base_id=knowledge_base_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{company_id}/vector-library/knowledge-bases/batch-delete",
    response_model=BatchDeleteResponse,
    summary="批量删除子库",
    description=(
        "批量删除公司级大库下的子库及其全部向量数据。后台文件系统中的原文件不受影响。"
    ),
)
async def batch_delete_sub_libraries(
    company_id: Annotated[UUID, Path(description="公司 ID。")],
    payload: BatchSubLibraryDeleteRequest,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> BatchDeleteResponse:
    """批量删除子库，先校验所有子库归属，再逐个清理。"""
    library = await _get_company_library(session, company_id)
    sub_libraries = list(
        await session.scalars(
            select(KnowledgeBase).where(
                KnowledgeBase.id.in_(payload.knowledge_base_ids),
                KnowledgeBase.company_id == company_id,
                KnowledgeBase.vector_library_id == library.id,
            )
        )
    )
    found_ids = {sub_library.id for sub_library in sub_libraries}
    missing_ids = [
        str(knowledge_base_id)
        for knowledge_base_id in payload.knowledge_base_ids
        if knowledge_base_id not in found_ids
    ]
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "部分子库不存在或不属于该公司", "knowledge_base_ids": missing_ids},
        )

    # 先统一检查任务状态，避免批量删除执行到中途才发现还有文件正在入库。
    document_ids = list(
        await session.scalars(
            select(Document.id).where(
                Document.knowledge_base_id.in_(payload.knowledge_base_ids),
            )
        )
    )
    await _ensure_documents_idle(session, document_ids)
    for knowledge_base_id in payload.knowledge_base_ids:
        await _delete_sub_library_data(
            session, company_id=company_id, knowledge_base_id=knowledge_base_id
        )
    return BatchDeleteResponse(
        deleted_count=len(payload.knowledge_base_ids),
        deleted_ids=[str(knowledge_base_id) for knowledge_base_id in payload.knowledge_base_ids],
    )
