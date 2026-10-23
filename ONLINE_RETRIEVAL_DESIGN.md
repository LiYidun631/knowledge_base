# 在线检索设计与接口说明

状态：检索、问答生成和问答审计链路已实现，待使用真实模型与业务数据联调  
更新日期：2026-10-20

`/search` 接口只返回检索结果，便于单独检查召回、融合、重排、上下文组织和引用；`/ask` 在同一套检索链路后调用聊天模型生成答案，并将问答、来源快照和阶段日志写入 MySQL。

离线入库中的 Office 图片 OCR 超时问题单独记录为待优化项，不阻塞在线检索设计。当前 DOCX/PPTX 图片会保留原图资产，视觉说明失败时继续完成文档入库。

## 1. 检索能力

在线检索只返回知识片段和来源，不调用聊天模型生成答案。默认使用混合检索：MySQL 中的 BM25 关键词召回与 ChromaDB 向量召回并行执行，再用加权 Reciprocal Rank Fusion（RRF）融合；知识库建库时绑定了 Rerank 模型时，融合后的父块会再送入该模型排序。

API 也允许指定 `bm25` 或 `vector` 单通道，便于关键词调试、模型故障隔离和效果对比。`bm25` 模式只访问 MySQL，不调用 Embedding、Chroma 或 Rerank。

## 2. 接口

### `POST /api/v1/knowledge-bases/{knowledge_base_id}/search?company_id={company_id}`

`company_id` 是必填 UUID 查询参数，`knowledge_base_id` 是路径 UUID，不放入 JSON 请求体。当前本机 API 地址及模板为 `http://127.0.0.1:8333/api/v1`，代码默认端口为 `8000`，以实际配置为准。

独立 `main.py` 通过 `require_service_auth` 校验 `Authorization: Bearer <公司 Token>`，每请求查询 `company_credentials` 的 SHA-256 哈希与启用状态，无鉴权缓存；同一公司可有多枚按项目命名的凭据，任一启用凭据覆盖本公司全部子库（不做项目级数据隔离）。管理员环境 `KNOWLEDGE_SERVICE_AUTH_ADMIN_TOKEN` 必须非空，否则独立服务业务请求 `503`（即使已有公司凭据），健康检查公开。旧环境映射 `KNOWLEDGE_SERVICE_AUTH_COMPANY_TOKENS` 已彻底不再支持，管理员须重新签发；迁移 `0014_company_credentials`（父版本 `0013_document_asset_markers`）只建空表，不自动生成或导入凭据；迁移 `0025_multi_company_credentials`（父版本 `0024_qa_faq_matched`）升级为多凭据表，已有凭据保留为「默认凭据」。真实迁移及实际 `.env` 更新仍待核对目标与备份。

公司凭据由管理员通过 `GET /api/v1/companies/{company_id}/tokens` 列出（含停用，不含哈希/明文）、`POST .../tokens` 体 `{name}` 签发（`201`，同公司内名称重复 `409`）、`POST .../tokens/{credential_id}/reset` 重置（`200`，替换该凭据并启用）、`DELETE .../tokens/{credential_id}` 停用（`204`，保留哈希，重复 `204`）。签发/重置仅限 active 公司；未知公司或凭据返回 `404`；停用允许归档公司凭据。公司 Token 不能管理本公司或其他公司的凭据；嵌入入口 `include_knowledge_module` 的普通业务路由仍由宿主鉴权，**上述凭据 API 即使嵌入也必须携带管理员 Bearer**。

签发/重置先 commit 再返回 `{company_id, company_credential_id, name, token, token_type: "Bearer"}`，明文仅一次，响应含 `Cache-Control: no-store`、`Pragma: no-cache`，不支持 GET 明文回读；请求或响应丢失需管理员重置该凭据，不要重试签发。重置/停用对后续新请求立即生效，不中断已在途请求、无需重启服务端；并发操作以 MySQL 公司行锁串行，最后成功写入的状态生效。详见 [接口文档](API_DOCUMENTATION.md)。

`test_agent` 客户端不变：为每个项目配置 API 签发的对应公司凭据（禁止管理员 Token）、上述 URL、真实公司 UUID 和 `source -> 子库 UUID` 映射，设置 `CHAT_KNOWLEDGE_RETRIEVAL_ENABLED=true`，修改客户端配置后重启。多枚凭据共享公司全部数据权限；只调用 `/search`，不调用 `/ask`。跨机器建议 HTTPS；禁止 Token、Authorization 头及含 Token 响应进入日志。

请求示例（统一携带上述 Bearer 请求头）：

```json
{
  "query": "高血压患者日常生活需要注意什么？",
  "top_k": 5,
  "retrieval_mode": "hybrid",
  "document_ids": ["<可选的文档 UUID>"]
}
```

