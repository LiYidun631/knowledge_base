from dataclasses import dataclass
from hmac import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.application.admin_credentials import hash_admin_token
from knowledge_service.application.company_credentials import hash_company_token
from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.models import (
    AdminCredential,
    Company,
    CompanyCredential,
    Document,
    DocumentAsset,
    KnowledgeBase,
    QARecord,
)
from knowledge_service.infrastructure.persistence.session import get_db_session

bearer_scheme = HTTPBearer(auto_error=False, scheme_name="KnowledgeServiceBearer")

ADMIN_ROLES = frozenset({"admin", "operator", "viewer"})
BOOTSTRAP_ADMIN_NAME = "环境引导 Token"

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 只读角色仍可发起问答与检索：这类 POST 只产生查询记录，不改配置数据。
_VIEWER_POST_SUFFIXES = ("/ask", "/ask/stream", "/search")
# 凭据、模型配置、提示词与审计属于管理员专属范围，运营/只读角色一律拒绝。
_ADMIN_ONLY_SEGMENTS = frozenset(
    {"credentials", "tokens", "model-profiles", "model-prices", "prompts", "audit-logs"}
)


@dataclass(frozen=True)
class ServicePrincipal:
    """一次请求的调用者身份；company_id 为空表示管理员侧凭据。"""

    role: str
    name: str
    company_id: UUID | None = None
    credential_id: UUID | None = None
    is_bootstrap: bool = False

    @property
    def is_admin_side(self) -> bool:
        return self.company_id is None


def read_service_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str | None:
    admin_token = settings.auth_admin_token.get_secret_value()
    if not admin_token:
        # 缺少管理员配置时保持关闭，避免凭据失去签发和撤销入口。
        raise HTTPException(status_code=503, detail="知识库服务尚未配置管理员凭据")
    if credentials is None:
        # 缺失或格式错误的请求在打开数据库连接前拒绝。
        raise HTTPException(
            status_code=401,
            detail="缺少或无效的知识库访问凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied = credentials.credentials
    if compare_digest(supplied.encode("utf-8"), admin_token.encode("utf-8")):
        return None
    return supplied


async def authenticate_service(
    request: Request,
    token: Annotated[str | None, Depends(read_service_token)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ServicePrincipal:
    if token is None:
        principal = ServicePrincipal(role="admin", name=BOOTSTRAP_ADMIN_NAME, is_bootstrap=True)
    else:
        admin = (
            await session.execute(
                select(
                    AdminCredential.id,
                    AdminCredential.name,
                    AdminCredential.role,
                ).where(
                    AdminCredential.token_hash == hash_admin_token(token),
                    AdminCredential.is_enabled.is_(True),
                )
            )
        ).first()
        if admin is not None:
            # 库里出现未知角色时按只读处理，避免配置错误放大权限。
            role = admin.role if admin.role in ADMIN_ROLES else "viewer"
            principal = ServicePrincipal(role=role, name=admin.name, credential_id=admin.id)
        else:
            company_id = await session.scalar(
                select(CompanyCredential.company_id).where(
                    CompanyCredential.token_hash == hash_company_token(token),
                    CompanyCredential.is_enabled.is_(True),
                )
            )
            if company_id is None:
                raise HTTPException(
                    status_code=401,
                    detail="缺少或无效的知识库访问凭据",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            # 每个请求重新查库，不缓存鉴权结果，重置或停用后新请求立即失效。
            principal = ServicePrincipal(role="company", name="公司凭据", company_id=company_id)
    # 审计中间件在响应阶段从请求状态读取操作者身份。
    request.state.service_principal = principal
    return principal


def _enforce_admin_role(request: Request, principal: ServicePrincipal) -> None:
    """按角色拦截管理端请求；管理员不受限制。"""
    if principal.role == "admin":
        return
    segments = set(request.url.path.strip("/").split("/"))
    if segments & _ADMIN_ONLY_SEGMENTS:
        raise HTTPException(status_code=403, detail="该操作需要管理员凭据")
    if principal.role == "viewer" and request.method not in _READ_METHODS:
        # 问答与检索是只读角色的正常用法，不按写操作拦截。
        if not request.url.path.endswith(_VIEWER_POST_SUFFIXES):
            raise HTTPException(status_code=403, detail="只读凭据不能执行写操作")


async def require_admin_service_auth(
    request: Request,
    principal: Annotated[ServicePrincipal, Depends(authenticate_service)],
) -> None:
    """有效公司 Token 与无效凭据都不得进入管理端接口，并在此执行角色拦截。"""
    if principal.company_id is not None:
        raise HTTPException(status_code=403, detail="该操作需要管理员凭据")
    _enforce_admin_role(request, principal)


def _parse_scope_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="公司或资源 ID 必须是有效 UUID") from None


async def require_service_auth(
    request: Request,
    principal: Annotated[ServicePrincipal, Depends(authenticate_service)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """统一校验公司、资源归属和角色，防止只传资源 ID 的接口绕过租户隔离。"""
    if principal.company_id is None:
        _enforce_admin_role(request, principal)
        return

    company_id = principal.company_id
    path_params = request.path_params
    for supplied in (
        path_params.get("company_id"),
        *request.query_params.getlist("company_id"),
    ):
        if supplied is not None and _parse_scope_id(supplied) != company_id:
            raise HTTPException(status_code=403, detail="访问凭据无权访问该公司")

    resource_queries = []
    for parameter, model in (
        ("knowledge_base_id", KnowledgeBase),
        ("document_id", Document),
        ("qa_record_id", QARecord),
    ):
        if parameter in path_params:
            resource_queries.append(
                select(model.id).where(
                    model.id == _parse_scope_id(path_params[parameter]),
                    model.company_id == company_id,
                )
            )
    if "asset_id" in path_params:
        # 图片本身没有公司字段，必须通过所属文档验证授权。
        resource_queries.append(
            select(DocumentAsset.id)
            .join(Document, Document.id == DocumentAsset.document_id)
            .where(
                DocumentAsset.id == _parse_scope_id(path_params["asset_id"]),
                Document.company_id == company_id,
            )
        )

    if not resource_queries and "company_id" not in path_params:
        # 无归属入口仅开放按公司列表与全局产品目录只读；其余入口仅允许管理员。
        if request.method != "GET" or request.scope["route"].name not in {
            "list_knowledge_bases",
            "list_product_catalog",
        }:
            raise HTTPException(status_code=403, detail="该操作需要管理员凭据")

    active_company = await session.scalar(
        select(Company.id).where(
            Company.id == company_id,
            Company.status == "active",
        )
    )
    if active_company is None:
        raise HTTPException(status_code=403, detail="凭据所属公司不存在或已归档")
    for statement in resource_queries:
        if await session.scalar(statement) is None:
            # 不区分资源不存在或属于其他公司，避免泄露跨公司资源信息。
            raise HTTPException(status_code=404, detail="资源不存在")
