# 可嵌入的知识库模块

本项目既支持独立启动验证，也支持嵌入 `E:\llmpython\test_agent`。独立运行时使用本项目 `.env` 和 `main.py`；嵌入宿主时由 `test_agent` 创建 FastAPI 应用、复用宿主普通业务鉴权和数据库配置，但公司凭据管理 API 始终要求管理员 Bearer。离线入库在应用进程内执行，向量持久化到 ChromaDB 本地目录；入库所需的服务端临时副本默认保存在本地目录，服务器部署时可切换到阿里云 OSS。

设计文档：[架构](ARCHITECTURE.md)、[数据库表结构](DATABASE_SCHEMA.md)、[接口文档](API_DOCUMENTATION.md)、[百度千帆 OCR 配置](OCR_HTTP_SERVICE.md)。

在线检索和问答已实现：默认组合 MySQL BM25 关键词召回与 Chroma 向量召回，再进行加权 RRF 融合；可选单独使用 BM25/向量，也支持知识库绑定的 Rerank 模型。`/ask` 会调用聊天模型生成答案，并保存来源快照和阶段日志。接口说明见 [在线检索设计与接口](ONLINE_RETRIEVAL_DESIGN.md)。

## 技术要求

- Python 3.11+
- 独立知识库项目使用 `uv` 管理依赖
- MySQL 8.0+
- 阿里云 OSS（可选，仅使用对象存储时需要）

## 独立启动验证

独立部署的管理员环境 Token `KNOWLEDGE_SERVICE_AUTH_ADMIN_TOKEN` 必须非空，否则业务请求返回 `503`（即使已有公司凭据），健康检查公开。公司 Token 不再从环境映射读取：服务端每请求查询 `company_credentials` 的 SHA-256 哈希，每家公司可按项目签发多枚凭据并独立重置或停用，无鉴权缓存。旧 `KNOWLEDGE_SERVICE_AUTH_COMPANY_TOKENS` 已彻底不再支持，已有公司需由管理员重新签发。

`main.py` 独立入口挂载 `require_service_auth`，业务请求携带 `Authorization: Bearer <Token>`。管理员可跨公司管理，创建公司、旧版 `POST /knowledge-bases` 及公司凭据管理仅限管理员；公司 Token 只能操作状态为 `active` 的本公司及其全部子库、文档、图片、QA 记录和公司大库，不是子库级权限。**同一公司的多枚凭据共享全部公司权限，按项目分开发放只便于独立撤销，不构成项目级数据隔离。** 缺失/无效/停用/被替换的 Token 返回 `401`，显式跨公司参数或归档公司旧有效凭据返回 `403`，外公司资源 ID 返回 `404`。

真实数据库迁移及实际 `.env` 更新仍待核对目标与备份，本文不表示已执行。确认 `KNOWLEDGE_SERVICE_DATABASE_URL` 指向预期数据库并备份后，再从 `E:\llmpython\knowledge_base` 执行以下部署步骤；迁移 `0025_multi_company_credentials` 会把 `0014_company_credentials` 建立的单凭据表升级为多凭据表，已有 Token 原样保留并标记为「默认凭据」，不需要客户端换 Token。

```powershell
uv run alembic upgrade head
uv run provision-knowledge-catalog --env
uv run python main.py
```

启动前配置非空管理员 Token。启动后由管理员调用 `POST /api/v1/companies` 取得公司 UUID（已有公司复用其 UUID），再通过下列接口按项目签发、重置或停用凭据；全部接口即使在嵌入模式也必须携带管理员 Bearer，公司 Token 不得管理本公司或其他公司的凭据：

| 操作 | 路径 | 结果 |
| --- | --- | --- |
| 列表 | `GET /api/v1/companies/{company_id}/tokens` | `200`；返回全部凭据（含已停用），不含哈希或明文 |
| 签发 | `POST /api/v1/companies/{company_id}/tokens`，体 `{"name": "项目标识"}` | `201`；仅 active 公司，同公司内项目名重复返回 `409` |
| 重置 | `POST /api/v1/companies/{company_id}/tokens/{credential_id}/reset` | `200`；替换该凭据并启用，仅 active 公司；未知凭据返回 `404` |
| 停用 | `DELETE /api/v1/companies/{company_id}/tokens/{credential_id}` | `204`；保留哈希，重复停用仍 `204`，未知凭据返回 `404`；允许撤销归档公司凭据 |

签发与重置返回 `{company_id, company_credential_id, name, token, token_type: "Bearer"}`；Token 为 `ks_` + `secrets.token_urlsafe(32)`（256 位随机），先 commit，再返回明文一次，响应含 `Cache-Control: no-store`、`Pragma: no-cache`，没有 GET 明文回读。请求或响应丢失需管理员重置该枚凭据，不要重试签发。重置/停用对后续新请求立即生效，无需重启服务端，不中断已在途请求；并发生命周期操作以 MySQL 公司行锁串行，最后成功写入的状态生效。完整契约见 [接口文档](API_DOCUMENTATION.md)。

跨机器调用建议 HTTPS；禁止将 Token、Authorization 头或含 Token 的响应写入日志。