| 字段 | 规则 | 说明 |
| --- | --- | --- |
| `query` | 必填，去除首尾空格后不能为空，最多 4000 字符 | 问题或关键词 |
| `top_k` | 可选，默认 5，不能超过 `.env` 中的服务上限 | 最终父块数量 |
| `retrieval_mode` | `hybrid`、`bm25` 或 `vector`，默认 `hybrid` | 召回策略 |
| `document_ids` | 可选，最多 50 个 UUID | 只搜索目标知识库内指定文档；不属于该库时返回 `422` |

成功响应示例：

```json
{
  "company_id": "<公司 UUID>",
  "knowledge_base_id": "<知识库 UUID>",
  "query": "高血压患者日常生活需要注意什么？",
  "retrieval_mode": "hybrid",
  "results": [
    {
      "rank": 1,
      "content": "建议保持规律作息，限制钠盐摄入，并按医嘱监测血压。",
      "scores": {
        "cosine_distance": 0.12,
        "cosine_similarity": 0.88,
        "bm25_score": 4.27,
        "rrf_score": 0.016,
        "rerank_score": 0.91
      },
      "source": {
        "document_id": "<文档 UUID>",
        "document_version_id": "<当前版本 UUID>",
        "version_number": 2,
        "filename": "高血压健康指南.pdf",
        "chunk_id": "<父块 UUID>",
        "page_number": 8,
        "section": "生活方式管理"
      }
    }
  ]
}
```

没有命中时返回 `200` 和空 `results`。结果正文使用 MySQL 父块；子块只参与召回。分数含义：cosine 距离越小越相似，BM25、RRF 和 Rerank 分数越大越靠前；不同通道的原始分数不能直接相互比较。未执行的通道分数返回 `null`。

## 3. BM25 分词和倒排表

入库时只为可检索的 child 子块生成词项。中文连续字符拆成重叠二元组，例如“高血压”生成“高血”“血压”；单个汉字保留为一个词项。英文和数字按连续词切分并统一小写，不引入额外分词服务。

MySQL `document_chunk_terms` 保存 `chunk_id`、`term`、`frequency`、文档 ID 和版本 ID；`document_chunks.token_count` 保存子块词项总数。检索时按当前成功版本范围计算语料块数、平均长度、词项文档频率和 BM25 分数。版本状态变化仍以 `document_versions.is_current=true AND status='completed'` 为准。

迁移 `0006_bm25_terms` 会为已有子块分批回填词频与长度；后续上传在同一 MySQL 事务中保存分块和词项。重试会先删除该版本旧词项再重写，文档删除由外键级联清理词项。

## 4. 召回与融合顺序

1. 校验知识库处于 `active`，校验可选文档范围，并从 MySQL 取当前且成功的版本 ID。
2. `hybrid` 模式并行向 Embedding 服务生成查询向量，并在 MySQL 计算 BM25 候选；随后 Chroma 按知识库、当前版本和文档过滤向量候选。版本 ID 分批查询，避免历史向量占用候选名额。
3. 按子块 ID 取得父块正文、章节、页码和版本来源；SQL 再校验版本仍为当前成功状态，过滤检索期间已切换或删除的内容。
4. 每个通道先按父块去重。混合模式按 `weight / (rrf_k + rank)` 计算加权 RRF 分数，默认 rank 从 1 开始，向量权重 `0.7`、BM25 权重 `0.3`、`rrf_k=60`。
5. 如果知识库绑定了 Rerank 模型，使用模型档案中的 `base_url`、`rerank_path`、模型名和 `.env` 密钥引用请求兼容 `/reranks` 的接口，再按服务商分数排序；未绑定时保留 RRF/单通道结果顺序。
6. 返回最多 `top_k` 条父块，并带上文档、当前版本、页码和章节引用。接口不会把密钥或供应商响应正文写入日志。

## 5. 在线检索效果链路

在线检索按以下阶段组织，当前已实现到上下文扩展；后续可在此边界继续增加结果裁剪和生成链路：

### 5.1 查询预处理

- 去除首尾空白和重复空白；
- 拒绝空查询，限制最大长度；
- BM25 使用中文二元组、英文和数字词项；
- 向量通道使用原始问题生成查询向量，避免关键词清洗损失语义。

### 5.2 多路召回

BM25 和 Chroma 向量召回分别返回 child 子块。每个通道独立限制候选数，并且只允许当前且已完成的文档版本进入候选集。

### 5.3 父块合并

child 只负责提高召回精度，返回结果统一提升到 parent。相同父块在同一路由内去重；混合模式再按 child 的最佳排名参与 RRF，避免同一个父块重复占用返回名额。

### 5.4 Rerank

Rerank 的输入是去重后的父块候选，而不是所有 child。这样可以减少模型请求长度，也能让最终排序单位与 API 返回单位一致。绑定 Rerank 的知识库调用失败时返回受控错误，避免静默返回不可比较的排序结果。

### 5.5 上下文扩展

请求中的 `include_context=true` 会在命中父块后，按同一文档版本和父块 ordinal 读取相邻父块。相邻块只用于上下文，不重复计入检索排名；跨章节时停止向对应方向扩展，避免引入无关内容。响应的 `context` 按 `before`、`hit`、`after` 顺序拼接，`context_sources` 保留每段父块 ID 和关系。

