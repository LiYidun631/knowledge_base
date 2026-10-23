from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from knowledge_service.core.config import get_database_url


@lru_cache
def get_engine():
    return create_async_engine(
        get_database_url(),
        pool_pre_ping=True,
    )


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    # 管理端接口统一使用 Depends(get_db_session, scope="function")，让这里的 commit
    # 在响应发出前完成；否则前端紧随其后的列表查询可能读到尚未提交的旧数据。
    # SSE 流式接口保持默认 request 作用域，会话需覆盖整个流式生成过程。
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
