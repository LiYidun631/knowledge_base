import asyncio

import httpx
from fastapi import FastAPI

from knowledge_service.api.integration import include_knowledge_module

app = FastAPI()
include_knowledge_module(app)


def test_health_check_returns_service_status() -> None:
    async def request_health() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.get("/api/v1/health")

    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "knowledge-service",
        "version": "0.1.0",
    }