服务监听 `.env` 中的 `KNOWLEDGE_SERVICE_HOST` 和 `KNOWLEDGE_SERVICE_PORT`：代码默认端口 `8000`，当前本机及模板端口为 `8333`，API 地址为 `http://127.0.0.1:8333/api/v1`，接口文档为 `/docs`。

### test_agent 远程接入

客户端不变：配置 `KNOWLEDGE_SERVICE_URL` 为上述实际 API 地址；`KNOWLEDGE_SERVICE_TOKEN` 必须填管理员按项目签发的对应公司 Token，严禁填管理员 Token。多个项目共用一家公司时，为每个项目单独签发凭据，便于只撤销其中一个；它们的数据权限仍完全相同。同时配置真实 `KNOWLEDGE_SERVICE_COMPANY_ID` 和 `KNOWLEDGE_SERVICE_KNOWLEDGE_BASE_IDS`（`source -> 子库 UUID` 映射），不要用占位 UUID。默认来源须有映射，并设置 `CHAT_KNOWLEDGE_RETRIEVAL_ENABLED=true`（默认关闭），修改后重启 `test_agent`；重置某枚凭据后须更新对应客户端 Token 并重启。

`test_agent` 只按 `source` 映射调用 `/knowledge-bases/{knowledge_base_id}/search?company_id={company_id}`，公司 ID 放 Query、子库 ID 放路径，不调用 `/ask`。列表 `GET /api/v1/knowledge-bases` 同样必填公司 UUID，只返回该公司 `active` 子库并先过滤后分页（`offset=0`、`limit=50`，最大 `200`）；管理员查询未知公司返回空数组，公司 Token 不得冒用其他公司 ID。

## 在宿主项目中运行

以下为内嵌兼容模式，不是远程接入步骤。`include_knowledge_module` 的普通业务路由仍由宿主提供鉴权；公司凭据签发/重置/停用 API 例外，即使嵌入也必须使用 `KNOWLEDGE_SERVICE_AUTH_ADMIN_TOKEN` 的管理员 Bearer，宿主免鉴权配置不能绕过。

以下命令从 `E:\llmpython\test_agent` 执行。宿主可以继续使用原有的 Anaconda 环境；只需把知识库依赖安装到该环境，并确认宿主 `.env` 的 `CHAT_DB_*` 指向目标 MySQL，先备份再迁移。

```powershell
# 以下命令在 test_agent 原有的 Anaconda 环境中执行
conda activate <test_agent环境名>
python -m pip install -e "E:\llmpython\test_agent[full]"
python -m alembic upgrade head
provision-knowledge-catalog --env
python main.py
```

默认使用本地文件存储。上传、解析、切分、Embedding 和 Chroma 写入由 FastAPI lifespan 管理，不需额外启动 Celery Worker、Redis 队列或 Outbox 派发器。知识库支持常见文本、PDF、Word、Excel、PowerPoint、RTF、HTML、CSV、JSON 和图片；扫描 PDF 和图片通过百炼视觉模型识别，可在库内 OCR 档案中切换千帆同步或百度官方异步 PaddleOCR，旧版 Office 文件需要 LibreOffice 转换。管理页的原文件预览内置 PDF、图片、文本、Markdown、HTML、Word、Excel/CSV 和 PPT 渲染；旧版 Office、RTF、ODF 与 TIFF 通过 `/documents/{id}/preview` 服务端转换，其中旧版 Office 同样依赖 LibreOffice（Windows 标准安装目录会自动探测，无需额外配置）。把 `KNOWLEDGE_SERVICE_OBJECT_STORAGE_BACKEND` 设为 `s3` 即改用阿里云 OSS：先在控制台创建私有桶，再按 `.env.example` 填写内网端点（`https://s3.oss-<region>-internal.aliyuncs.com`）、地域 ID 和 RAM 子账号密钥；适配器固定使用 OSS 兼容的 SigV2 + 虚拟主机寻址，不会自动建桶。

API 文档位于宿主应用的 `/docs`，知识库路由默认前缀为 `/api/v1`。接口详情、请求格式和任务状态见 [API_DOCUMENTATION.md](API_DOCUMENTATION.md)。

## 数据和备份

- MySQL 保存公司凭据哈希与启用状态、知识库、模型档案、文档版本、分块、任务与 Outbox；公司 Token 明文不能从数据库恢复。
- Chroma 向量文件默认位于 `test_agent/data/knowledge_chroma`。
- 服务端临时副本默认保存在 `KNOWLEDGE_SERVICE_STORAGE_ROOT` 指定的本地目录。
- 备份时同时保存 MySQL、Chroma 目录和服务端临时副本目录；如果使用 OSS，还需按 OSS 的备份策略保存对象存储。

首次执行迁移前确认 `CHAT_DB_*` 连接的是预期数据库，并先备份。旧项目/凭据/Milvus 档案表保留，迁移不删除历史数据。若旧项目之间有同名知识库，迁移会停止并提示人工处理。
# 公司级公共知识库服务

当前数据层级为：公司 `company` → 公司级向量大库 `vector_library` → 子库 `knowledge_base` → 文档和分块。项目接入时只调用 HTTP 接口，并在上传、检索、问答和删除向量请求中传递 `company_id` 和子库 ID；服务端负责归属校验和 Chroma 元数据过滤。大库必须先清空子库才能删除，后台原文件由后台系统继续管理。
