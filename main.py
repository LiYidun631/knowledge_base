"""知识库服务的独立启动入口。"""

import sys
from pathlib import Path

# 允许直接执行项目根目录下的 main.py，无需先安装 editable package。
SRC_ROOT = Path(__file__).resolve().parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from fastapi import FastAPI  # noqa: E402

from knowledge_service.api.integration import include_knowledge_module  # noqa: E402
from knowledge_service.core.config import get_settings  # noqa: E402


def create_app() -> FastAPI:
    """创建独立运行的知识库 FastAPI 应用。"""
    settings = get_settings()
    app = FastAPI(
        title=settings.name,
        description="文档解析、分块、Embedding 和 Chroma 离线入库服务。",
        version="0.1.0",
    )
    # 独立运行时不挂载宿主鉴权；嵌入 test_agent 时由宿主传入鉴权依赖。
    include_knowledge_module(app, api_prefix=settings.api_prefix)
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
