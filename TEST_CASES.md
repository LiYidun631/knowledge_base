# 核心链路验收用例表

配套说明见 [TESTING.md](TESTING.md)。本表覆盖入库 → 检索 → 问答 → FAQ 全链路，以及链路成立所必需的准入与一致性用例。

## 编号与约定

- **优先级**：`P0` 契约核心或阻断项，失败即停线；`P1` 主功能正确性；`P2` 边界、异常与体验。
- **占位符**：`{company}` 公司 UUID，`{kb}` 子库 UUID，`{doc}` 文档 UUID，`{ver}` 版本 UUID，`{qa}` 问答记录 UUID，`<公司Token>` / `<管理员Token>` 为对应凭据值。
- **通用前置**：服务已启动且 `/api/v1/health` 返回 200；已完成 7.3 节的租户与两个子库（`KB-A` 全局切块、`KB-B` 覆盖切块）；四类模型档案连通性正常；业务用例默认使用 `POST /api/v1/knowledge-bases/{kb}/...?company_id={company}` 形态。
- **自动化列**：`文件名.py::用例名` 表示该行为已被 L1 断言，本轮无需手工重复；仅写文件名（含例数）表示覆盖在该文件内、具体分支请打开文件核对；`手工` 表示只能真实环境确认；`缺口` 表示既无自动化也无法打桩，需要补测或新增用例。

## A. 链路准入与鉴权

| 编号 | P | 用例 | 步骤 | 预期结果 | 自动化 |
| --- | --- | --- | --- | --- | --- |
| A-01 | P0 | 未配置管理员 Token 时服务失败关闭 | 独立部署下将 `KNOWLEDGE_SERVICE_AUTH_ADMIN_TOKEN` 置空后请求任一业务接口 | 业务接口 `503`，且**未打开数据库连接**；`GET /health` 仍 `200` | `test_service_auth.py::test_unconfigured_service_fails_closed_before_database`、`test_health.py::test_health_check_returns_service_status` |
| A-02 | P0 | 缺失或无效 Bearer | 分别去掉 `Authorization`、填随机串、填已停用凭据 | 三次均 `401`，且不落任何业务记录 | `test_service_auth.py::test_missing_or_invalid_token_is_rejected_before_database` |
| A-03 | P0 | 公司 Token 跨公司冒用 | 用 A 公司 Token 请求 `company_id={B公司}` 的子库检索 | `403`；不返回任何片段 | `test_service_auth.py::test_company_token_cannot_spoof_company_or_admin_permissions` |
| A-04 | P0 | 公司 Token 访问他人资源 ID | 用本公司 Token 请求外公司 `{kb}` / `{doc}` / `{qa}` | 统一 `404`，不泄露存在性 | `test_service_auth.py::test_company_token_cannot_access_foreign_resources` |
| A-05 | P1 | 只读角色可用问答与检索 | 以 `viewer` 管理员凭据依次调 `/search`、`/ask`、`/ask/stream` | 三者放行（只产生查询记录）；写配置接口 `403` | 缺口（`auth.py` 的 `_VIEWER_POST_SUFFIXES` 无独立路由用例） |
| A-06 | P1 | 公司 Token 不得管理凭据 | 用公司 Token 调 `GET/POST /companies/{company}/tokens` | `403` | `test_service_auth.py::test_company_cannot_manage_tokens` |

## B. 文档上传与版本创建

| 编号 | P | 用例 | 步骤 | 预期结果 | 自动化 |
| --- | --- | --- | --- | --- | --- |
| B-01 | P0 | 上传返回 202 并建立任务 | 以请求体原始字节上传一份带文字层 PDF，Query 带 `company_id`、`filename`、`file_id=DOC-K1`、`Content-Type` | `202` + `DocumentRead`（`latest_version_number=1`、`status=queued`）；`document_versions` 新增 v1、`ingestion_jobs` 新增 `queued`、Outbox 写入 `document.ingestion_requested` | `test_ingestion_flow.py::test_reupload_only_switches_current_version_after_success` |
| B-02 | P0 | 同 `document_key` 重传即新版本 | 用同一 `file_id=DOC-K1` 上传内容不同的第二版 | 仍 `202`；`latest_version_number=2`；`(knowledge_base_id, document_key)` 唯一约束不产生第二条文档行；v1 在 v2 成功前仍为 `is_current` | `test_document_list_labels.py::test_document_list_labels_and_version_replacement` |
| B-03 | P0 | 文件名与后缀校验 | ① 扩展名不在白名单（如 `.exe`）② `filename` 传空 ③ `filename` 含客户端路径 `C:\a\b.pdf` | ① `415`，detail `File extension .exe is not allowed`；② `422`；③ 落库名被规整为 `b.pdf` | `test_document_upload.py::test_normalize_filename_removes_client_path`、`test_document_upload.py::test_normalize_filename_rejects_empty_name` |
| B-04 | P0 | 空文件与超限 | ① 请求体 0 字节 ② 上传 > `KNOWLEDGE_SERVICE_MAX_UPLOAD_BYTES`（默认 50 MiB） | ① `422` `Uploaded file is empty`；② `413` `Uploaded file exceeds the configured size limit`，且超限即停止读取 | `test_document_upload.py::test_read_limited_upload_stops_when_limit_is_exceeded` |
| B-05 | P1 | 缺少必填 Query | ① 不传 `company_id` ② 不传 `filename` | 均 `422`，不产生文档行 | `test_document_upload.py`（文件级） |
| B-06 | P1 | 标签与目录随上传写入 | 上传时带 `product_names`、`model_names`、`source_directory_id`、`folder_id` | 文档列表与后续检索过滤读到这些标签；非法产品名/超长被拒 | `test_product_filter.py::test_register_document_stores_product_metadata` |
| B-07 | P1 | 文档正在删除时禁止上传 | 对 `status=deleting` 的文档重传同 `file_id` | 拒绝并提示「该文档正在删除，请稍后再上传」 | 缺口 |
| B-08 | P1 | 归档子库拒绝上传 | 归档 `{kb}` 后向其上传 | 拒绝（与 M 组归档语义一致），不建任务 | `test_kb_archive_restore.py::test_archive_hides_from_list_and_restore_recovers` |
| B-09 | P2 | URL 入库与 SSRF 防护 | `POST .../documents/from-url` 分别指向内网地址、重定向到内网、重定向环、超大/空内容 | 内网与重定向到私网被拒；重定向环被拒；超限与空内容被拒；公网正常返回字节并建版本 | `test_source_download.py`（5 例全绿） |
| B-10 | P2 | 本地副本路径穿越 | 检查服务端副本目录结构与含 `../` 的文件名 | 副本写入被限制在存储根内，穿越被拒 | `test_document_upload.py::test_local_object_storage_round_trip_and_rejects_traversal` |

