"""验证公司创建请求与服务端公司编码生成规则。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.api.v1.knowledge_bases import router
from knowledge_service.api.v1.schemas import CompanyCreate
from knowledge_service.application.companies import create_company
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import KnowledgeBase, ModelProfile
from knowledge_service.infrastructure.persistence.session import get_db_session


def test_company_create_does_not_accept_caller_supplied_code() -> None:
    """调用方只能提交公司名称和说明，不能指定内部公司编码。"""
    with pytest.raises(ValidationError):
        CompanyCreate(name="永康健康", code="yongkang")


def test_company_code_is_generated_and_unique(tmp_path) -> None:
    """服务端生成稳定格式的内部编码，且每家公司的编码唯一。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'companies.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                first = await create_company(session, name="永康健康", description=None)
                second = await create_company(session, name="测试公司", description=None)

                assert first.code.startswith("company-")
                assert second.code.startswith("company-")
                assert first.code != second.code
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_knowledge_base_list_requires_company_and_filters_before_pagination(tmp_path) -> None:
    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'company-list.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                first = await create_company(session, name="公司一", description=None)
                second = await create_company(session, name="公司二", description=None)
                embedding = ModelProfile(
                    name="list-embedding", kind="embedding", provider="openai_compatible",
                    model_name="test-embedding", dimensions=2,
                )
                session.add(embedding)
                await session.flush()
                now = datetime.now(UTC)
                for index, (company_id, name, status) in enumerate([
                    (first.id, "早期子库", "active"),
                    (second.id, "同名子库", "active"),
                    (first.id, "同名子库", "active"),
                    (first.id, "已归档子库", "archived"),
                    (None, "旧版无公司知识库", "active"),
                ]):
                    session.add(KnowledgeBase(
                        company_id=company_id, name=name, status=status,
                        embedding_model_id=embedding.id,
                        created_at=now + timedelta(seconds=index),
                    ))
                await session.commit()

                async def db_session():
                    yield session

                app = FastAPI()
                app.include_router(router, prefix="/api/v1")
                app.dependency_overrides[get_db_session] = db_session
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test",
                ) as client:
                    path = "/api/v1/knowledge-bases"
                    response = await client.get(path, params={"company_id": str(first.id)})
                    assert response.status_code == 200
                    assert [item["name"] for item in response.json()] == ["同名子库", "早期子库"]
                    assert all(item["company_id"] == str(first.id) for item in response.json())
                    response = await client.get(path, params={
                        "company_id": str(first.id), "offset": 1, "limit": 1,
                    })
                    assert [item["name"] for item in response.json()] == ["早期子库"]
                    response = await client.get(path, params={"company_id": str(second.id)})
                    assert [item["company_id"] for item in response.json()] == [str(second.id)]
                    response = await client.get(path, params={"company_id": str(uuid4())})
                    assert response.json() == []
                    for params in ({}, {"company_id": "invalid"}):
                        response = await client.get(path, params=params)
                        assert response.status_code == 422
        finally:
            await engine.dispose()

    asyncio.run(scenario())
