"""管理端写操作审计中间件：响应发送后落库，不影响业务结果。"""

import logging
import time

from fastapi import Request, Response

from knowledge_service.application.audit import AUDITED_METHODS
from knowledge_service.infrastructure.persistence.models import AuditLog

logger = logging.getLogger(__name__)


async def audit_write_requests(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
    """只记录已识别身份的管理端写操作，不保存请求体与查询串。"""
    started = time.perf_counter()
    response = await call_next(request)
    if request.method not in AUDITED_METHODS:
        return response

    principal = getattr(request.state, "service_principal", None)
    if principal is None or not principal.is_admin_side:
        return response
    factory = getattr(request.app.state, "knowledge_audit_session_factory", None)
    if factory is None:
        # 测试或宿主未绑定会话工厂时不写库，避免落到错误的数据库。
        return response

    try:
        async with factory() as session:
            session.add(
                AuditLog(
                    actor_name=principal.name,
                    actor_role=principal.role,
                    credential_id=principal.credential_id,
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    client_ip=request.client.host if request.client else None,
                )
            )
            await session.commit()
    except Exception:
        # 审计失败不能改变已生成的业务响应。
        logger.warning("审计日志写入失败：%s %s", request.method, request.url.path, exc_info=True)
    return response