## C. 离线解析

| 编号 | P | 用例 | 步骤 | 预期结果 | 自动化 |
| --- | --- | --- | --- | --- | --- |
| C-01 | P0 | 原生 PDF 不走 OCR | 入库带文字层 PDF，`ocr_mode=auto` | 解析日志无 OCR 调用；文字层完整；页码与章节路径正确 | `test_offline_parsing.py::test_text_pdf_does_not_call_vision_model_in_auto_mode` |
| C-02 | P0 | 章节路径进入可检索块 | 入库多级标题文档后查切块表 | 每个可检索块都带章节路径；索引文本含章节路径 | `test_ingestion_flow.py::test_index_text_contains_section_path_for_each_retrievable_block` |
| C-03 | P0 | 旧版 Office 依赖转换器 | 上传 `.doc`（LibreOffice 可用/不可用两种环境） | 可用：转换后解析成功；不可用：任务 `failed` 且错误指向转换器，不静默产出空内容 | `test_offline_parsing.py::test_legacy_doc_requires_office_converter` |
| C-04 | P1 | docx 段落与表格顺序 | 上传「段落→表格→段落」交叉的 docx | 顺序与原文一致，表格内容归属正确章节 | `test_offline_parsing.py::test_docx_preserves_paragraph_table_order_and_section` |
| C-05 | P1 | 标题编号识别 | 上传含 `1.`、`1.1`、`第三章` 等编号标题的 docx | 编号标题形成嵌套章节；多种编号格式均识别 | `test_offline_parsing.py::test_docx_heading_number_formats_are_supported` |
| C-06 | P1 | 编号步骤不被当标题 | 上传含 `1. 开机 2. 连接` 正文步骤列表的 docx / md | 步骤**不**被推断为章节标题，仍留在所属节内 | `test_offline_parsing.py::test_docx_numbered_steps_are_not_headings`、`test_markdown_numbered_steps_are_not_inferred_as_sections` |
| C-07 | P1 | 表格转块 | 上传含 Markdown 表格的 md、含 `<table>` 的 html、多 sheet 的 xlsx | 表格成为独立块并带表头；代码围栏内竖线不误判为表格；html 单元格不重复；xlsx 保留 sheet 名与行 | `test_offline_parsing.py::test_markdown_table_becomes_table_block_with_header`、`test_markdown_code_fence_pipes_are_not_tables`、`test_html_removes_scripts_and_avoids_duplicate_table_cells`、`test_xlsx_preserves_sheet_name_and_rows` |
| C-08 | P1 | PDF 矢量表格与插图 | 入库含矢量表格/插图的 PDF | 表格文字恢复且不重复；双栏按阅读顺序；纯文字页不触发视觉模型检测 | `test_offline_parsing.py::test_native_pdf_recovers_vector_table_and_drops_duplicate_text`、`test_native_pdf_reads_two_column_layout_in_reading_order`、`test_pure_text_page_skips_vision_image_detection` |
| C-09 | P1 | 表格内图片归属 | 上传表格单元格内嵌图片的 docx | 图片资产留在该表格块内，不漂移到别处 | `test_offline_parsing.py::test_docx_table_image_stays_in_its_cell` |
| C-10 | P1 | 其余格式基线 | 依次入库 `pptx` / `epub` / `csv` / `json` / 长 `txt` / `md(Setext)` / `html(<br>)` | 幻灯按页、epub 按书脊、csv 每行带表头、json 美化、长文本按上限切分、Setext 识别且围栏内井号不当标题、`<br>` 保留换行 | `test_offline_parsing.py`（`test_pptx_preserves_slide_text`、`test_epub_parses_spine_order_and_sections`、`test_csv_split_keeps_header_and_every_row`、`test_json_is_pretty_printed_for_retrieval`、`test_markdown_recognizes_setext_and_ignores_code_fence_headings`、`test_html_br_keeps_line_breaks`） |
| C-11 | P1 | 图片 OCR 失败隔离 | 文档内多数图片正常、一张触发供应商错误 | 单图失败不阻断整篇入库；其余内容完整 | `test_offline_parsing.py::test_docx_images_use_bounded_concurrency_and_isolate_failures` |
| C-12 | P2 | 白名单可配置 | 通过 `KNOWLEDGE_SERVICE_ALLOWED_DOCUMENT_EXTENSIONS` 增删扩展名后重启 | 配置生效；默认集合覆盖常见办公与图片格式 | `test_offline_parsing.py::test_default_extensions_include_common_formats` |
| C-13 | P2 | 解析并发不串页 | 入库多页扫描/图文混排 PDF | 页码与页面归属在并发下仍正确，无跨页串文 | 缺口 |
| C-14 | P2 | 重复入库幂等 | 对同一 `document_key` 连续重传 3 版 | 版本号 1→2→3，旧版本向量与词项不残留在检索结果 | `test_online_search.py::test_bm25_filters_old_versions_and_optional_document_scope` |

