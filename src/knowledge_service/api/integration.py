import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import Depends, FastAPI

from knowledge_service.api.audit import audit_write_requests
from knowledge_service.api.v1.admin import router as admin_router
from knowledge_service.api.v1.admin_login import router as admin_login_router
from knowledge_service.api.v1.answer import router as answer_router
from knowledge_service.api.v1.companies import router as companies_router
from knowledge_service.api.v1.documents import router as documents_router
from knowledge_service.api.v1.evaluation import router as evaluation_router
from knowledge_service.api.v1.faqs import router as faqs_router
from knowledge_service.api.v1.health import router as health_router
from knowledge_service.api.v1.knowledge_bases import (
    router as knowledge_bases_router,
)
from knowledge_service.api.v1.product_catalog import router as product_catalog_router
from knowledge_service.api.v1.search import router as search_router
from knowledge_service.application.audit import purge_expired_audit_logs
from knowledge_service.application.evaluation import (
    recover_stale_runs as recover_stale_evaluation_runs,
)
from knowledge_service.core.config import get_database_url
from knowledge_service.infrastructure.persistence.session import (
    get_engine,
    get_session_factory,
)
from knowledge_service.infrastructure.task_queue.outbox import run_dispatcher


def include_knowledge_module(
    app: FastAPI,
    *,
    api_prefix: str = "/api/v1",
    auth_dependency: Callable[..., Any] | None = None,
) -> None:
    """将知识库路由挂入宿主应用，并复用宿主提供的鉴权依赖。"""
    app.include_router(health_router, prefix=api_prefix)
    dependencies = [Depends(auth_dependency)] if auth_dependency else []
    app.include_router(companies_router, prefix=api_prefix, dependencies=dependencies)
    app.include_router(knowledge_bases_router, prefix=api_prefix, dependencies=dependencies)
    app.include_router(documents_router, prefix=api_prefix, dependencies=dependencies)
    app.include_router(search_router, prefix=api_prefix, dependencies=dependencies)
    app.include_router(answer_router, prefix=api_prefix, dependencies=dependencies)
    app.include_router(product_catalog_router, prefix=api_prefix, dependencies=dependencies)
    # 登录入口必须匿名可访问，因此不挂宿主鉴权依赖；其余管理端接口仍按管理员凭据校验。
    app.include_router(admin_login_router, prefix=api_prefix)
    # 管理端接口内部再校验管理员凭据，公司 Token 即使通过宿主鉴权也会被拒绝。
    app.include_router(admin_router, prefix=api_prefix, dependencies=dependencies)
    app.include_router(evaluation_router, prefix=api_prefix, dependencies=dependencies)
    app.include_router(faqs_router, prefix=api_prefix, dependencies=dependencies)
    # 管理端写操作在响应后落审计日志；未绑定会话工厂时自动跳过。
    app.middleware("http")(audit_write_requests)
    logger = logging.getLogger(__name__)

    async def start_ingestion() -> None:
        """应用启动时创建进程内任务循环，不再启动单独的 Worker。"""
        try:
            get_database_url()
        except RuntimeError as exc:
            logger.warning("知识库任务循环未启动：%s", exc)
            return
        stop_event = asyncio.Event()
        app.state.knowledge_ingestion_stop = stop_event
        app.state.knowledge_ingestion_task = asyncio.create_task(
            run_dispatcher(stop_event), name="knowledge-ingestion"
        )

    async def stop_ingestion() -> None:
        """应用关闭时等待当前入库任务完成，并释放共享数据库连接池。"""
        stop_event = getattr(app.state, "knowledge_ingestion_stop", None)
        task = getattr(app.state, "knowledge_ingestion_task", None)
        if stop_event is not None:
            stop_event.set()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task
        if get_engine.cache_info().currsize:
            await get_engine().dispose()

    async def recover_evaluations() -> None:
        """服务重启后把中断的评测运行标记为失败，避免长期停在 running。"""
        try:
            get_database_url()
        except RuntimeError:
            return
        async with get_session_factory()() as session:
            changed = await recover_stale_evaluation_runs(session)
            if changed:
                await session.commit()
                logger.info("已回收 %s 个中断的评测运行", changed)

    async def purge_audit_logs() -> None:
        """清理超过保留期的审计日志；表未迁移时只告警，不阻塞启动。"""
        try:
            get_database_url()
        except RuntimeError:
            return
        try:
            async with get_session_factory()() as session:
                removed = await purge_expired_audit_logs(session)
                if removed:
                    await session.commit()
                    logger.info("已清理 %s 条过期审计日志", removed)
        except Exception:
            logger.warning("审计日志清理失败", exc_info=True)

    host_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def knowledge_lifespan(application: FastAPI) -> AsyncIterator[None]:
        """组合宿主的 lifespan，并在其期间托管知识库任务循环。"""
        async with host_lifespan(application):
            await recover_evaluations()
            await purge_audit_logs()
            try:
                get_database_url()
            except RuntimeError:
                # 未配置数据库时跳过审计，避免启动阶段创建不可用引擎。
                pass
            else:
                application.state.knowledge_audit_session_factory = get_session_factory()
            await start_ingestion()
            try:
                yield
            finally:
                await stop_ingestion()

    app.router.lifespan_context = knowledge_lifespan
