# 知识库服务验收测试文档

适用范围：**核心链路 —— 文档入库 → 在线检索 → 问答生成 → FAQ**。管理端配置、多租户凭据全生命周期、统计成本与角色审计只作为链路前置条件出现，不做逐项验收。

配套文件：用例明细见 [TEST_CASES.md](TEST_CASES.md)。接口契约以 [API_DOCUMENTATION.md](API_DOCUMENTATION.md) 为准，表结构以 [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) 为准，检索算法以 [ONLINE_RETRIEVAL_DESIGN.md](ONLINE_RETRIEVAL_DESIGN.md) 为准。本文与它们冲突时，以那三份文档和代码为准，并回来修订本文。

---

## 1. 文档目的

给出一份可执行的验收方案：在什么环境、用什么数据、按什么顺序跑哪些用例、每条用例的通过判据是什么、哪些结论能由已有自动化测试直接给出、哪些必须在真实环境手工确认。

用例表里的每条断言都对应代码中的具体行为，不是泛化的测试模板。自动化映射列指向 `tests/` 下真实存在的测试标识，可直接跳转复核。

## 2. 测试范围

| 在范围内 | 不在范围内（本轮不验收） |
| --- | --- |
| 文件上传、版本创建与替换 | 公司/凭据管理页的完整 CRUD 交互 |
| 多格式离线解析与版面还原 | 模型档案、单价、提示词的界面细节 |
| OCR 触发条件与三供应商路径 | 审计日志导出、成本核算口径 |
| 父子切块参数与聚合规则 | 全局搜索、任务中心的界面呈现 |
| 向量写入、清理与失败重试 | 检索评测集的指标定义（仅用其结果） |
| BM25 / 向量 / 混合检索与 RRF、Rerank | 生产环境的容量规划与压测 |
| `/ask` 全部生成路径与问答记录落库 | 嵌入式宿主（`test_agent`）的鉴权改造 |
| `/ask/stream` 流式与异常终止 | |
| FAQ 生成、改写与高置信直出 | |
| 反馈与待补资料闭环 | |
| 版本回滚、文档删除对链路的影响 | |

## 3. 被测系统与核心链路

服务端 FastAPI，数据访问 SQLAlchemy async，元数据在 MySQL，向量在 ChromaDB 本地持久化目录，原始文件在服务端副本或对象存储。入库在应用进程内执行，无独立消息队列。

```
POST /documents  ──▶ document + document_version + ingestion_job(queued) + outbox 事件
                          │
                     进程内 worker 认领 → stage: load → parse → chunk → embed → index → complete
                          │                  │
                          │                  ├─ 无文字层的 PDF 页 / 旧版 Office → OCR / LibreOffice 转换
                          │                  └─ 父子切块（库级参数可覆盖全局）
                          ├─▶ Chroma 集合（按 Embedding 档案分集合，cosine）
                          ├─▶ MySQL 分块行 + BM25 词项行（同一事务）
                          └─▶ completed 后写 outbox：document.faq_generation_requested → FAQ 自动生成

POST /search ──▶ BM25 召回 + 向量召回 → RRF 融合 → Rerank（若绑定）→ top_k → 可选上下文扩展
POST /ask    ──▶ FAQ 高置信直出 → 否则检索 → 无结果 / 型号澄清 / 图片全节直出 → 否则聊天模型生成
                     └─▶ qa_records + qa_sources + qa_events（阶段日志、token 用量、耗时）
PUT /qa-records/{id}/feedback ──▶ 点踩自动转 review_status=pending（待补资料）
```

链路上必须核对的持久化对象：

