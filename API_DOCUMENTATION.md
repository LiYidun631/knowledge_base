# 知识库接口文档

版本：v0.4  
更新日期：2026-10-15

知识库 API 支持两种运行方式：独立运行 `knowledge_base` 时，默认地址为 `http://127.0.0.1:8000/api/v1`；嵌入 `test_agent` 时，使用宿主的 `APP_HOST`、`APP_PORT` 和 `/api/v1` 前缀。独立运行不启用鉴权；嵌入宿主后复用 `test_agent` 的 Bearer Token，当宿主 `.env` 的 `AUTH_ENABLED=false` 时无需 Token。没有独立项目 API Key 或 project 隔离。

知识库当前提供管理、文件上传和离线入库状态接口。在线向量检索接口尚未实现。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 知识库模块健康检查 |
| GET | `/model-presets` | 查询预置 Embedding 供应商模板 |
| POST | `/models` | 登记 Embedding 模型档案 |
| GET | `/models?kind=embedding\|rerank` | 查询可用模型档案 |
| POST | `/knowledge-bases` | 创建知识库并锁定模型 |
| GET | `/knowledge-bases` | 分页列出知识库 |
| GET | `/knowledge-bases/{knowledge_base_id}` | 查询知识库详情 |
| PATCH | `/knowledge-bases/{knowledge_base_id}` | 修改名称或说明 |
| DELETE | `/knowledge-bases/{knowledge_base_id}` | 归档知识库 |
| POST | `/knowledge-bases/{knowledge_base_id}/documents` | 上传文件并创建入库任务 |
| GET | `/knowledge-bases/{knowledge_base_id}/documents` | 分页列出逻辑文档 |
| GET | `/documents/{document_id}` | 查询文档详情 |
| GET | `/documents/{document_id}/versions` | 查询文档版本 |
| GET | `/documents/{document_id}/ingestion` | 查询最新版本入库状态 |
| POST | `/documents/{document_id}/retry` | 重试最新版本失败任务 |

## 健康检查

### `GET /health`

返回模块状态、模块名称和版本。它只检查应用是否能响应，不会验证 MySQL、文件存储或 Embedding 服务连接。

## 模型档案

### `GET /model-presets`

返回配置文件中的供应商模板，不包含任何 API Key。当前实现的 Embedding 适配器覆盖 DashScope、BigModel、火山引擎和百度千帆；DeepSeek 模板仅预留对话信息，不支持 Embedding。

### `POST /models`

从预置模板登记一个 Embedding 模型。请求只包含密钥引用，不提交密钥明文：

```json
{
  "name": "公司自建向量模型",
  "preset_name": "openai-text-embedding-3-small",
  "base_url": "https://embedding.example.com/v1",
  "model_name": "embedding-model-v1",
  "dimensions": 1024,
  "secret_ref": "custom_embedding"
}
```

`name`、`preset_name` 必填；`base_url`、`model_name`、`dimensions` 和 `secret_ref` 可覆盖模板默认值。实际密钥配置在 `.env` 的 `KNOWLEDGE_SERVICE_MODEL_API_KEYS`，其中的 `custom_embedding` 键应对应 `secret_ref`。同类型同名档案重复时返回 `409`；未知或不支持的模板返回 `422`。

### `GET /models`

返回已启用的模型档案，不返回密钥。可选 `kind=embedding` 或 `kind=rerank` 过滤。

默认模型由 `uv run provision-knowledge-catalog --env` 登记：Embedding 为 DashScope `text-embedding-v4`（1024 维），Rerank 为 `qwen3.7-text-rerank`。Rerank 当前只在建库时绑定，在线检索尚未实现。

## 知识库

### `POST /knowledge-bases`

创建逻辑知识库，不会新建 MySQL database 或 Chroma 文件库。请求示例：

```json
{
  "name": "健康知识库",
  "description": "健康科普资料",
  "embedding_model_id": "<可选的模型档案 UUID>",
  "rerank_model_id": null
}
```

`name` 必填，最多 160 字符，全库唯一。`embedding_model_id` 省略或为 `null` 时用当前默认 Embedding。`rerank_model_id` 省略时用默认 Rerank，显式传 `null` 时关闭。创建后 Embedding/Rerank 绑定立即锁定，即使文件尚未上传也不能更换。

成功返回 `201` 和知识库对象，含 `id`、名称、模型档案 ID、`models_locked_at`、状态和时间戳。名称冲突返回 `409`，所选模型不可用返回 `422`。

### `GET /knowledge-bases`

查询未归档知识库。支持 `offset`（默认 `0`）和 `limit`（默认 `50`，范围 `1–200`）。

### `GET /knowledge-bases/{knowledge_base_id}`

查询一个未归档知识库。不存在或已归档返回 `404`。

### `PATCH /knowledge-bases/{knowledge_base_id}`

至少传一个字段，只允许修改 `name`、`description`：

