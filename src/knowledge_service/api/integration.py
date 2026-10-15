import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import Depends, FastAPI

from knowledge_service.api.v1.documents import router as documents_router
from knowledge_service.api.v1.health import router as health_router
from knowledge_service.api.v1.knowledge_bases import (
    catalog_router,
)
from knowledge_service.api.v1.knowledge_bases import (
    router as knowledge_bases_router,
)
from knowledge_service.core.config import get_database_url
from knowledge_service.infrastructure.persistence.session import get_engine
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
    app.include_router(catalog_router, prefix=api_prefix, dependencies=dependencies)
    app.include_router(knowledge_bases_router, prefix=api_prefix, dependencies=dependencies)
    app.include_router(documents_router, prefix=api_prefix, dependencies=dependencies)
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

    host_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def knowledge_lifespan(application: FastAPI) -> AsyncIterator[None]:
        """组合宿主的 lifespan，并在其期间托管知识库任务循环。"""
        async with host_lifespan(application):
            await start_ingestion()
            try:
                yield
            finally:
                await stop_ingestion()

    app.router.lifespan_context = knowledge_lifespan
