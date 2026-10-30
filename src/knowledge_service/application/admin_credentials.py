"""管理员凭据管理：多管理员 Token 的签发、重置、停用与删除。"""

from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.domain.errors import AdminCredentialNotFound
from knowledge_service.infrastructure.persistence.models import AdminCredential


def hash_admin_token(token: str) -> str:
    # Token 来自 256 位安全随机数，不是低熵密码；哈希可直接用于唯一索引查询。
    return sha256(token.encode("utf-8")).hexdigest()


async def list_admin_credentials(session: AsyncSession) -> list[AdminCredential]:
    """列出全部管理员凭据；不返回任何哈希或明文。"""
    return list(
        await session.scalars(
            select(AdminCredential).order_by(
                AdminCredential.is_enabled.desc(), AdminCredential.created_at
            )
        )
    )


async def create_admin_credential(
    session: AsyncSession,
    *,
    name: str,
    role: str = "admin",
    username: str | None = None,
    password: str | None = None,
) -> tuple[AdminCredential, str]:
    """创建管理员凭据；返回凭据与一次性明文 Token。

    username/password 可选，设置后该管理员可用账号密码登录管理端。
    """
    # 延迟导入：admin_auth 复用本模块的 hash_admin_token，顶层导入会形成循环。
    from knowledge_service.application.admin_auth import hash_password, normalize_username

    token = f"ka_{token_urlsafe(32)}"
    credential = AdminCredential(
        name=name,
        role=role,
        token_hash=hash_admin_token(token),
        username=normalize_username(username) if username else None,
        password_hash=hash_password(password) if password else None,
    )
    session.add(credential)
    await session.flush()
    return credential, token


async def update_admin_credential(
    session: AsyncSession,
    *,
    admin_credential_id: UUID,
    role: str | None = None,
    is_enabled: bool | None = None,
    username: str | None = None,
    password: str | None = None,
) -> AdminCredential:
    """按需修改角色、启停状态与登录账号密码，返回最新凭据。"""
    from knowledge_service.application.admin_auth import (
        hash_password,
        normalize_username,
        revoke_credential_sessions,
    )

    credential = await get_admin_credential(session, admin_credential_id=admin_credential_id)
    if role is not None:
        credential.role = role
    if username is not None:
        # 传空串表示收回账号密码登录能力。
        credential.username = normalize_username(username) if username else None
    if password is not None:
        credential.password_hash = hash_password(password)
    needs_revoke = bool(password) or username == ""
    if is_enabled is not None:
        credential.is_enabled = is_enabled
        # 停用即踢掉全部已登录会话，重新启用也不会把旧会话放回来。
        needs_revoke = needs_revoke or not is_enabled
    await session.flush()
    if needs_revoke:
        await revoke_credential_sessions(session, credential_id=credential.id)
    return credential


async def count_enabled_admins(session: AsyncSession, *, exclude_id: UUID | None = None) -> int:
    """统计启用中的 admin 角色凭据数量，用于防止撤空管理员。"""
    statement = select(func.count(AdminCredential.id)).where(
        AdminCredential.role == "admin",
        AdminCredential.is_enabled.is_(True),
    )
    if exclude_id is not None:
        statement = statement.where(AdminCredential.id != exclude_id)
    return int(await session.scalar(statement) or 0)


async def get_admin_credential(
    session: AsyncSession, *, admin_credential_id: UUID
) -> AdminCredential:
    credential = await session.get(AdminCredential, admin_credential_id)
    if credential is None:
        raise AdminCredentialNotFound("Admin credential not found")
    return credential


async def reset_admin_credential(session: AsyncSession, *, admin_credential_id: UUID) -> str:
    """替换凭据并启用；旧 Token 对新请求立即失效，新明文仅本次返回。"""
    from knowledge_service.application.admin_auth import revoke_credential_sessions

    credential = await get_admin_credential(session, admin_credential_id=admin_credential_id)
    token = f"ka_{token_urlsafe(32)}"
    credential.token_hash = hash_admin_token(token)
    credential.is_enabled = True
    await session.flush()
    # 重置通常意味着旧 Token 已泄露，已登录会话一并撤销。
    await revoke_credential_sessions(session, credential_id=credential.id)
    return token


async def set_admin_credential_enabled(
    session: AsyncSession, *, admin_credential_id: UUID, is_enabled: bool
) -> None:
    from knowledge_service.application.admin_auth import revoke_credential_sessions

    credential = await get_admin_credential(session, admin_credential_id=admin_credential_id)
    credential.is_enabled = is_enabled
    await session.flush()
    if not is_enabled:
        await revoke_credential_sessions(session, credential_id=credential.id)


async def delete_admin_credential(session: AsyncSession, *, admin_credential_id: UUID) -> None:
    credential = await get_admin_credential(session, admin_credential_id=admin_credential_id)
    await session.delete(credential)
    await session.flush()
