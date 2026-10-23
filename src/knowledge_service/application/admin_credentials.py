"""管理员凭据管理：多管理员 Token 的签发、重置、停用与删除。"""

from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

from sqlalchemy import select
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
    session: AsyncSession, *, name: str
) -> tuple[AdminCredential, str]:
    """创建管理员凭据；返回凭据与一次性明文 Token。"""
    token = f"ka_{token_urlsafe(32)}"
    credential = AdminCredential(name=name, token_hash=hash_admin_token(token))
    session.add(credential)
    await session.flush()
    return credential, token


async def get_admin_credential(
    session: AsyncSession, *, admin_credential_id: UUID
) -> AdminCredential:
    credential = await session.get(AdminCredential, admin_credential_id)
    if credential is None:
        raise AdminCredentialNotFound("Admin credential not found")
    return credential


async def reset_admin_credential(
    session: AsyncSession, *, admin_credential_id: UUID
) -> str:
    """替换凭据并启用；旧 Token 对新请求立即失效，新明文仅本次返回。"""
    credential = await get_admin_credential(
        session, admin_credential_id=admin_credential_id
    )
    token = f"ka_{token_urlsafe(32)}"
    credential.token_hash = hash_admin_token(token)
    credential.is_enabled = True
    await session.flush()
    return token


async def set_admin_credential_enabled(
    session: AsyncSession, *, admin_credential_id: UUID, is_enabled: bool
) -> None:
    credential = await get_admin_credential(
        session, admin_credential_id=admin_credential_id
    )
    credential.is_enabled = is_enabled
    await session.flush()


async def delete_admin_credential(
    session: AsyncSession, *, admin_credential_id: UUID
) -> None:
    credential = await get_admin_credential(
        session, admin_credential_id=admin_credential_id
    )
    await session.delete(credential)
    await session.flush()