| 对象 | 关注字段 |
| --- | --- |
| `documents` / `document_versions` | `status`（`queued`/`processing`/`completed`/`failed`/`deleting`）、`latest_version_number`、`is_current` |
| `ingestion_jobs` | `status`（`queued`/`running`/`completed`/`failed`/`cancelled`）、`stage`、`attempts`、`error_code` |
| 分块与词项表 | 父块/子块数量、页码、章节路径、块类型 |
| Chroma 集合 | 集合名由 Embedding 档案 ID 与维度派生，只保留 `is_current` 版本可被检索 |
| `qa_records` | `status`（`running`/`success`/`no_results`/`failed`）、`review_status`（`none`/`pending`/`resolved`）、`faq_matched`、`feedback`、三段时间、三段 token |
| `qa_sources` / `qa_events` | 命中内容与上下文快照、四个分数、阶段日志 |
| `faq_entries` | 问题向量、库级/文档级归属、是否随当前版本 |

## 4. 测试环境

### 4.1 依赖

| 依赖 | 要求 | 缺失时的可观测后果 |
| --- | --- | --- |
| Python | 3.11+（`.python-version` 已固定） | — |
| 依赖管理 | `uv`（dev 组含 `pytest`、`aiosqlite`、`ruff`） | — |
| MySQL | 8.0+，已执行 `alembic upgrade head` | 迁移未到位时表结构断言失败 |
| ChromaDB | 本地持久化目录（`KNOWLEDGE_SERVICE_CHROMA_PERSIST_DIRECTORY`） | 向量检索返回 503 `Chroma 向量检索暂不可用` |
| LibreOffice | `KNOWLEDGE_SERVICE_OFFICE_CONVERTER_PATH` 指向 `soffice.exe`，留空则从 PATH 查找 | 旧版 `.doc/.ppt/.xls` 解析失败；预览接口 503 |
| 模型 | Embedding / Rerank / Chat / OCR 档案或环境变量 | Embedding 缺失 503；Rerank 调用失败 502 且**不降级**；Chat 失败 502 |
| 对象存储 | 可选，`KNOWLEDGE_SERVICE_OBJECT_STORAGE_BACKEND` | 本地副本模式即可验收 |
| 调用端 | Apifox 或 curl；管理端页面 `frontend/` | — |

### 4.2 关键配置项与默认值

验收时**逐项记录实际取值**，很多用例的预期结果随这些值变化。

| 变量 | 默认 | 影响 |
| --- | --- | --- |
| `KNOWLEDGE_SERVICE_AUTH_ADMIN_TOKEN` | 必须非空 | 独立部署下为空则业务请求一律 503（健康检查仍公开） |
| `KNOWLEDGE_SERVICE_MAX_UPLOAD_BYTES` | 50 MiB | 超限 413 |
| `KNOWLEDGE_SERVICE_ALLOWED_DOCUMENT_EXTENSIONS` | 30 项白名单 | 不在白名单 415 |
| `KNOWLEDGE_SERVICE_PARENT_CHUNK_CHARS` / `CHILD_CHUNK_CHARS` / `CHILD_CHUNK_OVERLAP_CHARS` | 1800 / 600 / 80 | 约束：overlap < child、parent ≥ child |
| `KNOWLEDGE_SERVICE_MAX_INGESTION_ATTEMPTS` / `INGESTION_RETRY_BASE_SECONDS` | 3 / 15 | 瞬时错误指数退避 `15 × 2^(attempts-1)` |
| `KNOWLEDGE_SERVICE_SEARCH_MAX_TOP_K` | 20 | 请求 `top_k` 超过即 422（字段本身允许 1–100） |
| `KNOWLEDGE_SERVICE_SEARCH_RRF_K` / `VECTOR_WEIGHT` / `BM25_WEIGHT` / `CANDIDATE_K` | 60 / 0.7 / 0.3 / 40 | 混合融合结果排序；两权重之和必须 > 0 |
| `KNOWLEDGE_SERVICE_RERANK_TIMEOUT_SECONDS` | 30 | 超时 → 502，无降级 |
| `KNOWLEDGE_SERVICE_CHAT_TIMEOUT_SECONDS` / `CHAT_TEMPERATURE` | 60 / 0.2 | 模型调用不重试，失败 → 502 |
| `KNOWLEDGE_SERVICE_FAQ_MATCH_THRESHOLD` | 0.85（`gt 0, le 1`） | FAQ 直出的相似度门槛 |
| `KNOWLEDGE_SERVICE_QA_BADCASE_RERANK_THRESHOLD` | 0.6（`gt 0, le 1`） | 最佳 rerank 分低于此值 → 自动标记待补资料 |

