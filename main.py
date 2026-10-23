"""知识库服务的独立启动入口。"""

import sys
from pathlib import Path

# 允许直接执行项目根目录下的 main.py，无需先安装 editable package。
SRC_ROOT = Path(__file__).resolve().parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from fastapi import FastAPI  # noqa: E402

from knowledge_service.api.auth import require_service_auth  # noqa: E402
from knowledge_service.api.integration import include_knowledge_module  # noqa: E402
from knowledge_service.core.config import get_settings  # noqa: E402


def _mount_admin_ui(app: FastAPI) -> None:
    """管理页构建产物存在时挂载到 /admin-ui，根路径重定向过去。

    不挂在根路径：StaticFiles 会吞掉未匹配方法的路径，把 API 的 405 变成 404。
    """
    dist = Path(__file__).resolve().parent / "frontend" / "dist"
    if not (dist / "index.html").exists():
        return
    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    class _AdminUiStaticFiles(StaticFiles):
        async def get_response(self, path, scope):  # type: ignore[no-untyped-def]
            response = await super().get_response(path, scope)
            # Windows 下 StaticFiles 传入的 path 使用反斜杠，统一交给 Path 解析。
            if "assets" in Path(path).parts:
                # 构建产物文件名带内容哈希，可长期缓存。
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                # 入口 HTML 必须每次回源校验，否则重新构建后仍会引用已删除的旧 chunk。
                response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/admin-ui", _AdminUiStaticFiles(directory=dist, html=True), name="admin-ui")

    @app.get("/", include_in_schema=False)
    async def _admin_ui_index() -> RedirectResponse:
        return RedirectResponse(url="/admin-ui/")


def create_app() -> FastAPI:
    """创建独立运行的知识库 FastAPI 应用。"""
    settings = get_settings()
    app = FastAPI(
        title=settings.name,
        description="文档解析、分块、Embedding 和 Chroma 离线入库服务。",
        version="0.1.0",
    )
    # 独立服务统一校验凭据和公司权限；嵌入模式仍由宿主传入鉴权依赖。
    include_knowledge_module(
        app, api_prefix=settings.api_prefix, auth_dependency=require_service_auth
    )
    _mount_admin_ui(app)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
