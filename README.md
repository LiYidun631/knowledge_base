# 可嵌入的知识库模块

本项目既支持独立启动验证，也支持嵌入 `E:\llmpython\test_agent`。独立运行时使用本项目 `.env` 和 `main.py`；嵌入宿主时由 `test_agent` 创建 FastAPI 应用、复用宿主鉴权和数据库配置。离线入库在应用进程内执行，向量持久化到 ChromaDB 本地目录；原始文件默认保存在本地目录，服务器部署时可切换到 RustFS/S3。

设计文档：[架构](ARCHITECTURE.md)、[数据库表结构](DATABASE_SCHEMA.md)、[接口文档](API_DOCUMENTATION.md)、[百度千帆 OCR 配置](OCR_HTTP_SERVICE.md)。

## 技术要求

- Python 3.11+
- 独立知识库项目使用 `uv` 管理依赖
- MySQL 8.0+
- RustFS（可选，仅使用 S3 对象存储时需要）

## 独立启动验证

从 `E:\llmpython\knowledge_base` 执行：

```powershell
uv run alembic upgrade head
uv run provision-knowledge-catalog --env
uv run python main.py
```

服务默认监听 `.env` 中的 `KNOWLEDGE_SERVICE_HOST` 和 `KNOWLEDGE_SERVICE_PORT`，接口文档为 `/docs`。

## 在宿主项目中运行

以下命令从 `E:\llmpython\test_agent` 执行。宿主可以继续使用原有的 Anaconda 环境；只需把知识库依赖安装到该环境，并确认宿主 `.env` 的 `CHAT_DB_*` 指向目标 MySQL。

```powershell
# 以下命令在 test_agent 原有的 Anaconda 环境中执行
conda activate <test_agent环境名>
python -m pip install -e "E:\llmpython\test_agent[full]"
python -m alembic upgrade head
provision-knowledge-catalog --env
python main.py
```

默认使用本地文件存储，无需启动 RustFS。上传、解析、切分、Embedding 和 Chroma 写入由 FastAPI lifespan 管理，不需额外启动 Celery Worker、Redis 队列或 Outbox 派发器。扫描 PDF 的无文字页面通过知识库专属配置调用百度千帆视觉模型逐页识别。仅当把 `KNOWLEDGE_SERVICE_OBJECT_STORAGE_BACKEND` 设为 `s3` 时，才需要启动 RustFS 或配置其他 S3 服务：

```powershell
docker compose --env-file E:\llmpython\test_agent\.env -f compose.infra.yaml up -d rustfs
```

API 文档位于宿主应用的 `/docs`，知识库路由默认前缀为 `/api/v1`。接口详情、请求格式和任务状态见 [API_DOCUMENTATION.md](API_DOCUMENTATION.md)。

## 数据和备份

- MySQL 保存知识库、模型档案、文档版本、分块、任务与 Outbox。
- Chroma 向量文件默认位于 `test_agent/data/knowledge_chroma`。
- 原始文件默认保存在 `KNOWLEDGE_SERVICE_STORAGE_ROOT` 指定的本地目录。
- 备份时同时保存 MySQL、Chroma 目录和原始文件目录；如果使用 S3，还需备份对应对象存储。

首次执行迁移前确认 `CHAT_DB_*` 连接的是预期数据库，并先备份。旧项目/凭据/Milvus 档案表保留，迁移不删除历史数据。若旧项目之间有同名知识库，迁移会停止并提示人工处理。
