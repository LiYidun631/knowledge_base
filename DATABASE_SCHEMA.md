# 数据库表结构设计

本文描述知识库作为 `test_agent` 内置模块后的 MySQL 元数据。数据库保存业务事实与任务状态，向量本体写入 Chroma 本地持久目录，原始文件默认存入宿主项目的本地目录；部署服务器时也可切换到 RustFS/S3。

## 核心表

| 表 | 主要字段 | 用途 |
| --- | --- | --- |
| `model_profiles` | `id`、`name`、`kind`、`provider`、`model_name`、`dimensions`、`settings`、`secret_ref`、`is_enabled`、`is_default` | Embedding/Rerank 档案；模型 URL 等普通参数保存于 `settings`，密钥只引用 `.env` 中的配置 |
| `knowledge_bases` | `id`、`name`、`description`、`embedding_model_id`、`rerank_model_id`、`models_locked_at`、`status` | 知识库元数据；名称全局唯一，模型创建后锁定 |
| `documents` | `id`、`knowledge_base_id`、`document_key`、最新文件名/版本号/状态 | 表示一个逻辑文档；同一知识库内 `document_key` 唯一 |
| `document_versions` | `id`、`document_id`、`version_number`、`object_key`、SHA-256、状态、`is_current` | 保存不可变上传版本；当前版本由唯一生成列约束 |
| `ingestion_jobs` | `id`、`document_id`、`document_version_id`、状态、阶段、尝试次数、错误摘要 | 记录离线入库处理和自动重试状态 |
| `document_chunks` | `id`、文档/版本 ID、父块 ID、序号、层级、文本、SHA-256、页码、metadata | 保存父子分块及来源信息；子块向量和文本另写入 Chroma |
| `outbox_events` | `id`、事件类型、聚合 ID、JSON payload、状态、尝试次数、可投递时间 | 与文档/任务在同一 MySQL 事务中创建，由应用进程内循环消费 |

## 遗留表保留

旧独立服务可能已经有 `projects`、`api_credentials`、`vector_store_profiles` 等表和历史数据。本轮不删除这些表、不清空历史行。迁移仅让新知识库记录不再依赖项目或 Milvus 档案外键，并把知识库名称改为全局唯一。历史 `project_id`、`vector_store_profile_id` 可空保留，用于审计旧数据；新建知识库不再写入它们。

若旧库不同项目中有同名知识库，迁移会主动失败，不自动改名或覆盖。请先备份数据库，再确认重复名称后手动处理。

## 模型绑定与密钥

Embedding 和 Rerank 的模型名、供应商、维度与 endpoint 等普通参数保存在 `model_profiles`。API Key 保存在部署环境 `.env` 的 `KNOWLEDGE_SERVICE_MODEL_API_KEYS` 中，档案使用 `secret_ref` 关联它。数据库与 API 响应均不保存或返回密钥明文。

建库时必须选择一个已启用的 Embedding，可选择已启用 Rerank 或显式关闭。创建后模型绑定不可修改。Chroma collection 以 Embedding 档案 ID 和维度作为隔离身份，避免模型向量空间混用。

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

outbox_events ──由 test_agent 应用进程内消费

Chroma：按模型档案分 collection，保存子块向量和 metadata
本地文件目录：按版本 object_key 保存原始文件（服务器可选 RustFS/S3）

历史关系（保留）：projects、api_credentials、vector_store_profiles
```
