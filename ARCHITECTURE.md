# test_agent 内置知识库模块架构

状态：实现基线 v0.7
日期：2026-10-16

## 1. 建设目标

知识库作为可被多个项目调用的 Python 公共服务，统一以公司为租户边界。每家公司拥有一个向量大库，大库下创建多个子库；各项目调用接口时必须携带 `company_id` 和子库 ID。离线入库和在线混合检索均已实现，认证由部署层或宿主项目统一接入。

项目使用 Python 和 `uv` 管理依赖，不使用 Conda/Anaconda。参考 WeKnora 的模型绑定、文档版本与任务状态设计，参考 MD_Agent 的解析、结构化切分、父子块和向量索引能力。

## 2. 架构决策

1. **独立公共服务。** 知识库路由可独立启动，也可由 `test_agent` 等项目以 HTTP 调用；调用方不直接访问 MySQL 或 Chroma。
2. **任务随应用进程运行。** FastAPI lifespan 启动一个轻量异步 Outbox 消费循环。MySQL 保存事件、任务状态和重试时间；应用重启后继续消费未完成任务。不需要 Celery、Redis、单独 Worker 或 Outbox 派发器进程。
3. **ChromaDB 本地持久化。** Chroma 客户端在 Python 进程内惰性创建，向量文件写入 `.env` 指定的 `KNOWLEDGE_SERVICE_CHROMA_PERSIST_DIRECTORY`。不需要 Milvus、etcd 或 Attu。生产部署必须为该路径配置持久磁盘备份。
4. **MySQL 保存业务元数据。** 知识库、模型档案、文档逻辑记录、不可变版本、分块、入库任务和事务 Outbox 保存在 MySQL。知识库表与 `test_agent` 复用聊天数据库连接配置。
5. **本地目录保存服务端临时副本。** 当前默认写入 `.env` 中 `KNOWLEDGE_SERVICE_STORAGE_ROOT` 指定的持久目录，不需要启动 RustFS。部署到服务器后，可把后端切换为 RustFS/S3 兼容存储。
6. **公司级隔离。** `company_id` 是上传、检索、向量 metadata 和问答审计的统一边界；旧 project、credential、Milvus profile 表和数据暂作历史保留，不由这次迁移删除。
7. **建库时绑定模型。** 公司大库创建时自动绑定开发人员在 `.env` 中配置并初始化的默认 Embedding 和 Rerank，业务调用方不能选择或关闭模型；模型一经绑定即锁定。Chroma collection 按 Embedding 档案和维度隔离。
8. **配置集中在 `.env`。** URL、模型供应商参数和密钥引用从 `.env` 装载；数据库只存 `secret_ref`，不存 API Key。`.env.example` 提供中文说明和默认模板。
9. **删除边界明确。** 公司级大库只有在没有任何子库时才能删除；子库和文件向量删除会清理知识库侧索引及服务端临时副本，后台原文件由后台系统继续管理。

## 3. 逻辑架构

```text
test_agent FastAPI 进程
├── 原有聊天、报告和健康分析 API
├── 知识库 API（复用宿主认证）
├── 应用内异步任务循环
│   ├── MySQL Outbox / 状态 / 文档版本 / 文本分块
│   ├── 原文件：本地持久目录（默认）或 RustFS/S3
│   ├── Embedding：DashScope 或已登记的其他供应商
│   └── 向量：ChromaDB PersistentClient -> 本地持久目录
├── 在线检索：MySQL BM25 + Chroma 向量召回 -> 加权 RRF -> 可选 Rerank -> 父块与引用
└── 在线问答：检索结果 -> 聊天模型 -> 答案、来源快照和阶段日志
```

## 4. 离线入库流程

1. 上传接口校验公司、文件大小、扩展名和知识库状态，将入库所需的服务端临时副本存入本地目录或对象存储。
2. 同一 MySQL 事务中创建/更新逻辑文档，新增不可变文档版本、入库任务与 Outbox 事件。
3. 应用内消费循环领取事件，更新任务阶段，并从对象存储读取文件、校验 SHA-256。
4. 解析 TXT、Markdown、PDF、DOC/DOCX、XLS/XLSX、PPT/PPTX、RTF、HTML/XML、CSV、JSON、LOG、OpenDocument 和常见图片，按父子块方式切分。表格、段落、幻灯片、PDF 版面和 HTML 内嵌图片统一生成原文占位符与位置元数据；视觉识别文本只进入召回索引，用户答案按占位符还原图片，不输出图片说明。图片对象保存到本地目录或 RustFS。
5. 使用知识库绑定的 Embedding 配置分批请求向量。
6. 以文档版本为范围写入 Chroma；任务重试会清理该版本的部分结果，避免污染其他版本。
7. 向量写入和 MySQL 分块登记成功后，把新版本标记为当前版本，旧版本历史保留。
8. 瞬时失败按退避时间自动重试；达到上限后保留错误摘要，允许 API 手动重试。

