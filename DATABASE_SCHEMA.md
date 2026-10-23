# 数据库表结构设计

本文描述知识库作为 `test_agent` 内置模块后的 MySQL 元数据。数据库保存业务事实与任务状态，向量本体写入 Chroma 本地持久目录，入库所需的服务端临时副本默认存入本地目录；部署服务器时也可切换到 RustFS/S3。迁移 `0008_schema_comments_complete` 会为所有已知业务表写入中文表注释，便于在 DataGrip 等工具中识别。

## 核心表

| 表 | 主要字段 | 用途 |
| --- | --- | --- |
| `model_profiles` | `id`、`name`、`kind`、`provider`、`model_name`、`dimensions`、`settings`、`secret_ref`、`is_enabled`、`is_default` | Embedding/Rerank 档案；模型 URL 等普通参数保存于 `settings`，密钥只引用 `.env` 中的配置 |
| `companies` | `id`、`code`、`name`、`status` | 公共服务最外层租户，公司级数据隔离边界 |
| `company_credentials` | `id`、`company_id`、`name`、`token_hash`、`is_enabled`、`created_at`、`updated_at` | 每公司可有多枚按项目命名的凭据，仅存 SHA-256 哈希；每请求查库鉴权 |
| `vector_libraries` | `id`、`company_id`、`embedding_model_id`、`rerank_model_id`、`models_locked_at`、`status` | 每家公司唯一的大库，统一锁定向量模型 |
| `knowledge_bases` | `id`、`company_id`、`vector_library_id`、`name`、`description`、`embedding_model_id`、`rerank_model_id`、`models_locked_at`、`status` | 公司大库下的子库；模型继承大库 |
| `documents` | `id`、`company_id`、`knowledge_base_id`、`document_key`、最新文件名/版本号/状态 | 表示一个逻辑文档；同一子库内 `document_key` 唯一，三级目录信息写入 metadata |
| `document_versions` | `id`、`document_id`、`version_number`、`object_key`、SHA-256、状态、`is_current` | 保存知识库服务端临时副本和不可变版本；当前版本由唯一生成列约束 |
| `ingestion_jobs` | `id`、`document_id`、`document_version_id`、状态、阶段、尝试次数、错误摘要 | 记录离线入库处理和自动重试状态 |
| `document_chunks` | `id`、文档/版本 ID、父块 ID、序号、层级、文本、`token_count`、SHA-256、页码、metadata | 保存父子分块及来源信息；`token_count` 用于 BM25 长度归一化 |
| `document_chunk_terms` | 子块 ID、词项、词频、文档 ID、版本 ID | BM25 倒排词项表；中文保存字符二元组，英文/数字保存词项 |
| `outbox_events` | `id`、事件类型、聚合 ID、JSON payload、状态、尝试次数、可投递时间 | 与文档/任务在同一 MySQL 事务中创建，由应用进程内循环消费 |
| `qa_sessions` | `id`、知识库 ID、状态、标题、metadata | 可选的在线问答会话，调用方传入 `session_id` 后自动创建 |
| `qa_records` | 请求 ID、问题、答案、检索模式、状态、模型、耗时、错误摘要 | 每次在线问答的主记录和审计摘要 |
| `qa_sources` | 问答 ID、排名、文档/版本/块 ID、正文快照、检索分数 | 保存生成答案实际使用的来源，原文删除后仍可追溯 |
| `qa_events` | 问答 ID、阶段、状态、耗时、错误代码、details | 保存请求、检索、生成等阶段日志 |

删除子库或文件向量时，MySQL 中会清理对应的 `documents`、`document_versions`、`ingestion_jobs`、`document_chunks`、`document_chunk_terms`、`document_assets` 和未发布 `outbox_events`。`qa_sources` 保存回答时的正文快照，文档删除后其外键按约束置空，历史问答审计仍然保留。后台原文件不属于本服务的业务表，也不会因这些删除操作被删除。

### 表的直观含义