### 4.3 启动与自检

从 `E:\llmpython\knowledge_base` 执行：

```powershell
uv run alembic upgrade head
uv run provision-knowledge-catalog --env
uv run python main.py
```

服务监听 `KNOWLEDGE_SERVICE_HOST:KNOWLEDGE_SERVICE_PORT`，本机当前端口 `8333`，基址 `http://127.0.0.1:8333/api/v1`。

```powershell
curl http://127.0.0.1:8333/api/v1/health
# 期望 200 且 {"status":"ok","service":"knowledge-service","version":"0.1.0"}
```

自动化基线（不需要真实 MySQL/模型，用 `aiosqlite` 与打桩，可离线跑）：

```powershell
uv run pytest -q                     # 基线：330 passed，约 94 秒，全程离线（aiosqlite + 打桩）
uv run pytest tests/test_faq.py -q   # 单模块
uv run ruff check .
```

并发跑多个 `pytest` 时必须指定不同 `--basetemp`，共用临时目录会产生 `PermissionError` 假失败。

### 4.4 调用约定

- 所有业务接口带 `Authorization: Bearer <Token>`。管理端接口用管理员凭据，业务接口用公司凭据。
- `company_id` 一律放 **Query**，子库 ID 放 **路径**；缺失或跨公司分别是 422/403。
- Token 值只写在客户端环境变量里，**不得出现在用例记录、截图或日志中**。
- 上传接口不是 multipart：文件字节直接作为请求体，元数据全部走 Query 参数，MIME 走 `Content-Type` 头。

## 5. 测试策略与分层

| 层 | 手段 | 覆盖对象 | 执行时机 | 通过标准 |
| --- | --- | --- | --- | --- |
| L1 自动化 | `uv run pytest`（330 例，核心链路 37 个文件 266 例） | 解析规则、切块算法、状态机、鉴权分支、请求校验、落库字段 | 每次改代码后、合并前 | 全绿；新增行为必须补用例 |
| L2 接口回归 | Apifox/curl 打真实服务 | 真实 MySQL + Chroma + 真实模型下的端到端契约 | 每轮验收、模型或配置变更后 | P0 全通过，P1 通过率 ≥ 95% |
| L3 管理端走查 | `frontend/` 页面 | 上传、目录、切块预览、原文件预览、问答测试页、待补资料 | L2 之后 | 主流程无阻断，界面数据与接口一致 |
| L4 业务场景 | 以调用方视角（`health_manager_ai` 只调 `/search`，不调 `/ask`） | 真实产品资料 + 真实用户问题集 | 上线前 | 准确率与badcase 达到第 6.3 节口径 |

分层原则：能被 L1 稳定断言的行为不在 L2 重复设计为 P0，L2 只保留「真实模型输出质量」和「跨组件一致性」这两类必须由真实环境证明的判据。用例表的自动化映射列就是这条原则的落地记录。

## 6. 准入与准出标准

### 6.1 准入（Entry Criteria）

1. `alembic upgrade head` 成功，且目标库已备份。
2. 管理员 Token、公司 Token 已签发并可访问 `/admin/me`、`/companies`。
3. Embedding / Rerank / Chat / OCR 四类档案在管理端「模型配置」连通性测试通过。
4. 健康检查 200，`/docs` 可访问。
5. 测试库为独立子库，不与生产资料混用。

### 6.2 准出（Exit Criteria）