## D. OCR 与扫描件

| 编号 | P | 用例 | 步骤 | 预期结果 | 自动化 |
| --- | --- | --- | --- | --- | --- |
| D-01 | P0 | 无文字层页触发 OCR | 入库整篇扫描件 PDF | 缺文字层的页进入 OCR，识别文本入库且页码保留 | `test_offline_parsing.py::test_pdf_without_text_requires_ocr`、`test_scanned_pdf_uses_qianfan_and_keeps_page_number` |
| D-02 | P0 | 未配置 OCR 时明确失败 | 清空 OCR 档案与 `KNOWLEDGE_SERVICE_OCR_MODEL_NAME`，入库扫描件 | 任务 `failed`，错误含 `OCR_REQUIRED: 请在模型管理中配置默认 OCR 模型…`；**不自动重试** | `test_ocr_client.py::test_model_or_api_key_must_be_configured_before_http` |
| D-03 | P1 | 千帆路径与分批 | `ocr_mode=paddleocr`，入库 12 页扫描件 | 专用识别端点被调用；超过 10 页自动分批；返回 Markdown 且缺失页有明确报错 | `test_ocr_client.py::test_paddleocr_pdf_batches_over_ten_pages`、`test_paddleocr_recognize_pdf_pages_returns_markdown`、`test_paddleocr_pdf_missing_pages_raises` |
| D-04 | P1 | 百度异步路径 | `ocr_mode=baidu_async`，入库扫描件 | 先提交任务再轮询直至完成；标题层级、表格、图片块映射正确；AK/SK 缺失时不发请求 | `test_ocr_client.py::test_baidu_async_image_submits_polls_and_maps_layout`、`test_baidu_async_converts_titles_tables_and_images`、`test_baidu_async_requires_ak_and_sk_before_http` |
| D-05 | P1 | OCR 供应商错误不外泄 | 制造 4xx/5xx、空返回、业务错误码、任务失败 | 报错信息不含响应正文与密钥；空响应被判无效 | `test_ocr_client.py::test_http_error_does_not_expose_response_body_or_api_key`、`test_empty_model_response_is_rejected`、`test_baidu_async_task_failure_surfaces_provider_error`、`test_baidu_async_business_error_code_is_rejected` |
| D-06 | P1 | 版面块顺序 | 入库多栏排版的扫描页 | 块按栏顺序输出，标题层级可成嵌套章节 | `test_ocr_client.py::test_layout_blocks_are_ordered_by_columns`、`test_offline_parsing.py::test_ocr_heading_blocks_form_nested_sections` |
| D-07 | P2 | 档案优先于环境 | 同时配置 .env 与库内默认 OCR 档案，取值不同 | 运行时档案覆盖环境变量；档案缺密钥时在发请求前报错 | `test_ocr_client.py::test_runtime_profile_overrides_environment_for_image`、`test_runtime_profile_without_key_raises_before_http` |
| D-08 | P2 | 异步轮询超时 | 人为拖长 OCR 任务（或调小 `OCR_ASYNC_TIMEOUT_SECONDS`） | 到达总超时后任务 `failed` 并带可读错误码，不永久挂起 | 缺口 |

## E. 切块与父子块

| 编号 | P | 用例 | 步骤 | 预期结果 | 自动化 |
| --- | --- | --- | --- | --- | --- |
| E-01 | P0 | 全局默认参数生效 | `KB-A`（不覆盖）入库长文档，查切块预览 | 父块约 ≤1800、子块约 ≤600、重叠 80；父块/子块总数与总字数随预览返回 | `test_chunk_preview.py::test_chunk_preview_lists_parents_and_children`、`test_kb_chunk_params.py::test_resolve_chunk_params_merges_with_global_defaults` |
| E-02 | P0 | 库级覆盖参数 | 同一文档入 `KB-B`（1200/400/50），与 `KB-A` 对比 | 两库块数与块长不同；`KB-B` 参数取自库列而非全局；只影响此后新入库 | `test_kb_chunk_params.py::test_build_chunks_respects_per_kb_params`、`test_chunk_aggregation.py::test_smaller_per_kb_params_still_bound_parent_and_child` |
| E-03 | P0 | 参数非法组合被拒 | `PATCH /knowledge-bases/{kb}` 依次提交 ① `overlap ≥ child` ② `parent < child` ③ `0` 或负数 | 均 `422`；合法组合落库并在下次入库生效 | `test_kb_chunk_params.py::test_kb_update_allows_chunk_fields_and_rejects_invalid_combos`、`test_offline_parsing.py::test_invalid_chunk_parameters_rejected` |
| E-04 | P1 | 相邻小节合并 | 入库多个很短的相邻小节 | 合并为一个父块，不产生碎片父块 | `test_chunk_aggregation.py::test_adjacent_small_sections_merge_into_one_parent` |
| E-05 | P1 | 节边界与缓冲上限 | 构造「节内容达到子块上限」与「单节超大」两种文档 | 达到上限时在节边界冲刷；超大节按父块上限二次切分且不丢字 | `test_chunk_aggregation.py::test_section_boundary_flushes_when_buffer_reaches_child_limit`、`test_large_section_splits_at_parent_limit_and_preserves_text` |
| E-06 | P1 | 子块重叠不丢原文 | 对比子块拼接文本与原文章 | 重叠只用于召回，全文可覆盖，无内容丢失 | `test_offline_parsing.py::test_child_overlap_preserves_all_source_text`、`test_chunk_aggregation.py::test_heading_text_merges_into_first_content_parent` |
| E-07 | P1 | 表格与图片块独立 | 含表格与图片标记的文档 | 表格独立成块、图片标记并入周围文本；无标题文档按父块上限聚合 | `test_chunk_aggregation.py::test_tables_stay_independent_and_text_flushes_around_them`、`test_asset_marker_block_merges_into_surrounding_text`、`test_unsectioned_document_aggregates_by_parent_limit` |
| E-08 | P2 | 切块确定性 | 同一文档相同参数重复入库 | 分块内容与稳定 ID 完全一致，可安全重放 | `test_chunk_aggregation.py::test_build_chunks_is_deterministic` |