| 表 | 用大白话理解 |
| --- | --- |
| `model_profiles` | 模型通讯录：记录使用哪家平台、模型名称、向量维度和密钥引用。 |
| `knowledge_bases` | 知识库目录：记录知识库名称，以及创建时绑定的 Embedding/Rerank 模型。 |
| `documents` | 文件目录：同一个业务文件的逻辑记录，重复上传会增加版本。 |
| `document_versions` | 文件版本仓库：每次上传的原文件地址、大小、哈希和当前状态。 |
| `ingestion_jobs` | 入库进度表：记录解析、分块、Embedding、索引等阶段和失败原因。 |
| `document_chunks` | 文本分块表：保存父块、子块和页码，便于审计和后续检索。 |
| `document_chunk_terms` | 关键词索引：记录每个子块里各个词出现几次，供 MySQL 计算 BM25。 |
| `document_assets` | 图片资产：保存图片对象地址、召回说明、原文占位符 `marker`、位置 JSON、页码和区域坐标。 |
| `outbox_events` | 待处理事件表：保证上传成功后，应用重启也能继续执行入库任务。 |
| `qa_sessions` | 问答会话目录：可将多次问题归到同一个会话。 |
| `qa_records` | 问答流水账：记录用户问题、模型答案、状态、耗时和错误。 |
| `qa_sources` | 答案依据：保存回答生成时的文档片段和检索分数快照。 |
| `qa_events` | 处理日志：记录检索和大模型生成分别是否成功、耗时多少。 |

## 公司凭据（`company_credentials`）

| 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | `Uuid`，主键 | 凭据 ID，重置/停用按此定位；同一公司可多行 |
| `company_id` | `Uuid`，非空、外键 → `companies.id`，`ON DELETE CASCADE`，普通索引 | 凭据所属公司；物理删除公司会级联删除全部凭据 |
| `name` | `String(120)`，非空 | 项目/用途标识，同一公司内唯一（`uq_company_credentials_company_name`）；不改变数据权限边界 |
| `token_hash` | `String(64)`，非空、全局唯一 | 公司 Token 的 SHA-256 十六进制哈希，不存明文 |
| `is_enabled` | 布尔，非空 | 是否启用；停用只改状态，保留哈希与记录 |
| `created_at` | UTC 时间戳，非空 | 凭据创建时间 |
| `updated_at` | UTC 时间戳，非空 | 凭据最近更新时间 |

同一公司可签发多枚凭据（凭据数量不设上限），每枚按 `name` 标识项目或调用方，可独立签发、重置、停用，互不影响。**多枚凭据只区分接入身份，权限仍以公司为边界**：任一启用凭据都可访问本公司全部子库、文档、图片、QA 记录和公司大库，不做项目级数据隔离。

公司 Token 为 `ks_` + `secrets.token_urlsafe(32)`，随机部分为 256 位。管理员环境配置 `KNOWLEDGE_SERVICE_AUTH_ADMIN_TOKEN` 仍保留，不存入此表；非空是独立服务业务鉴权的前提，否则返回 `503`，健康检查公开。公司 Token 鉴权每请求查表，无鉴权缓存；无效、停用或被替换的 Token 返回 `401`，归档公司旧有效凭据返回 `403`。归档公司不会删除凭据。

凭据 API 即使嵌入宿主也必须使用管理员 Bearer，公司 Token 不能管理本公司或其他公司的凭据：

- `GET /api/v1/companies/{company_id}/tokens`：列出公司全部凭据（含停用），不含哈希或明文；未知公司返回 `404`，归档公司可查询。
- `POST /api/v1/companies/{company_id}/tokens`，体 `{name}`：仅 active 公司可签发，返回 `201`；未知/归档公司 `404`；同公司内名称重复返回 `409`。
- `POST /api/v1/companies/{company_id}/tokens/{credential_id}/reset`：仅 active 公司且凭据存在可重置，覆盖该行哈希并启用，返回 `200`；未知公司或凭据返回 `404`。
- `DELETE /api/v1/companies/{company_id}/tokens/{credential_id}`：停用并保留哈希，返回 `204`，重复停用仍 `204`；无公司/凭据返回 `404`，允许停用归档公司的凭据。

签发/重置先 commit 再返回 `{company_id, company_credential_id, name, token, token_type: "Bearer"}`，明文仅返回一次；响应含 `Cache-Control: no-store`、`Pragma: no-cache`，无 GET 明文回读。请求或响应丢失需管理员重置该凭据，不要重试签发。生命周期操作以 MySQL 公司行锁串行，最后成功写入的状态生效；后续新请求立即使用最新状态，不中断已在途请求。跨机器建议 HTTPS；禁止将 Token、Authorization 头或含 Token 响应写入日志。

### 迁移边界

迁移 `0014_company_credentials` 的父版本为 `0013_document_asset_markers`，只创建空单凭据表及其约束，不为已有公司自动生成凭据，也不自动导入旧环境映射。迁移 `0025_multi_company_credentials` 的父版本为 `0024_qa_faq_matched`，把 `company_id` 主键升级为独立 `id` 主键并新增 `name`；已有凭据原地保留为「默认凭据」，旧 Token 继续有效。MySQL 使用原生 `ALTER TABLE`，其他方言重建表；降级时每公司只保留一枚（优先启用且最新）凭据。旧 `KNOWLEDGE_SERVICE_AUTH_COMPANY_TOKENS` 已彻底不再支持，管理员须为依赖旧映射的公司重新签发凭据。该表与历史 `api_credentials` 无自动迁移关系，下文历史描述保留。

