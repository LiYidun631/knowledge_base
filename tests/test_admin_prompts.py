"""管理员提示词覆写接口验证：覆写 > 文件 > 内置默认。"""

import asyncio
import json
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge_service.core.config import Settings, get_settings
from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.session import get_db_session


def _admin_prompts_app(monkeypatch, settings, factory):
    import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app = main.create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    async def db_session():
        async with factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db_session] = db_session
    return app


def test_http_admin_prompt_override_cycle(monkeypatch, tmp_path) -> None:
    """提示词覆写可保存、生效、删除回退；未知名称与空正文被拒绝。"""

    async def scenario() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin_prompts.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        prompt_file = tmp_path / "system_prompt.txt"
        prompt_file.write_text("文件提示词，品牌 {{brand_name}}。\n", encoding="utf-8")
        settings = Settings(
            _env_file=None,
            auth_admin_token="admin-test-token",
            chat_brand_name="测试品牌",
            chat_system_prompt_file=Path(prompt_file),
        )
        # 提示词解析链路从 chat 模块读配置，同样指向测试设置。
        from knowledge_service.infrastructure.model_providers import chat

        monkeypatch.setattr(chat, "get_settings", lambda: settings)
        app = _admin_prompts_app(monkeypatch, settings, factory)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
        ) as anonymous:
            response = await anonymous.get("admin/prompts")
            assert response.status_code == 401

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1/",
            headers={"Authorization": "Bearer admin-test-token"},
        ) as client:
            # 初始为文件来源，占位符保持原样供管理页编辑。
            response = await client.get("admin/prompts")
            assert response.status_code == 200, response.text
            items = response.json()
            assert len(items) == 3
            assert [item["name"] for item in items] == [
                "knowledge_answer_system",
                "faq_generation",
                "faq_rewrite",
            ]
            assert items[0]["label"] == "问答生成系统提示词"
            assert items[0]["source"] == "file"
            assert items[0]["content"] == "文件提示词，品牌 {{brand_name}}。"
            assert items[0]["updated_at"] is None
            assert items[1]["source"] == "builtin"
            assert items[2]["source"] == "builtin"

            # 保存覆写后即时生效并带更新时间。
            override_text = "自定义提示词：{{brand_name}} 请简短回答。\n\n"
            response = await client.put(
                "admin/prompts/knowledge_answer_system",
                json={"content": override_text},
            )
            assert response.status_code == 200, response.text
            saved = response.json()
            assert saved["source"] == "override"
            assert saved["content"] == override_text.strip()
            assert saved["updated_at"] is not None

            response = await client.get("admin/prompts")
            assert response.json()[0]["source"] == "override"
            assert response.json()[0]["content"] == override_text.strip()

            # 生成链路读取覆写，并把品牌占位符替换为 .env 品牌名。
            from knowledge_service.infrastructure.model_providers import chat

            captured: dict[str, object] = {}

            def response_fn(request: httpx.Request) -> httpx.Response:
                captured.update(json.loads(request.content))
                return httpx.Response(
                    200, json={"choices": [{"message": {"content": "答案"}}]}
                )

            real_client = httpx.AsyncClient
            monkeypatch.setattr(chat, "get_settings", lambda: settings)
            monkeypatch.setattr(chat, "resolve_secret", lambda _ref: "test-key")
            monkeypatch.setattr(
                chat.httpx,
                "AsyncClient",
                lambda **kwargs: real_client(
                    transport=httpx.MockTransport(response_fn), **kwargs
                ),
            )
            async with factory() as session:
                answer = await chat.generate_answer(
                    query="问题", context="资料", session=session
                )
            assert answer == "答案"
            assert captured["messages"][0]["content"] == "自定义提示词：测试品牌 请简短回答。"

            # 删除覆写回退文件默认；重复删除幂等。
            response = await client.delete("admin/prompts/knowledge_answer_system")
            assert response.status_code == 204
            response = await client.delete("admin/prompts/knowledge_answer_system")
            assert response.status_code == 204
            response = await client.get("admin/prompts")
            assert response.json()[0]["source"] == "file"

            # FAQ 生成提示词覆写后，生成链路按名读取覆写内容。
            response = await client.put(
                "admin/prompts/faq_generation", json={"content": "FAQ 生成覆写提示词"}
            )
            assert response.status_code == 200
            captured_faq: dict[str, object] = {}

            def faq_response_fn(request: httpx.Request) -> httpx.Response:
                captured_faq.update(json.loads(request.content))
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"message": {"content": '[{"question": "问", "answer": "答"}]'}}
                        ]
                    },
                )

            monkeypatch.setattr(
                chat.httpx,
                "AsyncClient",
                lambda **kwargs: real_client(
                    transport=httpx.MockTransport(faq_response_fn), **kwargs
                ),
            )
            async with factory() as session:
                pairs = await chat.generate_faq_pairs(
                    context="资料", count=1, session=session
                )
            assert pairs == [{"question": "问", "answer": "答"}]
            assert captured_faq["messages"][0]["content"] == "FAQ 生成覆写提示词"

            # FAQ 改写提示词同样支持覆写并即时生效。
            response = await client.put(
                "admin/prompts/faq_rewrite", json={"content": "FAQ 改写覆写提示词"}
            )
            assert response.status_code == 200
            captured_rewrite: dict[str, object] = {}

            def rewrite_response_fn(request: httpx.Request) -> httpx.Response:
                captured_rewrite.update(json.loads(request.content))
                return httpx.Response(
                    200, json={"choices": [{"message": {"content": "改写后答案"}}]}
                )

            monkeypatch.setattr(
                chat.httpx,
                "AsyncClient",
                lambda **kwargs: real_client(
                    transport=httpx.MockTransport(rewrite_response_fn), **kwargs
                ),
            )
            async with factory() as session:
                rewritten = await chat.rewrite_faq_answer(
                    question="问", answer="答", session=session
                )
            assert rewritten == "改写后答案"
            assert captured_rewrite["messages"][0]["content"] == "FAQ 改写覆写提示词"

            # 删除 FAQ 覆写后回落到内置默认。
            response = await client.delete("admin/prompts/faq_generation")
            assert response.status_code == 204
            response = await client.delete("admin/prompts/faq_rewrite")
            assert response.status_code == 204
            response = await client.get("admin/prompts")
            by_name = {item["name"]: item for item in response.json()}
            assert by_name["faq_generation"]["source"] == "builtin"
            assert by_name["faq_rewrite"]["source"] == "builtin"

            # 未知提示词名称与空正文。
            response = await client.put(
                "admin/prompts/unknown_name", json={"content": "内容"}
            )
            assert response.status_code == 404
            response = await client.delete("admin/prompts/unknown_name")
            assert response.status_code == 404
            response = await client.put(
                "admin/prompts/knowledge_answer_system", json={"content": "   "}
            )
            assert response.status_code == 422
            response = await client.put(
                "admin/prompts/knowledge_answer_system",
                json={"content": "长" * 20001},
            )
            assert response.status_code == 422

    asyncio.run(scenario())