## F. 向量写入与索引一致性

| 编号 | P | 用例 | 步骤 | 预期结果 | 自动化 |
| --- | --- | --- | --- | --- | --- |
| F-01 | P0 | 集合按 Embedding 档案隔离 | 查 Chroma 目录集合名，并用不同 Embedding 档案建两库 | 集合名由档案 ID 与维度派生，互不混用；维度不符被拒 | `test_chroma_writer.py::test_write_batches_and_scopes_metadata_to_kb_document_and_version`、`test_vector_dimension_mismatch_is_rejected` |
| F-02 | P0 | 写中途失败清理 | 令 embed/index 阶段抛错（或断网）后观察 | 本版本残留向量被清除，不出现「半个版本可被检索」；错误为 `CHROMA_WRITE_ERROR` 时按 `15 × 2^(attempts-1)` 重试，上限 `MAX_INGESTION_ATTEMPTS=3` | `test_chroma_writer.py::test_partial_write_is_cleaned_and_reported_without_leaking_provider_error` |
| F-03 | P0 | 重入库先删后写 | 同 `document_key` 上传 v2 并成功 | 旧向量按 `document_version_id` 清理；检索只命中 v2 | `test_chroma_writer.py::test_delete_removes_every_document_version` |
| F-04 | P1 | MySQL 与 Chroma 同事务 | 成功入库后比对切块行数与 Chroma 向量数 | 分块/词项在向量写成功后的同一事务写入，两边数量对应；无一方孤立 | `test_ingestion_flow.py::test_ingestion_completion_retry_and_duplicate_delivery` |
| F-05 | P1 | 事件投递与死信 | 令 FAQ 生成任务抛错 | 事件保留并重试，超过上限进入死信而非丢失；成功时 `document.faq_generation_requested` 被派发 | `test_outbox_delivery.py::test_failed_in_process_task_keeps_event_for_retry`、`test_faq_generation_event_is_dispatched`、`test_faq_generation_event_retry_and_dead_letter` |
| F-06 | P1 | Embedding 供应商协议 | 观察管理端模型连通性与入库调用 | 按档案发送模型名/端点/密钥；返回顺序被纠正；非法向量被拒；缺密钥在调用前失败 | `test_embedding_provider.py`（11 例全绿） |
| F-07 | P2 | 集合 schema 校验 | 更换切块方案版本或复用旧集合 | schema 不符在写入前被拒，不污染既有集合 | `test_chroma_writer.py::test_collection_schema_mismatch_is_rejected_before_any_write` |

## G. 在线检索

| 编号 | P | 用例 | 步骤 | 预期结果 | 自动化 |
| --- | --- | --- | --- | --- | --- |
| G-01 | P0 | 混合检索返回完整命中结构 | `POST .../search?company_id={company}` 体 `{"query":"…","top_k":5}` | `200`；每条含 `rank`（从 1）、`content`、`scores.{cosine_distance,cosine_similarity,bm25_score,rrf_score,rerank_score}`、`source.{document_id,document_version_id,version_number,filename,chunk_id,page_number,section,section_path}`、`assets` | `test_online_search.py::test_hybrid_search_fuses_sources_and_drops_stale_candidates` |
| G-02 | P0 | 只检索当前版本 | 该文档存在 v1/v2，仅 v2 为当前 | 结果不含 v1 内容，`version_number` 恒为当前版 | `test_online_search.py::test_chroma_search_batches_and_filters_current_versions` |
| G-03 | P0 | 三种检索模式差异 | 同一 query 分别 `retrieval_mode=hybrid` / `bm25` / `vector` | 默认 `hybrid`；`bm25` 不做向量召回、不触发 Rerank；`vector` 以向量为主序；三者均能召回强相关内容 | `test_online_search.py::test_bm25_filters_old_versions_and_optional_document_scope` |
| G-04 | P0 | 空结果不报错 | 查询库内不存在主题 | `200` + `results: []` | 缺口（`/ask` 侧有覆盖，`/search` 空结果路由未断言） |
| G-05 | P0 | 参数边界 | ① `top_k=25`（> `SEARCH_MAX_TOP_K=20`）② `top_k=0` / `101` ③ `query` 为空或仅空格 ④ 未知字段 ⑤ `document_ids` 传 51 个 | 均 `422`；请求模型 `extra="forbid"`，未知字段被拒 | `test_online_search.py`（路由级）、`test_kb_chunk_params.py`（校验风格） |
| G-06 | P1 | RRF 融合权重生效 | 临时把 `SEARCH_VECTOR_WEIGHT`/`BM25_WEIGHT` 调为 0.9/0.1 与 0.1/0.9，对比同一 query 的排序 | 排序随权重迁移，符合 `Σ weight/(60+rank)`；两权重之和为 0 时启动即校验失败 | 缺口 |
| G-07 | P1 | Rerank 生效 | `{kb}` 绑定 Rerank 档案后 `hybrid` 检索 | 融合后才重排、再截断 `top_k`；`scores.rerank_score` 非空且序随其变化 | 缺口 |
| G-08 | P1 | Rerank 失败不降级 | 令 Rerank 端点超时或 5xx | `502`（`RERANK_TIMEOUT` / `RERANK_PROVIDER_HTTP_*`），**不返回未重排结果**；`/ask` 同样失败 | 缺口 |
| G-09 | P1 | Rerank 档案不可用 | 禁用被 `{kb}` 绑定的 Rerank 档案 | `503`（模型不可用），错误指明 Rerank | `test_service_auth.py`（503 分支族）、`test_default_embedding_choice.py` |
| G-10 | P1 | 中文分词与词频 | 检索中英混排、同词重复的 query | 命中不受语言切换影响；重复词按 BM25 正常放大相关性（`k1=1.5`、`b=0.75`） | `test_online_search.py::test_tokenizer_handles_chinese_english_and_repeated_terms` |
| G-11 | P1 | 上下文扩展 | `include_context=true` 检索步骤类问题 | 命中块保留且扩展止于章节边界；整节场景按原文顺序包含嵌套步骤 | `test_online_search.py::test_context_expansion_keeps_hit_and_stops_at_section_boundary`、`test_full_section_context_includes_nested_steps_in_source_order` |
| G-12 | P1 | 限定文档范围检索 | `document_ids` 传本库 1–3 个文档 | 仅这些文档可命中；传外库/外公司文档 ID 返回 `422` | `test_online_search.py::test_bm25_filters_old_versions_and_optional_document_scope` |
| G-13 | P2 | 大结果集稳定性 | `top_k` 取 20（上限）与 `candidate_k=40` 组合，重复 5 次 | 排序稳定可复现；不出现候选被过期版本挤占后空返回 | 缺口 |