### 5.6 结果裁剪

当前上下文扩展使用 `KNOWLEDGE_SERVICE_SEARCH_MAX_CONTEXT_CHARS` 限制每条结果的组合文本长度，并优先保证命中块保留。`/ask` 生成阶段会在此基础上拼接跨结果上下文；每个结果保留完整来源信息，正文裁剪不能删除引用所需的文档 ID、版本 ID、页码和章节。

### 5.7 诊断字段

检索调试阶段保留以下内部指标：各通道候选数量、父块去重数量、RRF 得分、Rerank 得分、最终裁剪数量和耗时。对外响应继续只返回必要分数和来源，避免暴露供应商请求细节。

### 5.8 问答生成与审计

`POST /knowledge-bases/{knowledge_base_id}/ask?company_id={company_id}` 复用前面的检索链路，将命中父块及可选上下文交给聊天模型生成中文答案。问答记录分为四部分：`qa_records` 保存问题、答案、状态和耗时；`qa_sources` 保存生成答案时的正文与分数快照；`qa_events` 保存请求、检索、生成阶段日志；`qa_sessions` 保存可选的多轮会话。

检索无结果时记录 `no_results`，不会调用聊天模型；检索或生成失败时记录 `failed` 和安全错误摘要。接口返回 `company_id`、`qa_record_id` 和 `request_id`，可通过 `GET /qa-records/{qa_record_id}?company_id={company_id}` 追踪来源和阶段日志，查询仍需携带上述 Bearer Token。API Key、系统提示词和供应商完整响应不会写入数据库。

## 6. 集中配置

下列参数放在项目 `.env`，`knowledge_service.core.config.Settings` 提供类型和范围校验；详细中文说明见 `.env.example`。

| 配置 | 默认值 | 用途 |
| --- | ---: | --- |
| `KNOWLEDGE_SERVICE_SEARCH_CANDIDATE_K` | `40` | 每个召回通道候选数 |
| `KNOWLEDGE_SERVICE_SEARCH_VERSION_FILTER_BATCH_SIZE` | `500` | 每次 Chroma 查询的版本数 |
| `KNOWLEDGE_SERVICE_SEARCH_MAX_TOP_K` | `20` | API 最大返回父块数 |
| `KNOWLEDGE_SERVICE_SEARCH_RRF_K` | `60` | RRF 排名平滑常数 |
| `KNOWLEDGE_SERVICE_SEARCH_VECTOR_WEIGHT` | `0.7` | 向量通道融合权重 |
| `KNOWLEDGE_SERVICE_SEARCH_BM25_WEIGHT` | `0.3` | BM25 通道融合权重 |
| `KNOWLEDGE_SERVICE_SEARCH_MAX_QUERY_TERMS` | `256` | 查询最多使用的词项数 |
| `KNOWLEDGE_SERVICE_SEARCH_CONTEXT_BEFORE` | `1` | 每条命中结果最多向前扩展的同章节父块数 |
| `KNOWLEDGE_SERVICE_SEARCH_CONTEXT_AFTER` | `1` | 每条命中结果最多向后扩展的同章节父块数 |
| `KNOWLEDGE_SERVICE_SEARCH_MAX_CONTEXT_CHARS` | `12000` | 每条结果上下文的最大字符数 |
| `KNOWLEDGE_SERVICE_BM25_K1` | `1.5` | BM25 词频饱和参数 |
| `KNOWLEDGE_SERVICE_BM25_B` | `0.75` | BM25 文档长度归一化参数 |
| `KNOWLEDGE_SERVICE_RERANK_TIMEOUT_SECONDS` | `30` | Rerank 调用超时秒数 |

## 7. 错误状态

| 状态码 | 场景 |
| --- | --- |
| `200` | 检索完成，无命中时列表为空 |
| `401` | 独立服务 Bearer Token 缺失、无效、停用或被替换；嵌入检索按宿主规则 |
| `403` | 公司凭证与显式 company_id 不符，或使用归档公司旧有效凭据 |
| `404` | 知识库不存在、已归档或资源 ID 属于其他公司 |
| `422` | 必填 company_id 缺失、UUID 无效，或查询、top_k、文档范围不合法 |
| `502` | Rerank 服务返回无效响应或上游错误 |
| `503` | 独立服务管理员环境 Token 为空，或 Embedding、Chroma、绑定模型暂不可用 |

已绑定的 Rerank 若调用失败会返回受控错误，不悄悄降级成未重排结果。BM25 单通道不受外部模型服务是否在线影响。

## 8. 验证范围

自动化测试覆盖中文与英文分词、词频、当前版本与文档范围过滤、父块返回、加权 RRF、旧版本剔除和入库时同步写词项。自动化测试使用 SQLite、Mock Embedding/Chroma，不需要真实密钥或外网。真实 DashScope Embedding、Chroma 持久数据和 `qwen3.7-text-rerank` 的联调需在 Apifox/部署环境完成。
