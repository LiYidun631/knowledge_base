"""管理员账号密码登录：密码哈希、会话签发与吊销、在线爆破限流。

会话 Token 与长期 Token 共用同一套 Bearer 鉴权，只是有效期短且可吊销；
数据库里两者都只保存 SHA-256 哈希，密码单独用 PBKDF2-HMAC-SHA256 加盐哈希。
"""

import logging
from collections import defaultdict, deque
from datetime import timedelta
from hashlib import pbkdf2_hmac
from hmac import compare_digest
from secrets import token_hex, token_urlsafe
from time import monotonic
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.application.admin_credentials import hash_admin_token
from knowledge_service.infrastructure.persistence.models import (
    AdminCredential,
    AdminSession,
    utc_now,
)

logger = logging.getLogger(__name__)

# 会话有效期 7 天：管理员每天使用管理端，不该被频繁要求重填密码。
ADMIN_SESSION_TTL = timedelta(days=7)
PBKDF2_ITERATIONS = 200_000
_PASSWORD_PREFIX = "pbkdf2_sha256"

# 换成短密码就多了在线爆破面，按来源 IP 做内存计数兜住。
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 300
_login_failures: dict[str, deque[float]] = defaultdict(deque)


class LoginDenied(Exception):
    """账号或密码不正确；统一文案，避免暴露账号是否存在。"""


class LoginRateLimited(Exception):
    """同一来源 IP 失败次数过多。"""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"登录失败次数过多，请 {retry_after} 秒后重试")
        self.retry_after = retry_after


class LoginAccountTaken(Exception):
    """登录账号已被其他管理员占用。"""


def normalize_username(username: str) -> str:
    """账号统一按小写存储与比对，避免同一账号出现大小写两份。"""
    return username.strip().lower()


def hash_password(password: str) -> str:
    salt = token_hex(16)
    digest = pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()
    return f"{_PASSWORD_PREFIX}${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        scheme, iterations, salt, digest = encoded.split("$")
        if scheme != _PASSWORD_PREFIX:
            return False
        candidate = pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        ).hex()
    except (ValueError, TypeError):
        logger.warning("管理员密码哈希格式异常，按校验失败处理")
        return False
    return compare_digest(candidate, digest)


def prune_login_failures(client_ip: str | None) -> None:
    """丢弃窗口外的失败记录。"""
    if not client_ip:
        return
    failures = _login_failures.get(client_ip)
    if not failures:
        return
    deadline = monotonic() - LOGIN_WINDOW_SECONDS
    while failures and failures[0] < deadline:
        failures.popleft()
    if not failures:
        _login_failures.pop(client_ip, None)


def ensure_login_allowed(client_ip: str | None) -> None:
    prune_login_failures(client_ip)
    failures = _login_failures.get(client_ip or "")
    if failures and len(failures) >= LOGIN_MAX_FAILURES:
        retry_after = max(1, int(LOGIN_WINDOW_SECONDS - (monotonic() - failures[0])))
        raise LoginRateLimited(retry_after)


def record_login_failure(client_ip: str | None) -> None:
    if client_ip:
        _login_failures[client_ip].append(monotonic())


def clear_login_failures(client_ip: str | None) -> None:
    _login_failures.pop(client_ip or "", None)


async def authenticate_password(
    session: AsyncSession, *, username: str, password: str
) -> AdminCredential:
    """按账号校验启用中的管理员与密码；任何失败都抛同一异常。"""
    credential = (
        await session.scalars(
            select(AdminCredential).where(
                AdminCredential.username == normalize_username(username),
                AdminCredential.is_enabled.is_(True),
            )
        )
    ).first()
    if credential is None or not verify_password(password, credential.password_hash):
        raise LoginDenied("账号或密码不正确")
    return credential


async def issue_session(
    session: AsyncSession, *, credential: AdminCredential, client_ip: str | None
) -> tuple[AdminSession, str]:
    """签发一次性明文会话 Token，库内只存哈希。"""
    token = f"kas_{token_urlsafe(32)}"
    record = AdminSession(
        credential_id=credential.id,
        token_hash=hash_admin_token(token),
        expires_at=utc_now() + ADMIN_SESSION_TTL,
        client_ip=client_ip,
    )
    session.add(record)
    await session.flush()
    return record, token


async def find_active_session(
    session: AsyncSession, *, token: str
) -> tuple[AdminSession, AdminCredential] | None:
    """按会话 Token 查有效会话；凭据停用即视为失效。"""
    row = (
        await session.execute(
            select(AdminSession, AdminCredential)
            .join(AdminCredential, AdminCredential.id == AdminSession.credential_id)
            .where(
                AdminSession.token_hash == hash_admin_token(token),
                AdminCredential.is_enabled.is_(True),
            )
        )
    ).first()
    if row is None:
        return None
    record, credential = row
    if record.expires_at <= utc_now():
        return None
    return record, credential


async def revoke_session_by_token(session: AsyncSession, *, token: str) -> int:
    result = await session.execute(
        delete(AdminSession).where(AdminSession.token_hash == hash_admin_token(token))
    )
    await session.flush()
    return int(result.rowcount or 0)


async def revoke_credential_sessions(session: AsyncSession, *, credential_id: UUID) -> int:
    """停用凭据或改密后踢掉全部会话，新登录需要重新输密码。"""
    result = await session.execute(
        delete(AdminSession).where(AdminSession.credential_id == credential_id)
    )
    await session.flush()
    return int(result.rowcount or 0)


async def purge_expired_sessions(session: AsyncSession) -> int:
    result = await session.execute(
        delete(AdminSession).where(AdminSession.expires_at <= utc_now())
    )
    await session.flush()
    return int(result.rowcount or 0)


async def ensure_username_available(
    session: AsyncSession, *, username: str, exclude_id: UUID | None = None
) -> None:
    statement = select(AdminCredential.id).where(
        AdminCredential.username == normalize_username(username)
    )
    if exclude_id is not None:
        statement = statement.where(AdminCredential.id != exclude_id)
    if await session.scalar(statement) is not None:
        raise LoginAccountTaken("登录账号已被占用")