| 项 | 判据 |
| --- | --- |
| P0 用例 | 100% 执行且通过 |
| P1 用例 | 执行率 100%，通过率 ≥ 95%，失败项均有缺陷单且非链路阻断 |
| 缺陷 | 无未闭环的 S1/S2；S3 及以下有明确排期 |
| 自动化 | `uv run pytest` 全绿，本轮新增缺陷均有回归用例 |
| 数据一致性 | 抽查 5 条问答记录：接口返回、`qa_records`、`qa_sources`、`qa_events` 四处内容一致 |
| 留痕 | 执行记录表填写完整（环境、配置取值、每轮起止时间、结果） |

### 6.3 缺陷分级与问答质量口径

| 级别 | 定义 | 示例 |
| --- | --- | --- |
| S1 阻断 | 链路不可用或数据被破坏 | 上传后任务永不停留在 `queued`；检索命中已删除文档 |
| S2 严重 | 主流程错误结果或错账 | 旧版本内容仍被检索到；FAQ 直出答案与库内文本不符 |
| S3 一般 | 次要功能或体验缺陷 | 预览转换失败但原文件可下载 |
| S4 轻微 | 文案、排版 | 状态标签命名不一致 |

答案质量按问题集人工评定三档：`正确` / `部分正确（可上线，标注待优化）` / `错误`。业务场景 PPT 口径：**错误答案 ≤ 5%，无结果率与待补资料量作为趋势指标记录**，不设为阻断项。

## 7. 测试数据准备

### 7.1 文档样本矩阵

| 样本 | 目的 | 期望解析行为 |
| --- | --- | --- |
| 带文字层的原生 PDF（含标题编号、跨页章节、表格、双栏） | 版面主路径 | **不调用 OCR**（`auto` 模式）；标题层级形成章节路径；表格独立成块；双栏按阅读顺序 |
| 纯扫描件 PDF（图片页，≥1 页含表格） | 触发 OCR | 仅缺文字层的页进入 OCR；页码保留 |
| >10 页扫描 PDF | 千帆分批 | `paddleocr` 路径按每批 ≤10 页提交 |
| `.docx`（编号标题 + 编号正文步骤 + 表格内嵌图片） | 标题推断边界 | 编号正文步骤**不得**被识别为标题；表格图片留在原单元格 |
| `.doc` / `.ppt` / `.xls` 旧版 | 转换器依赖 | 经 LibreOffice 转换后解析；无转换器时报错且任务 `failed` |
| `.xlsx`（多 sheet）、`.pptx`（多页） | 表格与幻灯 | sheet 名与行、每页文字保留 |
| `.md`（Setext 标题、代码围栏含竖线） | Markdown 边界 | 代码围栏内竖线**不**被当表格；围栏内井号不当标题 |
| `.html`（含 `<script>`、`<br>`、重复表格单元） | HTML 清洗 | 脚本剔除、单元格不重复、`<br>` 保留换行 |
| `.epub` | 书脊顺序 | 按 spine 顺序成节 |
| `.csv` / `.json` / 长 `.txt` | 结构化文本 | CSV 每行保留表头；JSON 美化；纯文本按上限切分 |
| 单张大图（产品操作示意图 PNG） | 图片资产 | 生成图片资产，检索命中带 `assets`，答案内可渲染 |
| 同名 `document_key` 的第二版（内容不同） | 版本与回滚 | 版本号 +1，成功后才切换 `is_current` |

### 7.2 问题集

每个子库准备不少于 30 问，覆盖 6 类，每类都要有可判定答案：

| 类别 | 数量 | 用于用例 |
| --- | --- | --- |
| FAQ 原文改写问法（同义、口语化） | ≥6 | K-05、K-06、K-07 |
| 明确答案在文档某页某节（含页码、型号可核对） | ≥8 | G、I 全部 |
| 需要跨节/跨页汇总（操作步骤类） | ≥4 | I-09、G-11 |
| 库内确实没有的内容 | ≥4 | I-05、L-02 |
| 涉及多型号且问题未指明型号 | ≥3 | I-07、I-08、H-05 |
| 触发图片全节直出的用法类问题 | ≥3 | I-10 |

### 7.3 租户与库结构