```json
{"name":"健康科普资料库","description":null}
```

`description: null` 可清空说明；模型 ID 字段不允许通过此接口更新。名称冲突返回 `409`。

### `DELETE /knowledge-bases/{knowledge_base_id}`

归档知识库，返回 `204`。此操作保留数据库记录、原始文件和 Chroma 向量；当前没有物理删除接口。归档后不能继续上传。

## 上传文件和离线入库

### `POST /knowledge-bases/{knowledge_base_id}/documents`

每次上传一个文件，使用原始二进制 body，不是 JSON、Base64 或 multipart。参数如下：

| 位置 | 名称 | 说明 |
| --- | --- | --- |
| 路径 | `knowledge_base_id` | 目标知识库 UUID |
| 查询 | `filename` | 文件名，需有支持的扩展名 |
| 查询 | `document_key` | 可选稳定文档标识；同一标识再次上传会新增版本 |
| 请求头 | `Content-Type` | 可选 MIME 类型 |
| 请求体 | 文件字节 | 非空原文件内容 |

支持扩展名、最大上传大小和文件存储配置见项目 `.env`。默认写入 `data/knowledge_files`；部署服务器时可配置 RustFS/S3。当前支持 TXT、Markdown、PDF、DOCX、HTML、CSV；扫描 PDF 和混合文本 PDF 的无文字页会调用百度千帆视觉模型；模型、接口地址和密钥配置见 [OCR_HTTP_SERVICE.md](OCR_HTTP_SERVICE.md)。

PowerShell 示例：

```powershell
$base = "http://127.0.0.1:8111/api/v1" # 按 APP_PORT 调整
$kbId = "<知识库 UUID>"
$token = "<仅当宿主 AUTH_ENABLED=true 时填写>"
$authHeader = @()
if ($token) { $authHeader = @("-H", "Authorization: Bearer $token") }
curl.exe -X POST "$base/knowledge-bases/$kbId/documents?filename=guide.md&document_key=guide-001" `
  -H "Content-Type: text/markdown" @authHeader --data-binary "@E:/documents/guide.md"
```

开启鉴权时传 `Authorization: Bearer <token>`；关闭鉴权时可去掉该请求头。上传成功返回 `202`，只表示原文件和任务已登记，后台仍在处理。再次上传相同 `document_key` 会创建递增版本，不覆盖历史对象。

### 查询文档和任务

- `GET /knowledge-bases/{knowledge_base_id}/documents?offset=0&limit=50`：分页查询知识库中的逻辑文档。
- `GET /documents/{document_id}`：查询逻辑文档及最新版本状态。
- `GET /documents/{document_id}/versions`：按版本号倒序返回历史版本；`is_current=true` 表示当前成功版本。
- `GET /documents/{document_id}/ingestion`：查询最新版本最近一次任务。
- `POST /documents/{document_id}/retry`：仅可重试最新版本的失败任务，返回 `200` 表示已重新排队。

任务状态为 `queued`、`running`、`completed` 或 `failed`。阶段为 `load`、`parse`、`chunk`、`embed`、`index`、`complete`。轮询建议每 2～5 秒一次；完成或失败后停止。

任务查询对象示例：

```json
{
  "id": "<任务 UUID>",
  "document_id": "<文档 UUID>",
  "document_version_id": "<版本 UUID>",
  "status": "running",
  "stage": "embed",
  "attempts": 1,
  "error_details": {},
  "started_at": "2026-10-14T02:01:10Z",
  "finished_at": null
}
```

## 常见 HTTP 状态

| 状态码 | 含义 |
| --- | --- |
| 201 | 知识库或模型档案创建成功 |
| 202 | 文件和入库任务已登记，任务仍在后台处理 |
| 204 | 知识库归档成功 |
| 400 | 请求头格式错误 |
| 401 | 宿主 Bearer Token 缺失或无效（仅开启鉴权时） |
| 404 | 知识库/文档不存在或知识库已归档 |
| 409 | 名称冲突、模型绑定锁定或任务不可重试 |
| 413 | 文件超过配置大小 |
| 415 | 文件扩展名不支持 |
| 422 | 参数或模型配置不合法 |

## 本地运行

独立验证时从 `E:\llmpython\knowledge_base` 执行，使用本项目 `.env` 的 `KNOWLEDGE_SERVICE_DATABASE_URL`；嵌入 `test_agent` 时使用宿主原有的数据库配置。迁移和模型初始化分别执行一次：

```powershell
uv run alembic upgrade head
uv run provision-knowledge-catalog --env
```

独立运行知识库时执行 `uv run python main.py`，默认访问 `http://127.0.0.1:8000/docs`；嵌入 `test_agent` 时启动宿主原有的 `main.py`，访问宿主的 `/docs`。入库任务循环随 FastAPI 应用自动启动，无需额外终端；当前本地文件存储不需要启动 RustFS。不要为此模块启动 Redis、Milvus、etcd 或 Celery。