跨格式标题识别、章节路径和分块/检索传递规则见[章节结构设计](DOCUMENT_HEADING_STRUCTURE_DESIGN.md)。当前 parser 对该方案只实现了部分标题信号，文档不应被理解为所有格式都已按此设计完成。

Chroma 与 MySQL 不能参与同一事务。若进程在 Chroma 写入后、MySQL 完成提交前退出，重试会清理同一版本向量再完整写入。HTTP 202 表示上传和任务登记成功，不表示入库已经结束。

## 5. 持久化边界

- **MySQL：** 知识库、模型配置、文档/版本、父子块、BM25 词频倒排表、任务与 Outbox 状态。
- **Chroma：** Embedding 向量、子块文本及检索 metadata，按模型档案和向量维度分 collection。
- **本地文件目录（默认）：** 入库所需的服务端临时副本和版面图片按版本使用独立 object key；服务器可选 RustFS/S3。
- **删除操作：** 文件向量删除会移除服务端临时副本和内部图片资产；后台文件系统中的原文件不由本服务删除。
- **`.env`：** 数据库连接来源、对象存储访问参数、模型 URL、密钥映射及切分/重试参数。

Chroma 目录是必须备份的业务数据。当前部署应采用单应用实例独占该目录；不要让多个服务器或多个并发应用副本共享同一个 Chroma SQLite 目录。需要横向扩容或多机共享时，再评估 Chroma Server 或服务端向量库。

## 6. 数据库调整策略

迁移 `0004_embedded_chroma` 将知识库名称约束调整为全局唯一，并把历史 `project_id`、`vector_store_profile_id` 改为可空，以便新结构不再要求这些概念。旧项目、密钥、向量档案及表结构保留，不执行 DROP 或历史数据清理。若不同旧项目已有同名知识库，迁移会停止并要求先处理名称冲突。

原有迁移已应用于数据库的场景，使用新的 Alembic revision `knowledge_alembic_version` 继续升级；任何迁移前都先备份数据库。嵌入到宿主 MySQL 时，迁移连接复用 `CHAT_DB_*`，不要误连曾用于独立原型的另一个库。

## 7. 项目依赖和启动

- `chromadb` 直接由 `test_agent` 的 `pyproject.toml` 管理。
- `knowledge_service` 模块源码位于宿主 `src/knowledge_service`。
- 应用仍只需启动原有 `python main.py` / PyCharm Run 配置；入库任务循环由应用 lifespan 启停。
- 需运行 Alembic 数据迁移，并登记默认 DashScope Embedding、Rerank 档案。
- 当前本地配置直接写入文件目录，不需要启动 RustFS；服务器改用 S3 时再部署 RustFS 或配置其他 S3 存储。
- Chroma 数据保存在 `.env` 的目录中，不能放在临时目录或容器易失层。

## 8. 在线检索

`POST /api/v1/knowledge-bases/{knowledge_base_id}/search` 默认并行执行 MySQL BM25 与 Chroma 向量召回，再按加权 RRF 融合；支持 `retrieval_mode=bm25|vector|hybrid` 和可选 Rerank。所有通道先按当前且已完成的文档版本过滤，最后返回去重父块和来源引用。BM25 中文使用字符二元组、英文和数字使用连续词项，不依赖额外检索服务。接口与配置详见 [在线检索设计文档](ONLINE_RETRIEVAL_DESIGN.md)。

## 9. 删除流程

删除单个文件向量时，服务端先校验 `company_id + knowledge_base_id + file_id`，再拒绝仍在入库的任务。清理顺序为：按文档版本删除 Chroma 向量、删除服务端临时副本和图片资产、删除 BM25 词项及分块、删除入库任务和 Outbox 事件，最后删除逻辑文档。外部清理失败时文档保留 `deleting` 状态，重复请求可以继续完成清理。

删除子库会对其中每个文档执行同一套清理流程，完成后删除子库记录。删除大库前只检查子库是否为空，不允许通过级联删除绕过子库清理；因此不会误删仍被管理的文件索引。

## 10. 当前范围

已落地知识库和文档 API、模型目录、文件上传、离线解析与切分、DashScope Embedding/OCR 调用、MySQL BM25 词项索引、Chroma 写入、混合检索、RRF、可选 Rerank、在线问答生成、问答来源快照、阶段日志、任务状态、自动重试、应用内任务循环、单文件向量删除、批量文件向量删除、子库删除和空大库删除。生产备份运维与真实模型端到端联调仍需结合部署环境完成。