1. 管理员建公司 → 记 `company_id`。
2. 建公司级向量大库（绑定指定 Embedding/Rerank，并测一次「锁定模型」）。
3. 建两个子库：`KB-A` 用全局切块默认；`KB-B` 覆盖为 `parent 1200 / child 400 / overlap 50`。同一份长文档同时入库两库，用于 E 组对比。
4. 为公司签发一枚测试专用 Token（用后停用），名称写清用途，便于独立撤销。

## 8. 自动化基线与覆盖映射

[TEST_CASES.md](TEST_CASES.md) 共 **120 条用例（P0 44 / P1 58 / P2 18）**，其中 11 条无自动化保证、1 条只能人工评定答案口径。

核心链路相关 37 个测试文件、266 例；其余 64 例覆盖管理端配置、迁移与运维。按模块：

| 用例模块 | 测试文件（例数） |
| --- | --- |
| A 链路准入与鉴权 | `test_service_auth.py`(46)、`test_health.py`(1) |
| B 上传与版本创建 | `test_document_upload.py`(5)、`test_document_list_labels.py`(1)、`test_source_download.py`(5) |
| C 离线解析 | `test_offline_parsing.py`(39) |
| D OCR 与扫描件 | `test_ocr_client.py`(30)、`test_ingestion_flow.py`(6) |
| E 切块与父子块 | `test_chunk_aggregation.py`(9)、`test_kb_chunk_params.py`(6)、`test_chunk_preview.py`(1)、`test_chunk_defaults.py`(1) |
| F 向量写入与清理 | `test_chroma_writer.py`(6)、`test_embedding_provider.py`(11)、`test_outbox_delivery.py`(3) |
| G 在线检索 | `test_online_search.py`(7)、`test_bm25_migration.py`(1)、`test_vector_library_query.py`(1) |
| H 产品与型号过滤 | `test_product_filter.py`(9) |
| I 问答 `/ask` | `test_chat_provider.py`(18)、`test_qa_audit.py`(3) |
| J 流式问答 | `test_chat_provider.py` 内 6 个 `stream_qa_events_*`、`test_qa_stream_failure.py`(1) |
| K FAQ | `test_faq.py`(11) |
| L 反馈与待补资料 | `test_feedback.py`(2) |
| M 回滚与删除 | `test_document_version_activate.py`(3)、`test_document_deletion.py`(2)、`test_document_batch_delete.py`(2)、`test_library_deletion.py`(3)、`test_document_preview.py`(5)、`test_document_file.py`(1)、`test_document_folders.py`(2)、`test_kb_archive_restore.py`(3) |
| 数据契约辅助 | `test_mysql_compatibility.py`(6)、`test_knowledge_base_policy.py`(4)、`test_default_embedding_choice.py`(5) |
| 检索评测回归 | `test_evaluation.py`(4) |
| 问答记录管理端 | `test_admin_qa_records.py`(3) |

上表逐格相加即 266 例，与 `uv run pytest --collect-only -q` 的核心链路文件计数一致；全量 330 例中另外 64 例属于管理端配置、凭据、审计与迁移。

### 8.1 自动化覆盖不到的部分（必须真实环境手工确认）

| 缺口 | 为什么 L1 测不了 | 手工判据 |
| --- | --- | --- |
| 真实模型答案的事实正确性与话术 | L1 全部打桩，只断言调用形状 | 问题集人工三档评定（6.3） |
| 真实 Embedding 的相似度分布（0.85 阈值是否合适） | 桩返回固定向量 | 观察 `faq_matched` 命中率与误命中率 |
| 真实 Rerank 分数分布（0.6 badcase 线） | 分数为构造值 | 看 `qa_sources.rerank_score` 分布与待补资料量 |
| LibreOffice 转换在目标机上的实际可用性 | 转换器不在 CI 里 | 旧版 Office 上传→预览→下载全链 |
| OCR 供应商真实返回的表格/标题还原质量 | HTTP 层打桩 | 扫描页与纸质件逐页比对 |
| 图片在最终答案中的可访问性（`KNOWLEDGE_SERVICE_PUBLIC_BASE_URL`） | 需外部可达的基址 | 浏览器打开答案里的图片 URL |
| 客户端断开后的记录终态 | 需真实 SSE 中断 | `qa_records.status` 与 `error_code` |
| Chroma 目录跨机迁移/权限 | 需真实目录 | 重启服务后检索仍命中 |