## H. 产品与型号过滤

| 编号 | P | 用例 | 步骤 | 预期结果 | 自动化 |
| --- | --- | --- | --- | --- | --- |
| H-01 | P0 | 带标签文档按值过滤 | 库内混有标 `产品A`、`产品B` 的文档，检索带 `product_name=产品A` | 只返回 `产品A` 文档命中；过滤在召回前下推为 `document_ids` | `test_product_filter.py::test_search_product_filter_restricts_to_matching_documents`、`test_http_search_accepts_product_filter_and_short_circuits` |
| H-02 | P0 | 无标签文档视为通用 | 库内含未打标签的通用资料，带 `model_name=X` 检索 | 未打标文档**通过**过滤并可命中；打标文档仅当标签列表含该值才通过 | `test_product_filter.py::test_metadata_matches_product_treats_unlabeled_as_general` |
| H-03 | P1 | 多品类文档合并 | 一个文档同时标 `产品A`、`产品B` | 按 `产品A` 或 `产品B` 过滤都能命中（列表内精确匹配，去空格） | `test_product_filter.py::test_document_product_labels_merges_legacy_and_list_shapes` |
| H-04 | P1 | 过滤无匹配时短路 | 传一个库内不存在的型号 | `results: []`，不调用向量检索，不报错 | `test_product_filter.py::test_http_search_accepts_product_filter_and_short_circuits` |
| H-05 | P1 | 与 FAQ 池联动 | 带型号过滤调 `/ask`，该型号有文档级 FAQ | 文档级 FAQ 进入匹配池；**不带过滤时只用库级 FAQ**；非当前版本文档的 FAQ 不参与 | `test_faq.py::test_match_faq_product_scope`、`test_match_faq_excludes_stale_document_versions` |
| H-06 | P2 | 超长与空串过滤值 | `product_name` 传 201 字符 / 空串 | 超长 `422`；空串按未传处理（归一为 `None`） | `test_product_filter.py::test_normalize_product_lists_merges_params_and_rejects_oversize` |
| H-07 | P2 | 目录改名级联 | 全局产品目录 `PUT /product-catalog/{id}` 改名 | 既有文档标签级联更新，过滤新名即命中 | `test_product_filter.py::test_http_product_catalog_update_renames_and_cascades` |

## I. 问答 `POST /ask`

