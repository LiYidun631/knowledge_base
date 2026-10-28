"""管理端审计日志：保留期清理与写操作判定。"""

from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_service.infrastructure.persistence.models import AuditLog, utc_now

# 审计日志保留天数；启动时清理更早的记录，避免表无限增长。
AUDIT_RETENTION_DAYS = 180

# 只审计会改变数据的请求方法；问答与检索以外的写操作都会落库。
AUDITED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def purge_expired_audit_logs(session: AsyncSession, *, now: datetime | None = None) -> int:
    """删除超过保留期的审计日志，返回删除条数。"""
    cutoff = (now or utc_now()) - timedelta(days=AUDIT_RETENTION_DAYS)
    result = await session.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
    return int(result.rowcount or 0)