## 9. 手工执行规范

- 每轮从干净子库开始；同一批样本重复使用时，在记录表注明是第几轮替换。
- 断点核对顺序固定：接口响应 → `documents`/`document_versions` → `ingestion_jobs` → 切块/词项 → Chroma 命中 → `qa_records`。任何一环与接口不一致即为缺陷线索。
- 只读核对 SQL（改 `:kb` 后使用，禁止在验收中执行任何写操作）：

  ```sql
  SELECT d.filename, d.status AS doc_status, v.version_number, v.is_current, v.status AS ver_status
  FROM documents d JOIN document_versions v ON v.document_id = d.id
  WHERE d.knowledge_base_id = :kb ORDER BY d.created_at DESC;

  SELECT status, stage, attempts, error_code, updated_at
  FROM ingestion_jobs WHERE knowledge_base_id = :kb ORDER BY created_at DESC LIMIT 20;

  SELECT status, review_status, faq_matched, feedback, result_count,
         retrieval_duration_ms, generation_duration_ms, total_duration_ms,
         total_tokens, chat_model_name, error_code
  FROM qa_records WHERE knowledge_base_id = :kb ORDER BY created_at DESC LIMIT 20;
  ```
- 耗时与 token 只在 L4 场景统计，L2 不以此判定通过与否；`/ask` 的 FAQ 直出记录中检索与生成两段耗时均为 0，属预期。
- 每轮结束提交留痕：环境快照（配置取值表）、用例结果、缺陷单号、当轮样本清单。

## 10. 风险与已知限制

| 风险 | 说明 | 应对 |
| --- | --- | --- |
| 入库为进程内执行 | 服务重启会中断在途任务，靠陈旧任务回收与 `attempts` 重试恢复 | 验收中专门跑一次「入库中途重启服务」，确认任务回到 `queued` 并最终 `completed` |
| Rerank 失败不降级 | 直接 502，问答随之失败 | 记录为已知行为；如需降级另立需求 |
| 同一公司多枚凭据共享全部权限 | 按项目签发不等于数据隔离 | 权限用例只验证公司级隔离，不验证项目级隔离 |
| 旧版 Office 依赖外部转换器 | 无 LibreOffice 时该类文档全链路不可用 | 环境前置项，缺失时用例标记为阻塞而非失败 |
| FAQ 直出与低分待补依赖阈值 | 阈值变动会显著改变命中与待补量 | 每轮记录 `KNOWLEDGE_SERVICE_FAQ_MATCH_THRESHOLD` / `QA_BADCASE_RERANK_THRESHOLD` 实际值 |
| 向量大库绑定模型后锁定 | 已绑定档案不可改，删除返回 409 | 建库时就决定 Embedding，避免中途换模型导致全库重入库 |

## 11. 执行记录模板

| 轮次 | 日期 | 环境/分支 | 配置差异 | 用例范围 | P0 通过 | P1 通过 | 缺陷 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| R1 | | master @ commit | | A/B/C/E/G/I/K | | | | |
| R2 | | | | D/F/H/J/L/M | | | | |
| R3（回归） | | | | 缺陷项 + 全 P0 | | | | |

缺陷单最少记录：编号、级别、用例编号、复现请求（脱敏）、期望/实际、涉及表或接口、回归结论。

## 12. 相关文档

[README.md](README.md) · [API_DOCUMENTATION.md](API_DOCUMENTATION.md) · [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [ONLINE_RETRIEVAL_DESIGN.md](ONLINE_RETRIEVAL_DESIGN.md) · [OCR_HTTP_SERVICE.md](OCR_HTTP_SERVICE.md) · [TEST_CASES.md](TEST_CASES.md)
