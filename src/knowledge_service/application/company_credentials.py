from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.domain.errors import (
    CompanyCredentialAlreadyExists,
    CompanyCredentialNotFound,
    KnowledgeBaseNotFound,
)
from knowledge_service.infrastructure.persistence.models import Company, CompanyCredential


def hash_company_token(token: str) -> str:
    # Token 来自 256 位安全随机数，不是低熵密码；哈希可直接用于唯一索引查询。
    return sha256(token.encode("utf-8")).hexdigest()


async def _lock_company(
    session: AsyncSession, company_id: UUID, *, allow_archived: bool = False
) -> None:
    # 先锁公司行，使同一公司的签发、重置、停用在 MySQL 中串行执行。
    company = await session.scalar(
        select(Company).where(Company.id == company_id).with_for_update()
    )
    if company is None or (not allow_archived and company.status != "active"):
        raise KnowledgeBaseNotFound("Company not found")


async def _get_credential(
    session: AsyncSession, *, company_id: UUID, credential_id: UUID
) -> CompanyCredential:
    # 同时限制公司归属，避免管理员误操作其他公司的凭据。
    credential = await session.scalar(
        select(CompanyCredential)
        .where(
            CompanyCredential.id == credential_id,
            CompanyCredential.company_id == company_id,
        )
        .with_for_update()
    )
    if credential is None:
        raise CompanyCredentialNotFound
    return credential


async def list_company_credentials(
    session: AsyncSession, *, company_id: UUID
) -> list[CompanyCredential]:
    """列出公司全部凭据，启用的排在前面；已归档公司仍可查看。"""
    exists = await session.scalar(select(Company.id).where(Company.id == company_id))
    if exists is None:
        raise KnowledgeBaseNotFound("Company not found")
    return list(
        await session.scalars(
            select(CompanyCredential)
            .where(CompanyCredential.company_id == company_id)
            .order_by(CompanyCredential.is_enabled.desc(), CompanyCredential.created_at)
        )
    )


async def issue_company_token(
    session: AsyncSession, *, company_id: UUID, name: str
) -> tuple[CompanyCredential, str]:
    """为某项目签发一枚新凭据；同公司内名称唯一，明文仅本次返回。"""
    await _lock_company(session, company_id)
    existing = await session.scalar(
        select(CompanyCredential.id)
        .where(
            CompanyCredential.company_id == company_id,
            CompanyCredential.name == name,
        )
        .with_for_update()
    )
    if existing is not None:
        raise CompanyCredentialAlreadyExists("Credential name already exists")
    token = f"ks_{token_urlsafe(32)}"
    credential = CompanyCredential(
        company_id=company_id, name=name, token_hash=hash_company_token(token)
    )
    session.add(credential)
    await session.flush()
    return credential, token


async def reset_company_token(
    session: AsyncSession, *, company_id: UUID, credential_id: UUID
) -> tuple[CompanyCredential, str]:
    """替换单枚凭据的 Token 并启用；只影响该凭据，其他项目 Token 不受影响。"""
    await _lock_company(session, company_id)
    credential = await _get_credential(
        session, company_id=company_id, credential_id=credential_id
    )
    token = f"ks_{token_urlsafe(32)}"
    credential.token_hash = hash_company_token(token)
    credential.is_enabled = True
    await session.flush()
    return credential, token


async def disable_company_token(
    session: AsyncSession, *, company_id: UUID, credential_id: UUID
) -> None:
    """停用单枚凭据；归档公司也允许撤销，重复停用保持幂等。"""
    await _lock_company(session, company_id, allow_archived=True)
    credential = await _get_credential(
        session, company_id=company_id, credential_id=credential_id
    )
    credential.is_enabled = False
    await session.flush()