| 编号 | P | 用例 | 步骤 | 预期结果 | 自动化 |
| --- | --- | --- | --- | --- | --- |
| I-01 | P0 | 正常问答返回完整结构 | `POST .../ask?company_id={company}` 体 `{"query":"…","top_k":5}` | `200` + `qa_record_id`、`request_id`、`session_id`、`answer`、`faq_matched`、`retrieval_mode`、`sources`（结构同检索命中） | `test_chat_provider.py`（生成路径族）、`test_qa_audit.py::test_qa_success_saves_answer_source_snapshot_and_events` |
| I-02 | P0 | 问答三段留痕一致 | 取 I-01 的 `{qa}` 调 `GET /qa-records/{qa}`，并查 `qa_records`/`qa_sources`/`qa_events` | 详情与列表一致；来源快照含内容、上下文、四个分数与 `asset_ids`；事件含 `request`/`retrieval`/`generation` 三阶段 | `test_qa_audit.py::test_qa_success_saves_answer_source_snapshot_and_events`、`test_admin_qa_records.py::test_http_admin_qa_record_snapshot_renders_asset_markers` |
| I-03 | P0 | 无结果不叫模型 | 问库内确无内容的问题 | `status=no_results`、`sources=[]`、答案为「知识库中没有检索到与问题相关的内容。」（带过滤时含「与「X」相关的内容」）；**聊天模型未被调用**；`review_status=pending` | `test_qa_audit.py::test_qa_failure_and_no_results_are_persisted` |
| I-04 | P0 | 模型失败可追 | 令 Chat 端点超时/5xx（或调小 `CHAT_TIMEOUT_SECONDS=60`） | `502`；`qa_records.status=failed`、`review_status=pending`、`error_code` 为 `CHAT_TIMEOUT` / `CHAT_PROVIDER_HTTP_*` / `CHAT_RESPONSE_INVALID` 之一；**不重试** | `test_qa_audit.py::test_qa_failure_and_no_results_are_persisted` |
| I-05 | P1 | token 与耗时记账 | 成功问答后查 `retrieval_duration_ms`、`generation_duration_ms`、`total_duration_ms`、`prompt/completion/total_tokens`、`chat_model_name` | 三段时间均 > 0 且总段 ≈ 前两段之和；token 与模型返回用量一致；`chat_model_name` 取实际档案 | `test_chat_provider.py::test_generate_answer_uses_chat_profile_settings` |
| I-06 | P1 | 生成参数不可漂移 | 检查答案生成调用（管理端问答测试页多问几轮） | `temperature=0.2`、`enable_thinking=false`，上下文边界保留、事实性表述不被改写 | `test_chat_provider.py::test_generate_answer_preserves_context_boundary_and_factual_temperature` |
| I-07 | P1 | 多型号澄清 | 命中结果含 >1 个型号且问题未指明型号 | `clarification_required=true`、`clarification_question` 为「请问您使用的是以下哪个型号？」并列出选项；**未调用模型**，`generation_duration_ms=0` | `test_chat_provider.py::test_stream_qa_events_emit_clarification_done_without_model` |
| I-08 | P1 | 问题已含型号则不澄清 | 问题里写明某型号 | 结果收敛到该型号并正常作答，不触发澄清 | `test_chat_provider.py`（澄清分支） |
| I-09 | P1 | 答案含图片可访问 | 问操作步骤类问题，命中块含图片 | 上下文与答案按原位置渲染图片标记，返回可访问的绝对图片 URL；模型漏写的资产被补齐，伪造链接被剔除 | `test_chat_provider.py::test_answer_context_and_search_result_include_absolute_image_url`、`test_answer_images_keep_model_omitted_assets_out`、`test_answer_images_strip_fake_links_without_appending_assets` |
| I-10 | P1 | 图片全节直出 | 问「测量步骤/使用方法」类问题且命中含图 | 返回原文章节文本，不调模型，`generation_duration_ms=0` | `test_chat_provider.py::test_exact_image_answer_merges_plain_chunks_in_source_order` |
| I-11 | P1 | 低分自动转待补 | 绑定 Rerank，问一个答案质量问题（最佳 `rerank_score < 0.6`） | `status=success` 但 `review_status=pending`；未启用 Rerank（无分数）时**不误标** | `test_qa_audit.py::test_qa_success_low_rerank_marks_pending` |
| I-12 | P1 | 会话归属 | ① 首次传新 `session_id` ② 复用 ③ 用其他子库的 `session_id` | ①自动建会话 ②归入同会话 ③`422` | `test_chat_provider.py`（会话族）、`test_project_traces.py` |
| I-13 | P2 | 表格答案归一 | 问答案落在宽表的问题 | 宽表在答案中被展开可读、`<br>` 类换行被清理，非表格文本原样保留 | `test_chat_provider.py::test_normalize_answer_expands_wide_table_and_cleans_html_breaks`、`test_normalize_answer_preserves_non_table_text` |
| I-14 | P2 | 单轮语义确认 | 同一会话连续两问，第二问依赖第一问上下文 | 按当前设计**不携带历史**给模型（单轮），第二问应独立可答；据此向调用方声明限制 | 缺口（需产品确认是否列入需求） |

## J. 流式问答 `POST /ask/stream`

| 编号 | P | 用例 | 步骤 | 预期结果 | 自动化 |
| --- | --- | --- | --- | --- | --- |
| J-01 | P0 | 事件序列正确 | 正常提问，观察 SSE | `Content-Type: text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`；事件顺序 `meta`（`qa_record_id`/`request_id`）→ 多个 `delta`（`{text}`）→ `done`（完整 `KnowledgeAnswerResponse` JSON） | `test_chat_provider.py::test_stream_qa_events_emit_meta_deltas_and_done` |
| J-02 | P0 | 记录先落库再流式 | 提问后立即用 `meta` 里的 `{qa}` 调 `GET /qa-records/{qa}` | 记录在流开始前已提交；流中即可查到 | `test_chat_provider.py::test_generate_answer_stream_yields_delta_chunks` |
| J-03 | P0 | 检索失败转 error 事件 | 令检索/Rerank 失败后流式提问 | 收到 `error` 事件，`detail` 与非流式对应 HTTP 错误文案一致；记录 `status=failed` 且**用独立会话写入**（不因主流失败丢账） | `test_qa_stream_failure.py::test_stream_failure_persists_with_independent_session`、`test_chat_provider.py::test_stream_qa_events_emit_error_when_search_fails` |
| J-04 | P1 | 客户端中断 | 流式中途关闭页面/curl | 后台任务被保护执行，记录以 `CLIENT_DISCONNECTED`（「客户端已断开连接」）收尾，不悬挂 `running` | 缺口 |
| J-05 | P1 | 免模型路径同样走流 | FAQ 直出 / 澄清 / 无结果三种场景流式提问 | FAQ 直出不做检索与模型调用；澄清与直出场景 `delta` 可为空但 `done` 完整；`faq_matched`、`clarification_required` 字段与非流式一致 | `test_chat_provider.py::test_stream_qa_events_faq_direct_answer_skips_search_and_model`、`test_stream_qa_events_faq_miss_falls_through_to_search`、`test_stream_qa_events_emit_clarification_done_without_model` |
| J-06 | P1 | 文档级 FAQ 带来源 | 带型号过滤流式命中文档级 FAQ | `done.sources` 含该文档引用；库级 FAQ 命中时 `sources` 为空 | `test_chat_provider.py::test_stream_qa_events_faq_document_answer_includes_source` |
| J-07 | P2 | 长答案分片不断流 | 问一个需要长回答（含表格、图片）的问题 | 分片连续、无乱序丢字，最终 `done.answer` 与拼接内容一致；管理端问答测试页渲染无丢失 | `test_chat_provider.py`（流式分片） |

