import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.core.config import Settings, get_settings
from knowledge_service.core.credentials import resolve_profile_api_key
from knowledge_service.infrastructure.model_providers.chat import ChatProviderError
from knowledge_service.infrastructure.model_providers.embeddings import EmbeddingProviderError
from knowledge_service.infrastructure.ocr_client import LayoutResult, OcrServiceError
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.models import (
    Company,
    CompanyCredential,
    KnowledgeBase,
    ModelProfile,
    VectorLibrary,
)
from knowledge_service.infrastructure.persistence.session import get_db_session


def _app(monkeypatch, settings):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


@asynccontextmanager
async def _admin_service(monkeypatch, tmp_path, *, with_encryption_key=True):
    """独立 SQLite 库验证管理端接口，不连接已有业务数据。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            embedding = ModelProfile(
                name="已绑定-embedding",
                kind="embedding",
                provider="openai_compatible",
                model_name="text-embedding-3-small",
                dimensions=1536,
                settings={"base_url": "https://api.openai.com/v1"},
                is_default=True,
            )
            rerank = ModelProfile(
                name="备用-rerank",
                kind="rerank",
                provider="dashscope",
                model_name="qwen3.7-text-rerank",
                settings={
                    "base_url": "https://dashscope.aliyuncs.com/compatible-api/v1",
                    "rerank_path": "/reranks",
                },
            )
            session.add_all([embedding, rerank])
            await session.flush()
            company = Company(code="admin-company", name="管理测试公司")
            session.add(company)
            await session.flush()
            session.add(
                CompanyCredential(
                    company_id=company.id,
                    name="默认凭据",
                    token_hash=sha256(b"some-company-token").hexdigest(),
                )
            )
            library = VectorLibrary(
                company_id=company.id,
                name="占位大库",
                embedding_model_id=embedding.id,
            )
            session.add(library)
            await session.flush()
            session.add(
                KnowledgeBase(
                    name="占位子库",
                    company_id=company.id,
                    embedding_model_id=embedding.id,
                    vector_library_id=library.id,
                )
            )
            await session.commit()

        settings = Settings(
            _env_file=None,
            auth_admin_token="admin-test-token",
            credential_encryption_key=(
                Fernet.generate_key().decode() if with_encryption_key else ""
            ),
        )
        app = _app(monkeypatch, settings)
        # 加密助手直接调用 lru_cache 配置，需要与 FastAPI 依赖注入分开打补丁。
        monkeypatch.setattr("knowledge_service.core.credentials.get_settings", lambda: settings)

        async def db_session():
            async with factory() as session:
                yield session
                await session.commit()

        app.dependency_overrides[get_db_session] = db_session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers={"Authorization": "Bearer admin-test-token"},
        ) as client:
            yield client, factory
    finally:
        await engine.dispose()


def _valid_embedding_payload(**overrides):
    payload = {
        "name": "新模型",
        "kind": "embedding",
        "provider": "openai_compatible",
        "model_name": "text-embedding-3-large",
        "dimensions": 3072,
        "settings": {"base_url": "https://api.openai.com/v1", "batch_size": 16},
    }
    payload.update(overrides)
    return payload


def test_admin_model_profile_crud_and_company_token_denied(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, _factory):
            response = await client.post("admin/model-profiles", json=_valid_embedding_payload())
            assert response.status_code == 201, response.text
            created = response.json()
            assert created["kind"] == "embedding"
            assert created["is_default"] is False
            assert created["has_api_key"] is False

            response = await client.get("admin/model-profiles")
            assert response.status_code == 200
            assert {item["name"] for item in response.json()} >= {
                "已绑定-embedding",
                "备用-rerank",
                "新模型",
            }
            assert all("api_key" not in item and "cipher" not in item for item in response.json())

            response = await client.get("admin/model-profiles", params={"kind": "rerank"})
            assert response.status_code == 200
            assert [item["name"] for item in response.json()] == ["备用-rerank"]

            response = await client.patch(
                f"admin/model-profiles/{created['id']}",
                json={"model_name": "text-embedding-3-small"},
            )
            assert response.status_code == 200
            assert response.json()["model_name"] == "text-embedding-3-small"

            response = await client.delete(f"admin/model-profiles/{created['id']}")
            assert response.status_code == 204
            response = await client.get(f"admin/model-profiles/{created['id']}")
            assert response.status_code == 404

            # 公司 Token 不能访问管理端接口。
            headers = {"Authorization": "Bearer some-company-token"}
            response = await client.get("admin/model-profiles", headers=headers)
            assert response.status_code == 403

    asyncio.run(scenario())


def test_admin_bound_embedding_profile_rejects_vector_contract_changes(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, factory):
            async with factory() as session:
                bound_id = str(
                    await session.scalar(
                        select(ModelProfile.id).where(ModelProfile.name == "已绑定-embedding")
                    )
                )

            # 已绑定档案不能改模型名、维度或调用参数，否则已入库向量会与新向量空间失配。
            for payload in (
                {"model_name": "text-embedding-3-large"},
                {"dimensions": 3072},
                {"settings": {"base_url": "https://api.openai.com/v2"}},
            ):
                response = await client.patch(f"admin/model-profiles/{bound_id}", json=payload)
                assert response.status_code == 409, response.text
                assert "已被知识库使用" in response.json()["detail"]

            # 名称不改变向量契约，允许修改。
            response = await client.patch(
                f"admin/model-profiles/{bound_id}", json={"name": "绑定档案改名"}
            )
            assert response.status_code == 200
            assert response.json()["name"] == "绑定档案改名"

            # 未绑定的 rerank 档案不受限制。
            async with factory() as session:
                free_id = str(
                    await session.scalar(
                        select(ModelProfile.id).where(ModelProfile.name == "备用-rerank")
                    )
                )
            response = await client.patch(
                f"admin/model-profiles/{free_id}", json={"model_name": "qwen3-rerank"}
            )
            assert response.status_code == 200
            assert response.json()["model_name"] == "qwen3-rerank"

    asyncio.run(scenario())


def test_admin_model_profile_validation_rejects_invalid_specs(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, _factory):
            response = await client.post(
                "admin/model-profiles", json=_valid_embedding_payload(dimensions=None)
            )
            assert response.status_code == 422
            response = await client.post(
                "admin/model-profiles",
                json=_valid_embedding_payload(provider="unknown_vendor"),
            )
            assert response.status_code == 422
            response = await client.post(
                "admin/model-profiles",
                json=_valid_embedding_payload(settings={"base_url": "not-a-url"}),
            )
            assert response.status_code == 422
            response = await client.post(
                "admin/model-profiles",
                json=_valid_embedding_payload(name="已绑定-embedding"),
            )
            assert response.status_code == 409

    asyncio.run(scenario())


def test_admin_chat_model_profile_crud_and_validation(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, _factory):
            response = await client.post(
                "admin/model-profiles",
                json={
                    "name": "默认生成模型",
                    "kind": "chat",
                    "provider": "dashscope",
                    "model_name": "qwen-plus",
                    "settings": {
                        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        "chat_path": "/chat/completions",
                    },
                },
            )
            assert response.status_code == 201, response.text
            created = response.json()
            assert created["kind"] == "chat"
            assert created["dimensions"] is None

            # 列表可按 chat 过滤。
            response = await client.get("admin/model-profiles", params={"kind": "chat"})
            assert response.status_code == 200
            assert [item["name"] for item in response.json()] == ["默认生成模型"]

            # chat 档案不允许填写向量维度。
            response = await client.post(
                "admin/model-profiles",
                json={
                    "name": "带维度",
                    "kind": "chat",
                    "provider": "dashscope",
                    "model_name": "qwen-plus",
                    "dimensions": 1536,
                    "settings": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
                },
            )
            assert response.status_code == 422
            # 地址与路径校验沿用现有规则。
            response = await client.post(
                "admin/model-profiles",
                json={
                    "name": "坏地址",
                    "kind": "chat",
                    "provider": "dashscope",
                    "model_name": "qwen-plus",
                    "settings": {"base_url": "not-a-url"},
                },
            )
            assert response.status_code == 422
            response = await client.post(
                "admin/model-profiles",
                json={
                    "name": "坏路径",
                    "kind": "chat",
                    "provider": "dashscope",
                    "model_name": "qwen-plus",
                    "settings": {
                        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        "chat_path": "chat/completions",
                    },
                },
            )
            assert response.status_code == 422
            response = await client.post(
                "admin/model-profiles",
                json={
                    "name": "占位模型名",
                    "kind": "chat",
                    "provider": "dashscope",
                    "model_name": "YOUR_CHAT_MODEL",
                    "settings": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
                },
            )
            assert response.status_code == 422

    asyncio.run(scenario())


def test_admin_ocr_model_profile_crud_and_validation(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, _factory):
            payload = {
                "name": "扫描件 OCR",
                "kind": "ocr",
                "provider": "qianfan",
                "model_name": "paddleocr-vl-0.9b",
                "settings": {
                    "base_url": "https://qianfan.baidubce.com/v2",
                    "chat_path": "/chat/completions",
                    "ocr_mode": "auto",
                },
            }
            response = await client.post("admin/model-profiles", json=payload)
            assert response.status_code == 201, response.text
            created = response.json()
            assert created["kind"] == "ocr"
            assert created["dimensions"] is None

            response = await client.get("admin/model-profiles", params={"kind": "ocr"})
            assert response.status_code == 200
            assert [item["name"] for item in response.json()] == ["扫描件 OCR"]

            # OCR 档案不允许写维度、供应商必须在 OCR 适配器白名单内、模式取值受限。
            for override in (
                {"dimensions": 1536},
                {"provider": "deepseek"},
                {"settings": {**payload["settings"], "ocr_mode": "layout"}},
                {"settings": {"base_url": "not-a-url"}},
                {"settings": {**payload["settings"], "chat_path": "chat/completions"}},
                {"model_name": "YOUR_OCR_MODEL"},
            ):
                response = await client.post(
                    "admin/model-profiles", json={**payload, "name": "坏配置", **override}
                )
                assert response.status_code == 422, (override, response.text)

            # OCR 也支持设为全局默认档案，供入库解析使用。
            response = await client.post(f"admin/model-profiles/{created['id']}/default")
            assert response.status_code == 200
            assert response.json()["is_default"] is True

            response = await client.get("admin/model-usage")
            assert response.status_code == 200
            ocr_slot = {slot["key"]: slot for slot in response.json()["slots"]}["ocr"]
            assert [
                (item["name"], item["source"], item["is_active"]) for item in ocr_slot["items"]
            ] == [
                ("扫描件 OCR", "default", True),
                ("环境 OCR 模型", "environment", False),
            ]

    asyncio.run(scenario())


def test_admin_model_api_key_is_write_only(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, factory):
            response = await client.post("admin/model-profiles", json=_valid_embedding_payload())
            profile_id = response.json()["id"]
            response = await client.put(
                f"admin/model-profiles/{profile_id}/api-key",
                json={"api_key": "sk-secret-plaintext-value"},
            )
            assert response.status_code == 204
            assert response.headers["cache-control"] == "no-store"

            response = await client.get(f"admin/model-profiles/{profile_id}")
            body = response.json()
            assert body["has_api_key"] is True
            assert "sk-secret-plaintext-value" not in response.text

            # 库内只存密文，模型调用时能解密回明文。
            async with factory() as session:
                row = await session.scalar(
                    select(ModelProfile).where(ModelProfile.id == UUID(profile_id))
                )
                assert row.api_key_cipher != "sk-secret-plaintext-value"
                assert "sk-secret" not in row.api_key_cipher
                assert resolve_profile_api_key(row) == "sk-secret-plaintext-value"

            response = await client.delete(f"admin/model-profiles/{profile_id}/api-key")
            assert response.status_code == 204
            response = await client.get(f"admin/model-profiles/{profile_id}")
            assert response.json()["has_api_key"] is False

    asyncio.run(scenario())


def test_admin_model_api_key_requires_encryption_key(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path, with_encryption_key=False) as (
            client,
            _factory,
        ):
            response = await client.post("admin/model-profiles", json=_valid_embedding_payload())
            profile_id = response.json()["id"]
            response = await client.put(
                f"admin/model-profiles/{profile_id}/api-key", json={"api_key": "sk-x"}
            )
            assert response.status_code == 503
            assert "CREDENTIAL_ENCRYPTION_KEY" in response.text
            assert "sk-x" not in response.text

    asyncio.run(scenario())


def test_admin_default_switch_and_delete_protection(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, factory):
            response = await client.post(
                "admin/model-profiles",
                json=_valid_embedding_payload(name="新默认"),
            )
            profile_id = response.json()["id"]
            response = await client.post(f"admin/model-profiles/{profile_id}/default")
            assert response.status_code == 200
            assert response.json()["is_default"] is True
            response = await client.get("admin/model-profiles", params={"kind": "embedding"})
            defaults = [item["name"] for item in response.json() if item["is_default"]]
            assert defaults == ["新默认"]

            # 仍被引用的档案不能删除。
            async with factory() as session:
                bound_id = str(
                    await session.scalar(
                        select(ModelProfile.id).where(ModelProfile.name == "已绑定-embedding")
                    )
                )
            response = await client.delete(f"admin/model-profiles/{bound_id}")
            assert response.status_code == 409
            assert "仍被" in response.json()["detail"]

            # 未绑定的 rerank 档案可以删除。
            async with factory() as session:
                free_id = str(
                    await session.scalar(
                        select(ModelProfile.id).where(ModelProfile.name == "备用-rerank")
                    )
                )
            response = await client.delete(f"admin/model-profiles/{free_id}")
            assert response.status_code == 204

    asyncio.run(scenario())


def test_admin_model_profile_connectivity_test(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, factory):
            async with factory() as session:
                embedding_id = str(
                    await session.scalar(
                        select(ModelProfile.id).where(ModelProfile.name == "已绑定-embedding")
                    )
                )
                rerank_id = str(
                    await session.scalar(
                        select(ModelProfile.id).where(ModelProfile.name == "备用-rerank")
                    )
                )
            response = await client.post(
                "admin/model-profiles",
                json={
                    "name": "连通性测试-chat",
                    "kind": "chat",
                    "provider": "dashscope",
                    "model_name": "qwen-plus",
                    "settings": {
                        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        "chat_path": "/chat/completions",
                    },
                },
            )
            assert response.status_code == 201, response.text
            chat_id = response.json()["id"]

            async def fake_embed(profile, texts):
                assert texts
                return [[0.5] * profile.dimensions]

            async def fake_rerank(profile, *, query, documents):
                assert query and len(documents) == 2
                return [(1, 0.9), (0, 0.2)]

            async def fake_answer(**kwargs):
                assert kwargs["profile"].id == UUID(chat_id)
                return "正常"

            monkeypatch.setattr("knowledge_service.api.v1.admin.embed_texts", fake_embed)
            monkeypatch.setattr("knowledge_service.api.v1.admin.rerank_documents", fake_rerank)
            monkeypatch.setattr("knowledge_service.api.v1.admin.generate_answer", fake_answer)

            response = await client.post(f"admin/model-profiles/{embedding_id}/test")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["ok"] is True
            assert payload["kind"] == "embedding"
            assert payload["latency_ms"] >= 0
            assert "1536" in payload["message"]

            response = await client.post(f"admin/model-profiles/{rerank_id}/test")
            assert response.status_code == 200
            payload = response.json()
            assert payload["ok"] is True
            assert payload["kind"] == "rerank"
            assert "2 条" in payload["message"]

            response = await client.post(f"admin/model-profiles/{chat_id}/test")
            assert response.status_code == 200
            payload = response.json()
            assert payload["ok"] is True
            assert payload["kind"] == "chat"
            assert payload["message"] == "Chat 调用成功，模型回复：正常"

            # OCR 用生成的测试图调用视觉模型，成功时回显识别文字。
            response = await client.post(
                "admin/model-profiles",
                json={
                    "name": "连通性测试-ocr",
                    "kind": "ocr",
                    "provider": "qianfan",
                    "model_name": "paddleocr-vl-0.9b",
                    "settings": {
                        "base_url": "https://qianfan.baidubce.com/v2",
                        "chat_path": "/chat/completions",
                        "ocr_mode": "auto",
                    },
                },
            )
            assert response.status_code == 201, response.text
            ocr_id = response.json()["id"]
            monkeypatch.setattr(
                "knowledge_service.api.v1.admin.analyze_image",
                lambda *args, **kwargs: LayoutResult((), "OCR TEST 123"),
            )
            response = await client.post(f"admin/model-profiles/{ocr_id}/test")
            assert response.status_code == 200
            payload = response.json()
            assert payload["ok"] is True
            assert payload["kind"] == "ocr"
            assert "OCR TEST 123" in payload["message"]

            monkeypatch.setattr(
                "knowledge_service.api.v1.admin.analyze_image",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    OcrServiceError("OCR_TIMEOUT: 视觉 OCR 请求超时")
                ),
            )
            response = await client.post(f"admin/model-profiles/{ocr_id}/test")
            assert response.status_code == 200
            payload = response.json()
            assert payload["ok"] is False
            assert payload["error_code"] == "OCR_TIMEOUT"

            # 停用档案也允许测试，便于启用前先验证密钥和地址。
            await client.patch(f"admin/model-profiles/{chat_id}", json={"is_enabled": False})
            response = await client.post(f"admin/model-profiles/{chat_id}/test")
            assert response.status_code == 200
            assert response.json()["ok"] is True

            async def failing_embed(profile, texts):
                raise EmbeddingProviderError("EMBEDDING_PROVIDER_HTTP_401: Embedding 服务返回错误")

            monkeypatch.setattr("knowledge_service.api.v1.admin.embed_texts", failing_embed)
            response = await client.post(f"admin/model-profiles/{embedding_id}/test")
            # 诊断接口失败也返回 200，由 ok 和 error_code 表达。
            assert response.status_code == 200
            payload = response.json()
            assert payload["ok"] is False
            assert payload["error_code"] == "EMBEDDING_PROVIDER_HTTP_401"
            assert payload["message"] == "Embedding 服务返回错误"

            async def failing_chat(**kwargs):
                raise ChatProviderError("CHAT_TIMEOUT: 聊天模型请求超时")

            monkeypatch.setattr("knowledge_service.api.v1.admin.generate_answer", failing_chat)
            response = await client.post(f"admin/model-profiles/{chat_id}/test")
            assert response.status_code == 200
            payload = response.json()
            assert payload["ok"] is False
            assert payload["error_code"] == "CHAT_TIMEOUT"
            assert "密钥" not in response.text

            response = await client.post(f"admin/model-profiles/{uuid4()}/test")
            assert response.status_code == 404

            response = await client.post(
                f"admin/model-profiles/{embedding_id}/test",
                headers={"Authorization": "Bearer some-company-token"},
            )
            assert response.status_code == 403

    asyncio.run(scenario())


def test_admin_model_usage_lists_internal_models_and_scopes(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, factory):
            response = await client.get("admin/model-usage")
            assert response.status_code == 200, response.text
            slots = {slot["key"]: slot for slot in response.json()["slots"]}
            assert list(slots) == ["embedding", "rerank", "chat", "ocr"]

            embedding = slots["embedding"]["items"]
            assert [(item["name"], item["source"], item["scope_names"]) for item in embedding] == [
                ("已绑定-embedding", "knowledge_base", ["管理测试公司 / 占位子库"])
            ]
            # 未绑定且未设默认的 Rerank 档案不算在用。
            async with factory() as session:
                rerank_id = await session.scalar(
                    select(ModelProfile.id).where(ModelProfile.name == "备用-rerank")
                )
            assert slots["rerank"]["items"] == []

            # 设为默认后，新建知识库会沿用它，总览应显示为默认来源。
            response = await client.post(f"admin/model-profiles/{rerank_id}/default")
            assert response.status_code == 200
            response = await client.get("admin/model-usage")
            rerank = {slot["key"]: slot for slot in response.json()["slots"]}["rerank"]["items"]
            assert [(item["name"], item["source"], item["scope_names"]) for item in rerank] == [
                ("备用-rerank", "default", [])
            ]

            chat = slots["chat"]["items"]
            assert [item["source"] for item in chat] == ["environment"]
            assert chat[0]["model_name"] == "qwen-plus"
            assert chat[0]["is_active"] is True

            ocr = slots["ocr"]["items"][0]
            assert ocr["model_name"] == "qwen-vl-plus"
            assert ocr["provider"] == "dashscope"
            assert ocr["is_active"] is True

            # 绑定 Rerank 并设置默认 Chat 后，总览应同步反映实际调用模型。
            async with factory() as session:
                kb = await session.scalar(select(KnowledgeBase))
                kb.rerank_model_id = rerank_id
                await session.commit()

            response = await client.post(
                "admin/model-profiles",
                json={
                    "name": "默认生成模型",
                    "kind": "chat",
                    "provider": "dashscope",
                    "model_name": "qwen-flash",
                    "settings": {
                        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        "chat_path": "/chat/completions",
                    },
                },
            )
            assert response.status_code == 201, response.text
            chat_id = response.json()["id"]
            response = await client.post(f"admin/model-profiles/{chat_id}/default")
            assert response.status_code == 200

            response = await client.get("admin/model-usage")
            assert response.status_code == 200
            slots = {slot["key"]: slot for slot in response.json()["slots"]}
            rerank = slots["rerank"]["items"]
            assert [(item["name"], item["source"], item["scope_names"]) for item in rerank] == [
                ("备用-rerank", "knowledge_base", ["管理测试公司 / 占位子库"])
            ]
            chat = slots["chat"]["items"]
            assert [(item["name"], item["source"], item["is_active"]) for item in chat] == [
                ("默认生成模型", "default", True),
                ("环境兜底模型", "environment", False),
            ]

    asyncio.run(scenario())


def test_admin_model_presets_are_code_defined(monkeypatch, tmp_path):
    async def scenario():
        async with _admin_service(monkeypatch, tmp_path) as (client, _factory):
            response = await client.get("admin/model-presets")
            assert response.status_code == 200
            presets = {item["key"]: item for item in response.json()}
            assert set(presets) == {
                "openai_compatible",
                "dashscope",
                "bigmodel",
                "volcengine",
                "qianfan",
                "baidu",
                "deepseek",
            }
            assert presets["dashscope"]["supports_embedding"] is True
            assert presets["dashscope"]["known_models"]
            assert presets["deepseek"]["supports_embedding"] is False
            assert presets["dashscope"]["supports_chat"] is True
            assert presets["dashscope"]["chat_path"] == "/chat/completions"
            assert "qwen-plus" in presets["dashscope"]["known_chat_models"]
            # 各厂商的重排与 OCR 调用方式内置在预设中，管理页只需填模型名和密钥。
            assert presets["dashscope"]["supports_rerank"] is True
            assert presets["dashscope"]["rerank_path"] == "/reranks"
            assert "qwen3.7-text-rerank" in presets["dashscope"]["known_rerank_models"]
            assert presets["qianfan"]["supports_ocr"] is True
            assert presets["qianfan"]["ocr_mode"] == "auto"
            assert "paddleocr-vl-0.9b" in presets["qianfan"]["known_ocr_models"]
            assert presets["baidu"]["supports_ocr"] is True
            assert presets["baidu"]["ocr_mode"] == "baidu_async"
            assert presets["baidu"]["base_url"] == "https://aip.baidubce.com"
            assert presets["deepseek"]["supports_ocr"] is False
            assert presets["deepseek"]["supports_rerank"] is False

    asyncio.run(scenario())
