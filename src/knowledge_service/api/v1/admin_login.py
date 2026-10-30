"""管理端登录入口：账号密码换发短期会话 Token。

本路由故意不挂管理员鉴权依赖，/login 必须匿名可访问；/logout 单独依赖鉴权以拿到当前会话。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.api.auth import (
    ADMIN_ROLES,
    ServicePrincipal,
    authenticate_service,
    bearer_scheme,
)
from knowledge_service.api.v1.schemas import AdminLoginRequest, AdminLoginResponse
from knowledge_service.application import admin_auth
from knowledge_service.infrastructure.persistence.session import get_db_session

router = APIRouter(prefix="/admin", tags=["管理端登录"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post(
    "/login",
    response_model=AdminLoginResponse,
    summary="管理员账号密码登录",
    description="校验通过后换发 7 天有效的会话 Token，按 Bearer 使用；失败按来源 IP 限流。",
)
async def admin_login(
    payload: AdminLoginRequest,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
) -> AdminLoginResponse:
    client_ip = _client_ip(request)
    try:
        admin_auth.ensure_login_allowed(client_ip)
        credential = await admin_auth.authenticate_password(
            session, username=payload.username, password=payload.password
        )
    except admin_auth.LoginRateLimited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except admin_auth.LoginDenied as exc:
        admin_auth.record_login_failure(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    admin_auth.clear_login_failures(client_ip)
    await admin_auth.purge_expired_sessions(session)
    record, token = await admin_auth.issue_session(
        session, credential=credential, client_ip=client_ip
    )
    # 会话明文仅本次返回，必须先提交成功。
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    role = credential.role if credential.role in ADMIN_ROLES else "viewer"
    return AdminLoginResponse(
        token=token,
        expires_at=record.expires_at,
        name=credential.name,
        admin_role=role,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="退出登录",
    description="撤销当前会话；长期 Token 与环境引导 Token 无会话可撤销，仅返回成功。",
)
async def admin_logout(
    _: Annotated[ServicePrincipal, Depends(authenticate_service)],
    session: Annotated[AsyncSession, Depends(get_db_session, scope="function")],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Response:
    if credentials is not None:
        await admin_auth.revoke_session_by_token(session, token=credentials.credentials)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