## K. FAQ 生成、改写与直出

| 编号 | P | 用例 | 步骤 | 预期结果 | 自动化 |
| --- | --- | --- | --- | --- | --- |
| K-01 | P0 | 入库成功自动生成 | 完成 B-01 后等 FAQ 生成任务 | 新文档获得自动 FAQ；`GET .../faqs` 可列出；批量补齐接口返回入队/跳过数 | `test_faq.py::test_generate_for_document_version`、`test_batch_generate_faqs_enqueues_missing_documents` |
| K-02 | P0 | 高置信直出 | 用与库级 FAQ 高度相似的问法调 `/ask` | `faq_matched=true`、答案为该 FAQ 原文；`retrieval_duration_ms=0`、`generation_duration_ms=0`、未调模型；库级 FAQ 命中时 `sources=[]` | `test_faq.py::test_ask_endpoint_faq_direct_answer`、`test_cosine_similarity_edges` |
| K-03 | P0 | 阈值边界 | 构造相似度分别在 0.85 上下的问题（可临时调 `FAQ_MATCH_THRESHOLD` 放大差异） | ≥阈值直出，<阈值转检索+生成（`faq_matched=false`）；阈值配置为 ≤0 或 >1 时启动校验失败 | `test_faq.py::test_faq_crud_and_match`、`test_cosine_similarity_edges` |
| K-04 | P0 | 只匹配当前版本 | 文档 v2 为当前、v1 有文档级 FAQ | v1 的 FAQ 不参与匹配，列表也不展示旧版本 FAQ | `test_faq.py::test_match_faq_excludes_stale_document_versions`、`test_list_faqs_hides_stale_document_versions` |
| K-05 | P1 | FAQ CRUD 与筛选 | 增改删库级与文档级 FAQ；用 `scope=all\|kb\|document`、`document_id`、`source`、`q` 查询 | 分页与筛选正确；`PATCH /admin/faqs/{id}` 生效；删除后不再命中 | `test_faq.py::test_http_faq_endpoints`、`test_faq_crud_and_match` |
| K-06 | P1 | AI 改写 | `POST /admin/faqs/{id}/rewrite` | 返回改写答案并可保存；原文不被自动覆盖；改写用提示词来自提示词管理 | `test_faq.py::test_rewrite_faq` |
| K-07 | P1 | 随机推荐问题 | `GET .../faqs/random` | 返回条目均属于该库，可多次调用不重复报错 | `test_faq.py::test_http_faq_endpoints` |
| K-08 | P1 | 单文档同步生成 | `POST /admin/documents/{doc}/faqs/generate` | 同步返回该文档 FAQ；文档未入库成功时给出可读错误 | `test_faq.py::test_generate_for_document_version` |
| K-09 | P1 | 归档库拒绝生成 | 对已归档子库调批量生成 | `409` | `test_kb_archive_restore.py::test_retry_rejected_for_archived_kb`、`test_faq.py`（生成端点族） |
| K-10 | P2 | 提示词三级解析 | 修改子库/公司级 FAQ 生成提示词后重新生成 | 按「库级 > 公司级 > 全局默认」生效；未配置回退默认 | `test_admin_prompts.py`（管理端侧） |
| K-11 | P2 | 生成不污染答案口径 | 对比自动 FAQ 答案与原文 | 答案不引入库内不存在的企业事实（型号、参数、认证号） | 手工 |

## L. 反馈与待补资料闭环

| 编号 | P | 用例 | 步骤 | 预期结果 | 自动化 |
| --- | --- | --- | --- | --- | --- |
| L-01 | P0 | 点踩自动转待补 | 对成功问答 `PUT /qa-records/{qa}/feedback?company_id={company}` 体 `{"feedback":"down","reason":"…"}` | `feedback=down`、`feedback_reason`、`feedback_at` 落库；`review_status` 由 `none` 变 `pending` | `test_feedback.py::test_qa_feedback_marks_pending_and_can_be_cleared` |
| L-02 | P0 | 撤销反馈不回退复核态 | 同一记录改 `{"feedback":null}` | 三个反馈字段清空；`review_status` 保持 `pending`（不清回 `none`） | `test_feedback.py::test_qa_feedback_marks_pending_and_can_be_cleared` |
| L-03 | P0 | 非法值与越权 | ① `{"feedback":"meh"}` ② 多传未知字段 ③ 用他公司 Token 反馈 | ① `422`（CHECK 限 `up`/`down`）② `422`（`extra="forbid"`）③ `404` | `test_feedback.py`（约束族）、`test_service_auth.py::test_company_token_cannot_access_foreign_resources` |
| L-04 | P1 | 已解决不被踩回 | `review_status=resolved` 的记录再次点踩 | 保持 `resolved`，`feedback=down` 仍记录 | `test_feedback.py::test_qa_feedback_marks_pending_and_can_be_cleared` |
| L-05 | P1 | 复核状态人工流转 | `PATCH /admin/qa-records/{qa}/review` 设 `resolved` 再回 `none` | 状态切换正确，管理端「问答记录」筛选 `review_status` 结果随之变化 | `test_admin_qa_records.py`（列表/详情/导出） |
| L-06 | P1 | 项目轨迹反馈 | 外部项目带 `X-Project-Key` 调 `PUT .../project-traces/{external_request_id}/feedback` | 轨迹的复核态更新；项目未登记或已停用返回 `404` | `test_feedback.py::test_project_trace_feedback_updates_review_status` |