真实数据库迁移及实际 `.env` 更新仍待核对目标与备份，不能将迁移文件已实现视为已在目标库执行。确认连接和备份后再按部署流程执行迁移、由管理员签发，并安全交付给调用方。

## 遗留表保留

旧独立服务可能已经有 `projects`、`api_credentials`、`vector_store_profiles` 等表和历史数据。本轮不删除这些表、不清空历史行。迁移仅让新知识库记录不再依赖项目或 Milvus 档案外键，并把知识库名称改为全局唯一。历史 `project_id`、`vector_store_profile_id` 可空保留，用于审计旧数据；新建知识库不再写入它们。

若旧库不同项目中有同名知识库，迁移会主动失败，不自动改名或覆盖。请先备份数据库，再确认重复名称后手动处理。

## 在线问答记录和日志

`qa_records` 保存业务可查询的问答结果；`qa_sources` 保存来源文本快照和当时的检索分数；`qa_events` 保存请求、检索、生成阶段的状态与耗时。API Key、系统提示词正文和供应商原始响应不会写入数据库。问答失败时仍会保存失败状态和错误摘要，便于排查模型超时、检索失败和配置错误。

`POST /knowledge-bases/{knowledge_base_id}/ask?company_id=...` 返回的 `request_id` 用于排查；详情接口使用问答记录 ID 查询来源和阶段日志。问答会话、记录、来源和阶段日志均保存 `company_id`，便于按公司审计。

## 模型绑定与密钥

Embedding 和 Rerank 的模型名、供应商、维度与 endpoint 等普通参数保存在 `model_profiles`。API Key 保存在部署环境 `.env` 的 `KNOWLEDGE_SERVICE_MODEL_API_KEYS` 中，档案使用 `secret_ref` 关联它。数据库与 API 响应均不保存或返回模型 API Key 明文；公司凭据的一次性签发/重置响应见上文。

公司大库创建时由服务端自动绑定 `.env` 配置并初始化的默认 Embedding 和 Rerank，调用方不能选择或关闭。创建后模型绑定不可修改。Chroma collection 以 Embedding 档案 ID 和维度作为隔离身份，避免模型向量空间混用。

## 文档版本和任务状态

重新上传同一个 `document_key` 会创建新版本并保留旧版本对象。解析、切分、Embedding、Chroma 写入及 MySQL 文本块登记全部成功后，才将该版本标记为当前版本。失败时保留旧当前版本。

Outbox 事件和任务状态同属 MySQL 事务。应用 lifespan 启动单个异步消费协程，失败时按 `available_at` 退避重试；进程重启后待处理事件仍在数据库中。Chroma 写入不可参与 MySQL 事务，写入中断的版本在安全重试时先按 `document_version_id` 清理部分向量。

## 数据关系

```text
model_profiles (embedding / rerank)
        │
        └──< knowledge_bases ──< documents ──< document_versions
                                               ├──< ingestion_jobs
                                               └──< document_chunks (父子块)
                                                     ├──< document_chunk_terms (BM25 词频)
                                                     └──< document_assets (图片资产)

knowledge_bases ──< qa_sessions ──< qa_records ──< qa_sources
                              └────< qa_records ──< qa_events

companies ──1── vector_libraries ──< knowledge_bases
companies ──0..*── company_credentials（id 主键，company_id 外键 + 索引，物理删除 CASCADE）
companies ──< documents（冗余 company_id，用于隔离校验）

outbox_events ──由 test_agent 应用进程内消费

Chroma：按模型档案分 collection，保存子块向量和 metadata
本地文件目录：按版本 object_key 保存服务端临时副本和 assets/ 下的图片（服务器可选 RustFS/S3）

历史关系（保留）：projects、api_credentials、vector_store_profiles
```

## DataGrip 中看到的 Chroma 表

如果连接的是 `data/knowledge_chroma/chroma.sqlite3`，看到的 `collections`、`segments`、`embeddings`、`embedding_metadata`、`tenants`、`databases` 等表属于 ChromaDB 内部实现，不是业务表，也不需要手工修改。MySQL 的 `document_chunk_terms` 是 BM25 业务索引，迁移 `0006_bm25_terms` 会为已有子块补建词频；Chroma 向量仍通过检索接口访问。