## M. 版本回滚、预览与删除

| 编号 | P | 用例 | 步骤 | 预期结果 | 自动化 |
| --- | --- | --- | --- | --- | --- |
| M-01 | P0 | 回滚切换当前版本 | `POST /documents/{doc}/versions/{ver_v1}/activate`（v1 为 `completed`） | `200`；仅 `is_current` 翻转，`latest_version_number` 仍为 2；随后 `/search` 命中 v1 内容、`/ask` 与文档级 FAQ 同步切换 | `test_document_version_activate.py::test_activate_switches_version_and_preview_follows` |
| M-02 | P0 | 回滚拒绝非法目标 | ① 目标版本 `failed` ② 传他库版本 ③ 子库已归档 ④ 该文档有 `queued`/`running` 任务 | ①②④ `409`（①文案「仅可切换到入库成功的版本」）；③ `409`；当前版本不变 | `test_document_version_activate.py::test_activate_rejects_failed_version_foreign_version_and_archived_kb`、`test_activate_rejected_while_ingestion_pending` |
| M-03 | P0 | 删除文档彻底清理 | `DELETE /documents/{doc}` | `204`；先置 `deleting`/任务 `cancelled`，再删 Chroma 全版本向量、对象存储原文件与图片资产、词项/切块/版本/任务/Outbox 行；检索与 FAQ 不再命中 | `test_document_deletion.py::test_document_delete_retries_external_cleanup_and_removes_all_records` |
| M-04 | P0 | 删除的外部清理失败可重放 | 令对象存储/Chroma 清理抛错后重放 `DELETE` | 首次返回 500，状态仍可继续；重复调用安全幂等并最终清干净 | `test_document_deletion.py::test_document_delete_retries_external_cleanup_and_removes_all_records` |
| M-05 | P1 | 删除锁冲突 | 入库进行中删除文档 | 被拒（进程锁持有中），不产生半成品清理 | `test_document_deletion.py::test_delete_refuses_while_ingestion_process_lock_is_held` |
| M-06 | P1 | 原文件下载与预览跟随当前版本 | `GET /documents/{doc}/file?disposition=inline\|attachment`、`GET /documents/{doc}/preview` | 返回**当前可检索版本**内容；尚无当前版时回退最新上传版；Office/RTF/ODF 转 PDF、TIFF 转 PNG 且结果被缓存；无转换器时预览 `503` | `test_document_file.py::test_document_file_serves_latest_version_original`、`test_document_preview.py`（5 例：转换、缓存、缺转换器 503、不支持格式） |
| M-07 | P1 | 失败任务重试 | `POST /documents/{doc}/retry`（分别对 `failed` 与 `completed` 任务） | `failed` → `200`，`attempts` 归零、任务与版本回 `queued`、Outbox 重新投递 `document.ingestion_requested`；非 `failed` → `409` `Only failed ingestion jobs can be retried`；归档库 → `409` | `test_kb_archive_restore.py::test_retry_rejected_for_archived_kb`、`test_ingestion_flow.py::test_ingestion_completion_retry_and_duplicate_delivery` |
| M-08 | P1 | 批量删除与跨库拒绝 | `POST /documents/batch-delete`（同库多文档 / 混入他库文档） | 同库成功删除；混入他库文档 ID 被拒 | `test_document_batch_delete.py::test_batch_delete_documents_ok`、`test_batch_delete_rejects_other_kb_documents` |
| M-09 | P2 | 目录与子库删除前置 | 删除目录/子库/大库 | 目录非空 `409`；大库需先删所有子库否则 `409`；子库删除先清向量 | `test_document_folders.py::test_folder_service_rules`、`test_library_deletion.py`（3 例）、`test_document_batch_delete.py` |

---

## 汇总

| 模块 | 用例数 | P0 | P1 | P2 | 手工/缺口 |
| --- | --- | --- | --- | --- | --- |
| A 链路准入与鉴权 | 6 | 4 | 2 | 0 | 1 |
| B 文档上传与版本创建 | 10 | 4 | 4 | 2 | 1 |
| C 离线解析 | 14 | 3 | 8 | 3 | 1 |
| D OCR 与扫描件 | 8 | 2 | 4 | 2 | 1 |
| E 切块与父子块 | 8 | 3 | 4 | 1 | 0 |
| F 向量写入与索引一致性 | 7 | 3 | 3 | 1 | 0 |
| G 在线检索 | 13 | 5 | 7 | 1 | 5 |
| H 产品与型号过滤 | 7 | 2 | 3 | 2 | 0 |
| I 问答 `/ask` | 14 | 4 | 8 | 2 | 1 |
| J 流式问答 | 7 | 3 | 3 | 1 | 1 |
| K FAQ 生成与直出 | 11 | 4 | 5 | 2 | 1（手工） |
| L 反馈与待补资料 | 6 | 3 | 3 | 0 | 0 |
| M 版本回滚、预览与删除 | 9 | 4 | 4 | 1 | 0 |
| **合计** | **120** | **44** | **58** | **18** | **12** |

11 条标记为 `缺口`：A-05、B-07、C-13、D-08、**G-04（唯一 P0 缺口）**、G-06、G-07、G-08、G-13、I-14、J-04；另有 K-11 只能人工评定答案口径。这些是本轮真实环境手工执行的重点，其中 G-06/G-07/G-08（权重、Rerank 生效与失败不降级）与 G-04（空结果）建议验收后补进 `tests/test_online_search.py`，J-04（客户端中断）补进 `tests/test_qa_stream_failure.py`，I-14（多轮上下文）先做产品决策再决定是否补测。
