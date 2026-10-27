# 知识库接口文档

版本：v0.11
更新日期：2026-10-23

知识库 API 支持两种运行方式：独立运行 `knowledge_base` 时，代码默认地址为 `http://127.0.0.1:8000/api/v1`，当前本机及 `.env.example` 配置为 `http://127.0.0.1:8333/api/v1`；嵌入宿主时使用宿主的监听地址和 `/api/v1` 前缀。

独立入口 `main.py` 挂载 `require_service_auth`，业务请求必须携带 `Authorization: Bearer <Token>`；健康检查公开。服务端环境配置 `KNOWLEDGE_SERVICE_AUTH_ADMIN_TOKEN`（`auth_admin_token: SecretStr`）必须非空，否则业务请求返回 `503`，即使数据库中已有公司凭据也不例外，不是关闭鉴权。

- 有效管理员 Token 可跨公司管理；创建公司 `POST /companies`、旧版 `POST /knowledge-bases` 及公司凭据签发/重置/停用接口仅限管理员。管理员 Token 有两个来源：`.env` 的 `KNOWLEDGE_SERVICE_AUTH_ADMIN_TOKEN`（引导 Token，始终有效，作为遗忘凭据时的兜底入口）和库内 `admin_credentials` 表（管理员 API 签发，见「管理员凭据（多管理员）」）。有效公司 Token 调用仅管理员接口返回 `403`；无效 Token 返回 `401`。
- 公司 Token 只允许操作状态为 `active` 的本公司全部子库、文档、图片、QA 记录及公司大库，不提供子库级权限。缺失、无效、停用或被替换的 Token 返回 `401`；归档公司旧有效凭据返回 `403`。显式 `company_id` 与凭证不匹配返回 `403`，跨公司资源 ID 返回 `404`；UUID 无效返回 `422`。
- 公司 Token 由管理员 API 签发，同一公司可签发多枚、按项目命名并独立重置或停用；服务端每请求查 `company_credentials` 的 SHA-256 哈希与启用状态，无鉴权缓存。多枚凭据只对应多个项目的接入身份，**权限仍以公司为边界，不做项目级数据隔离**。重置/停用对后续新请求立即生效，无需重启服务端，不中断已在途请求。普通公司 Token 不得签发、重置或停用本公司或其他公司的凭据。`test_agent` 只填 API 签发的对应公司 Token，严禁填管理员 Token。
- 旧环境映射 `KNOWLEDGE_SERVICE_AUTH_COMPANY_TOKENS` 已彻底不再支持。迁移 `0014_company_credentials`（父版本 `0013_document_asset_markers`）只建空表，不自动生成已有公司凭据，也不导入旧映射；管理员须重新签发。迁移 `0025_multi_company_credentials` 把单凭据表升级为多凭据表，已有凭据原地保留为「默认凭据」，旧 Token 继续有效。真实数据库迁移及实际 `.env` 更新仍待核对目标与备份，不代表已执行。

嵌入入口 `include_knowledge_module` 的普通业务路由仍由宿主提供鉴权；**公司凭据签发/重置/停用 API 是例外，始终要求管理员 Bearer**，不能通过宿主 `AUTH_ENABLED=false` 绕过。凭据数量按项目划分，但数据权限不随凭据分项目。

跨机器调用建议使用 HTTPS；禁止将 Token、Authorization 头或含 Token 的响应写入应用、代理、审计或调试日志。

知识库提供管理、文件上传、离线入库状态和在线检索接口。在线检索默认使用 MySQL BM25 与 Chroma 向量混合召回，详见 [在线检索设计与接口](ONLINE_RETRIEVAL_DESIGN.md)。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 知识库模块健康检查（公开） |
| POST | `/companies` | 创建公司租户（仅管理员） |
| GET | `/companies` | 列出全部公司（仅管理员，含已归档，含 `credential_count`/`active_credential_count`，供管理端列表使用） |
| GET | `/companies/{company_id}/tokens` | 列出公司全部凭据，含已停用，不含哈希或明文（仅管理员） |
| POST | `/companies/{company_id}/tokens` | 按项目名签发新公司 Token（仅管理员，体 `{"name": ...}`，201） |
| POST | `/companies/{company_id}/tokens/{credential_id}/reset` | 重置指定凭据并启用，旧 Token 立即失效（仅管理员，200） |
| DELETE | `/companies/{company_id}/tokens/{credential_id}` | 停用指定凭据，保留哈希（仅管理员，204） |
| DELETE | `/companies/{company_id}` | 删除公司租户及其凭据（仅管理员，无请求体，204）；公司下仍有大库时 409，需先删子库、再删大库 |
| POST | `/companies/{company_id}/vector-library` | 创建公司级向量大库；可选绑定指定 Embedding/Rerank 模型档案，不传用默认 |
| GET | `/companies/{company_id}/vector-library` | 查询公司 active 大库及锁定模型；本公司 Token 可查，其他公司 403，无大库返回 404 |
| POST | `/companies/{company_id}/vector-library/knowledge-bases` | 创建大库下的子库；可选覆盖父子分块参数 |
| DELETE | `/companies/{company_id}/vector-library` | 删除空的公司级向量大库 |
| DELETE | `/companies/{company_id}/vector-library/knowledge-bases/{knowledge_base_id}` | 删除单个子库及其知识库索引 |
| POST | `/companies/{company_id}/vector-library/knowledge-bases/batch-delete` | 批量删除子库及其知识库索引 |
| GET | `/admin/model-presets` | 代码内置厂商预设（仅管理员） |
| GET | `/admin/model-usage` | 按调用位置汇总实际使用的模型与知识库范围，含环境 OCR/Chat（仅管理员） |
| GET | `/admin/model-profiles?kind=embedding\|rerank\|chat\|ocr` | 列出模型档案，密钥只返回是否已配置（仅管理员） |
| POST | `/admin/model-profiles` | 新增模型档案（仅管理员，201） |
| GET | `/admin/model-profiles/{model_profile_id}` | 查询模型档案详情（仅管理员） |
| PATCH | `/admin/model-profiles/{model_profile_id}` | 修改模型档案；被大库/子库绑定的不可改（仅管理员） |
| DELETE | `/admin/model-profiles/{model_profile_id}` | 删除模型档案；被绑定返回 409（仅管理员） |
| POST | `/admin/model-profiles/{model_profile_id}/default` | 设为同类模型默认档案，返回更新后的档案详情（仅管理员，200） |
| DELETE | `/admin/model-profiles/{model_profile_id}/default` | 取消默认（仅管理员，204） |
| PUT | `/admin/model-profiles/{model_profile_id}/api-key` | 写入厂商 API Key，加密入库且不回读（仅管理员，204） |
| DELETE | `/admin/model-profiles/{model_profile_id}/api-key` | 清除已存 API Key（仅管理员，204） |
| POST | `/admin/model-profiles/{model_profile_id}/test` | 发起一次真实调用测试连通性；失败仍返回 200 与 `ok=false`（仅管理员） |
| GET | `/admin/credentials` | 列出库内管理员凭据，不返回哈希或明文（仅管理员） |
| POST | `/admin/credentials` | 创建管理员凭据，明文 Token 仅本次返回（仅管理员，201） |
| POST | `/admin/credentials/{admin_credential_id}/reset` | 重置管理员 Token 并启用（仅管理员，200） |
| PATCH | `/admin/credentials/{admin_credential_id}` | 启停管理员凭据（仅管理员，200） |
| DELETE | `/admin/credentials/{admin_credential_id}` | 删除管理员凭据（仅管理员，204） |
| GET | `/product-catalog?q=...&product_name=...` | 查询全局产品目录（管理员或有效公司 Token） |
| POST | `/product-catalog` | 新增产品名称与型号组合（仅管理员，201） |
| PUT | `/product-catalog/{catalog_id}` | 修改目录条目并级联更新文档标签（仅管理员） |
| POST | `/product-catalog/batch-delete` | 批量删除目录条目（仅管理员） |
| DELETE | `/product-catalog/{catalog_id}` | 删除目录条目（仅管理员，204） |
| POST | `/knowledge-bases` | **旧版兼容接口（Deprecated，仅管理员）**；新项目请使用公司大库/子库接口 |
| GET | `/knowledge-bases?company_id=...` | 按公司分页列出 active 知识库 |
| GET | `/knowledge-bases/{knowledge_base_id}` | 查询知识库详情 |
| PATCH | `/knowledge-bases/{knowledge_base_id}` | 修改名称、说明或子库分块参数（只影响新入库文档） |
| DELETE | `/knowledge-bases/{knowledge_base_id}` | 归档知识库 |
| POST | `/knowledge-bases/{knowledge_base_id}/documents` | 上传文件并创建入库任务 |
| POST | `/knowledge-bases/{knowledge_base_id}/documents/from-url` | 通过原文件 URL 创建入库任务 |
| POST | `/knowledge-bases/{knowledge_base_id}/search` | BM25、向量或混合检索文档片段 |
| POST | `/knowledge-bases/{knowledge_base_id}/ask` | 检索知识库并调用聊天模型生成答案 |
| GET | `/knowledge-bases/{knowledge_base_id}/qa-records?company_id=...` | 分页查询在线问答记录 |
| GET | `/qa-records/{qa_record_id}?company_id=...` | 查询问答详情、来源快照和阶段日志 |
| GET | `/admin/qa-records?faq_matched=true` | 管理员跨公司分页查询问答记录；可按 `faq_matched` 筛选 FAQ 直出 |
| GET | `/admin/knowledge-bases/{knowledge_base_id}/faqs` | 分页查询 FAQ；支持 `scope=all\|kb\|document`、`document_id`、`source`、`q` 搜索 |
| POST | `/admin/knowledge-bases/{knowledge_base_id}/faqs` | 新增库级或文档级 FAQ |
| GET | `/admin/knowledge-bases/{knowledge_base_id}/faqs/random` | 随机抽取问答页推荐问题 |
| POST | `/admin/knowledge-bases/{knowledge_base_id}/faqs/generate` | 批量补齐缺少自动 FAQ 的文档并返回入队/跳过数；归档库 409 |
| PATCH | `/admin/faqs/{faq_id}` | 修改 FAQ 问题或答案 |
| DELETE | `/admin/faqs/{faq_id}` | 删除 FAQ |
| POST | `/admin/faqs/{faq_id}/rewrite` | AI 改写 FAQ 答案 |
| POST | `/admin/documents/{document_id}/faqs/generate` | 为单个文档同步生成 FAQ |
| GET | `/knowledge-bases/{knowledge_base_id}/documents` | 分页列出逻辑文档 |
| GET | `/documents/{document_id}` | 查询文档详情 |
| GET | `/documents/{document_id}/versions` | 查询文档版本 |
| GET | `/documents/{document_id}/ingestion` | 查询最新版本入库状态 |
| POST | `/documents/{document_id}/retry` | 重试最新版本失败任务 |
| DELETE | `/documents/{document_id}` | 删除文档的全部版本、服务端临时副本和向量 |
| GET | `/documents/{document_id}/file?disposition=inline\|attachment` | 下载或预览最新版本原文件；inline 浏览器内预览，attachment 附件下载 |
| GET | `/documents/{document_id}/preview` | 把最新版本转换为浏览器可内嵌内容：Office/旧版 Office/RTF/ODF → PDF，TIFF → PNG |
| GET | `/document-assets/{asset_id}` | 读取检索结果中的图片资产 |
| DELETE | `/knowledge-bases/{knowledge_base_id}/file-vectors/{file_id}?company_id=...` | 删除单个后台文件的知识库索引 |
| POST | `/knowledge-bases/{knowledge_base_id}/file-vectors/batch-delete?company_id=...` | 批量删除后台文件的知识库索引 |
| GET | `/knowledge-bases/{knowledge_base_id}/folders` | 列出知识库目录（扁平列表，含上级目录 ID，前端自行组装成树） |
| POST | `/knowledge-bases/{knowledge_base_id}/folders` | 创建目录；同一级内不允许重名 |
| PATCH | `/knowledge-bases/{knowledge_base_id}/folders/{folder_id}` | 重命名或移动目录；`parent_id` 传 null 移到根目录 |
| DELETE | `/knowledge-bases/{knowledge_base_id}/folders/{folder_id}` | 删除空目录；目录下仍有子目录或文件时返回 409 |
| PATCH | `/documents/{document_id}` | 移动文档到目录；`folder_id` 传 null 移到根目录 |
| GET | `/documents/{document_id}/versions/{document_version_id}/chunks?offset=0&limit=100` | 切块预览：分页返回父块文本、字符数、页码、章节路径与块类型，附父块/子块总数与父块总字数 |

## 公司、大库和子库

公共服务的数据边界为“公司 → 公司级大库 → 子库 → 文档 → 分块”。公司级大库只允许一个，创建时可指定已启用的 Embedding/Rerank 模型档案（管理页选模型），不指定则自动绑定 `.env` 中配置并初始化的默认模型；模型在大库创建时锁定，已有内容的库不能更换，换模型需新建库。子库继承大库模型，并可单独覆盖父子分块参数。

上传、检索、问答和文件向量删除都必须传 `company_id`，例如：

```text
POST /knowledge-bases/{knowledge_base_id}/documents?company_id={company_id}&filename=说明书.pdf&file_id=file-001
POST /knowledge-bases/{knowledge_base_id}/search?company_id={company_id}
POST /knowledge-bases/{knowledge_base_id}/ask?company_id={company_id}
```

服务端会校验公司、向量大库和子库的归属关系；公司 ID 同时写入文档、Chroma metadata 和问答审计记录。公司凭证冒用其他显式 `company_id` 返回 `403`，跨公司资源 ID 返回 `404`。

删除接口同样要求公司归属匹配。知识库服务只删除自己的服务端临时副本、Chroma 向量、MySQL 分块、BM25 词项、图片资产和入库任务，不会删除后台文件系统中的原文件。后台更深层的三级目录等信息只保存在文档 metadata 中，不会被转换成知识库子库。

大库删除采用严格的父子约束：只要大库下还有任何子库（包括已归档子库），就返回 `409`；必须先删除全部子库，才能删除大库。删除子库会先清理子库下的所有文档索引，再删除子库记录。

知识库内支持多级目录管理（见下文「目录管理」）：目录只用于管理页归组展示，不参与检索过滤；上传文件时可指定 `folder_id` 归入目录，删除非空目录返回 `409`。

## 接口请求模板

以下示例中的 `$base` 是完整 API 前缀，当前本机为 `http://127.0.0.1:8333/api/v1`（代码默认端口 `8000`）；所有 UUID 和文件 ID 都需要替换成实际值。独立服务除健康检查外，每个请求统一携带：

```http
Authorization: Bearer <管理员通过 API 签发的对应公司 Token>
```

创建公司、旧版 `POST /knowledge-bases`、全部 `/admin/*` 管理端接口及公司凭据列表/签发/重置/停用必须改用管理员 Token。下方业务示例未重复列出鉴权头，实际调用不可省略；嵌入模式普通业务按宿主鉴权处理，但凭据管理 API 始终要求管理员 Bearer。JSON 请求统一使用（凭据列表、重置、停用无请求体，无需此头；签发需提交 `{"name": ...}`）：

```http
Content-Type: application/json
```

### 1. 健康检查

```http
GET $base/health
```

无请求体、无查询参数。成功响应：

```json
{"status":"ok","service":"knowledge-service","version":"0.1.0"}
```

### 2. 公司和向量大库

#### 创建公司

```http
POST $base/companies
Content-Type: application/json
```

```json
{
  "name": "永康健康",
  "description": "公司说明，可选"
}
```

`code` 不需要提交，由服务端自动生成；`description` 可省略或传 `null`。请保存响应中的 `id`，后续创建大库、上传文件和检索时将其作为 `company_id` 使用。创建公司不会自动签发公司 Token。

#### 列出全部公司（仅管理员）

```http
GET $base/companies
Authorization: Bearer <管理员 Token>
```

无请求体。返回全部公司（含已归档），按创建时间倒序，每项带 `credential_count`（凭据总数，含停用）与 `active_credential_count`（启用中数量），供管理端展示；不返回凭证明文或哈希。公司 Token 调用返回 `403`。

#### 公司凭据签发、重置与停用（仅管理员）

同一公司可签发多枚凭据，每枚对应一个项目或调用方，按 `name` 区分并独立重置、停用。**凭据只区分接入身份，不隔离数据权限**：任一启用凭据都可访问本公司全部子库、文档、图片、QA 记录和公司大库。独立和嵌入宿主两种模式均必须携带 `Authorization: Bearer <管理员 Token>`，不能用公司 Token 管理本公司或其他公司的凭据；有效公司 Token 调用仅管理员接口返回 `403`。

列表：

```http
GET $base/companies/{company_id}/tokens
Authorization: Bearer <管理员 Token>
```

返回该公司全部凭据（含已停用），启用在前，只含 `id`、`company_id`、`name`、`is_enabled` 与时间戳，不含哈希或明文。未知公司返回 `404`，归档公司可查询。

签发：

```http
POST $base/companies/{company_id}/tokens
Authorization: Bearer <管理员 Token>
Content-Type: application/json
```

```json
{ "name": "项目 A" }
```

`name` 为项目/用途标识，1–120 字符，去除首尾空白后不能为空，同一公司内唯一。仅允许 `active` 公司，成功 `201`；未知或归档公司返回 `404`；同公司内名称重复返回 `409`。凭据数量不设上限。

重置：

```http
POST $base/companies/{company_id}/tokens/{credential_id}/reset
Authorization: Bearer <管理员 Token>
```

需公司为 `active` 且凭据存在，成功 `200`；只替换该凭据的 Token 并启用（可恢复已停用凭据），同公司其他凭据不受影响。未知公司或凭据返回 `404`。

签发与重置响应体相同（示例值为占位符）：

```json
{
  "company_id": "<公司 UUID>",
  "company_credential_id": "<凭据 UUID>",
  "name": "项目 A",
  "token": "ks_<随机部分>",
  "token_type": "Bearer"
}
```

Token 由服务端以 `ks_` + `secrets.token_urlsafe(32)` 生成，随机部分为 256 位；`company_credentials` 仅存 SHA-256 哈希。**先 commit，再返回明文一次**，两种成功响应都包含：

```http
Cache-Control: no-store
Pragma: no-cache
```

不支持 GET 明文回读。请求或响应丢失时不能假定事务未提交，需管理员重置该凭据，不要重试签发；妥善接收并安全交付给调用方，不要把响应或 Token 写入日志。

停用：

```http
DELETE $base/companies/{company_id}/tokens/{credential_id}
Authorization: Bearer <管理员 Token>
```

成功 `204`，无响应体；只停用该凭据并保留哈希，重复停用仍为 `204`。无公司或凭据返回 `404`；允许停用归档公司的凭据。

重置/停用对后续新请求立即生效，无鉴权缓存，无需重启服务端，不中断已在途请求。并发生命周期操作通过 MySQL 公司行锁串行，最后成功写入的状态生效；签发/重置响应中的 Token 也可能被后续成功操作替换或停用。调用方收到重置后的新 Token 后，应更新自己的配置并重启客户端。

#### 管理员凭据（多管理员）

需要多人管理时，管理员可为他人签发独立的库内管理员 Token，与 `.env` 引导 Token 权限相同；凭据只存 SHA-256 哈希，管理接口同样不回读明文。

列表（仅管理员，不返回哈希或明文）：

```http
GET $base/admin/credentials
Authorization: Bearer <管理员 Token>
```

创建（明文 Token 仅本次返回一次）：

```http
POST $base/admin/credentials
Authorization: Bearer <管理员 Token>
Content-Type: application/json
```

```json
{ "name": "值班管理员" }
```

成功 `201`，响应含 `admin_credential_id` 与一次性 `token`（`ka_` + `secrets.token_urlsafe(32)`），响应头 `Cache-Control: no-store`。库内 `admin_credentials` 仅存 SHA-256 哈希与启用状态；`.env` 引导 Token 不在此列且始终有效，即使误删全部库内凭据也能恢复管理入口。

重置（旧 Token 立即失效，新明文仅本次返回，`200`）：

```http
POST $base/admin/credentials/{admin_credential_id}/reset
Authorization: Bearer <管理员 Token>
```

启停（`200`，返回更新后的凭据信息）：

```http
PATCH $base/admin/credentials/{admin_credential_id}
Authorization: Bearer <管理员 Token>
Content-Type: application/json
```

```json
{ "is_enabled": false }
```

删除（`204`，无响应体）：

```http
DELETE $base/admin/credentials/{admin_credential_id}
Authorization: Bearer <管理员 Token>
```

服务端每个请求都重新校验库内管理员 Token 的哈希与启用状态，无鉴权缓存，重置/停用/删除对后续新请求立即生效；不存在的凭据 ID 返回 `404`，凭据标识不能为空白。

#### 创建公司级向量大库

```http
POST $base/companies/{company_id}/vector-library
Content-Type: application/json
```

```json
{
  "name": "永康健康知识大库",
  "description": "公司统一知识向量库",
  "embedding_model_id": "<已启用 Embedding 档案 UUID，可选>",
  "rerank_model_id": "<已启用 Rerank 档案 UUID，可选>"
}
```

请求体允许 `name`、`description`、`embedding_model_id` 和 `rerank_model_id`。两个模型字段都可省略：省略时服务端读取 `.env` 中的默认模型配置并从模型目录绑定默认档案；`rerank_model_id` 显式传 `null` 表示该大库不使用 Rerank。传入的档案必须已登记且启用（`is_enabled=true`），否则返回 `422`；未配置默认模型时省略字段同样返回 `422`。模型在大库创建时锁定，之后不能由子库修改，已有内容的大库不能再更换模型。

#### 查询公司级向量大库

```http
GET $base/companies/{company_id}/vector-library
Authorization: Bearer <管理员或本公司 Token>
```

无请求体。返回公司的 active 大库及锁定模型（`embedding_model_id`、`rerank_model_id`），管理页据此决定是否展示模型选择步骤；本公司 Token 可查询本公司的，跨公司返回 `403`，公司还没有大库时返回 `404`。

#### 创建子库

```http
POST $base/companies/{company_id}/vector-library/knowledge-bases
Content-Type: application/json
```

```json
{
  "name": "产品说明书",
  "description": "产品使用和维护资料",
  "chunk_parent_chars": 1800,
  "chunk_child_chars": 600,
  "chunk_child_overlap_chars": 80
}
```

子库不需要填写模型参数，自动继承公司级大库配置。`chunk_parent_chars`、`chunk_child_chars`、`chunk_child_overlap_chars` 三个分块参数都可选（长度单位：字符），可只传其中一部分：不传的项沿用 `.env` 全局默认值；重叠长度必须小于子块长度、父块长度不能小于子块长度，违反返回 `422`。分块参数在文档入库时读取，修改只影响之后入库的新文档。返回值中的 `id` 就是后续上传、检索和问答使用的 `knowledge_base_id`。

#### 删除公司级大库

```http
DELETE $base/companies/{company_id}/vector-library
```

无请求体。只有大库下没有任何子库时才返回 `204`；仍有子库时返回 `409`。

#### 删除单个子库

```http
DELETE $base/companies/{company_id}/vector-library/knowledge-bases/{knowledge_base_id}
```

无请求体。会删除该子库下的知识库索引、分块、BM25 词项、图片资产、任务和服务端临时副本，后台原文件不删除。

#### 批量删除子库

```http
POST $base/companies/{company_id}/vector-library/knowledge-bases/batch-delete
Content-Type: application/json
```

```json
{
  "knowledge_base_ids": [
    "<子库 UUID 1>",
    "<子库 UUID 2>"
  ]
}
```

批量接口会先校验全部子库归属和入库任务状态；未知子库或存在排队、运行中的任务时返回错误。

### 3. 模型配置与管理（管理端接口）

模型档案（服务商、模型名、地址、维度、密钥引用）登记在数据库 `model_profiles` 表。登记有两种途径：开发人员在 `.env` 维护默认模型和预设后运行 `uv run provision-knowledge-catalog --env`，或管理员调用下方 `/admin/model-profiles` 系列接口增删改查。所有 `/admin/*` 接口仅管理员 Token 可调用，公司 Token 一律 `403`。

- `GET /admin/model-presets`：返回代码内置的厂商预设（openai_compatible、dashscope、bigmodel、volcengine、qianfan、deepseek、baidu 等），供管理页渲染服务商/模型下拉选项。每项声明该厂商支持的调用能力：`supports_embedding`/`embedding_path`、`supports_chat`/`chat_path`、`supports_rerank`/`rerank_path`/`rerank_base_url`/`known_rerank_models`、`supports_ocr`/`ocr_path`/`ocr_base_url`/`ocr_mode`/`known_ocr_models`；前端不再硬编码任何厂商地址或路径。
- `GET /admin/model-usage`：按调用位置返回服务内部实际使用的全部模型，`slots` 依次为 `embedding`（入库向量化、在线检索向量化、FAQ 问题匹配）、`rerank`（检索候选重排）、`chat`（问答生成、FAQ 自动生成、FAQ AI 改写）、`ocr`（扫描件与图片识别）。每个条目含 `source`（`knowledge_base` 知识库绑定 / `default` 默认档案 / `environment` 环境配置）、`scope_names`（使用该模型的公司 / 子库）、`is_active` 等字段；环境模型没有 `profile_id`，不可通过档案接口编辑。响应不含任何密钥明文。
- `GET /admin/model-profiles?kind=embedding|rerank|chat|ocr`：按类型列出档案；响应只含 `has_api_key` 布尔值，**任何接口都不回读密钥明文**。
- `POST /admin/model-profiles`：新增档案，名称重复返回 `409`；Embedding 档案要求正整数维度、白名单服务商、合法 URL 等，Chat 档案不填维度但要求 `base_url` 为合法 URL、`chat_path` 以 `/` 开头且模型名不能是 `YOUR_` 占位符；OCR 档案不填维度，要求白名单服务商、合法 `base_url`、`chat_path` 以 `/` 开头，`settings.ocr_mode` 仅接受 `auto`、`vision`、`paddleocr`、`baidu_async`，模型名不能是 `YOUR_` 占位符，违反返回 `422`。OCR 档案是全局默认模型，不绑定到具体知识库。
- `GET/PATCH/DELETE /admin/model-profiles/{model_profile_id}`：查询/修改/删除。已绑定到某个大库或子库的 Embedding 档案不可修改供应商、模型名、维度或调用参数，也不可删除（`409`），防止已入库向量与档案契约不一致；名称与 API Key 仍可维护。
- `POST/DELETE /admin/model-profiles/{model_profile_id}/default`：设置/取消同类默认档案，同一类型同时只允许一个默认。`POST .../default` 成功返回 `200` 与更新后的档案详情（`ModelProfileRead`），`DELETE .../default` 返回 `204`。
- `PUT /admin/model-profiles/{model_profile_id}/api-key`：写入厂商 API Key（`{"api_key": "..."}`），服务端用 Fernet 加密后入库，成功 `204`，响应带 `Cache-Control: no-store`；密钥内容任何查询接口都不返回，只能覆盖或删除，读不到。`DELETE .../api-key` 清除已存密钥。加密密钥未配置（`KNOWLEDGE_SERVICE_CREDENTIAL_ENCRYPTION_KEY` 为空）时写密钥接口返回 `503`。
- `POST /admin/model-profiles/{model_profile_id}/test`：按档案类型发起一次最小真实调用——Embedding 校验返回向量维度，Rerank 用两条固定文档校验排序，Chat 用固定追问校验能否生成回复，OCR 生成一张含 “OCR TEST 123” 的测试图并校验识别结果。成功返回 `200` 与 `{"ok": true, "kind": "embedding", "latency_ms": 123, "message": "..."}`；供应商超时、鉴权失败或响应不合规则仍返回 `200`，但 `ok=false`，`error_code` 给出如 `EMBEDDING_PROVIDER_HTTP_401`、`CHAT_TIMEOUT` 的错误码，`message` 为中文原因且不含密钥。测试不修改档案状态，停用档案也可测试。

密钥解析优先级：档案单独写入的 API Key（加密存储）优先，其次回落到 `secret_ref` 引用的 `KNOWLEDGE_SERVICE_MODEL_API_KEYS` 环境密钥。两者都没有时，使用该模型的入库/重排请求返回“密钥未配置”错误。

OCR 模型解析优先级：库内默认 OCR 档案（`is_default=true` 且启用）优先，没有时回落到 `.env` 的 `KNOWLEDGE_SERVICE_OCR_*` 配置。默认档案生效后即独占 OCR 调用：档案未写入 API Key 且 `secret_ref` 也未命中环境密钥时，识别会直接返回 `OCR_API_KEY_MISSING`，不会再回落到 `.env` 的 OCR 密钥，需补齐密钥或取消默认。OCR 档案的 `settings.chat_path` 是视觉对话接口路径；`ocr_mode=auto` 时模型名包含 `paddleocr` 会改走专用接口 `{base_url}/ocr/paddleocr`（`ocr_mode=paddleocr` 可强制，`vision` 可强制走视觉对话）。管理页「调用位置 → 扫描件与图片识别 → 接入管理」可用当前环境配置预填表单，保存后自动设为默认，无需重启服务。

`ocr_mode=baidu_async` 走百度智能云官方异步任务接口（预设 `baidu`，`base_url` 固定 `https://aip.baidubce.com`），与上面的同步 `/ocr/paddleocr` 路径并存：提交任务 → 轮询 `task/query` → 立即下载 `parse_result_url`（链接 30 天有效）。该模式使用百度智能云文字识别控制台「应用列表」中的 API Key 与 Secret Key（不是 IAM「安全认证」里的 Access Key），档案 API Key 按 `API Key:Secret Key` 写入（也接受 JSON `{"ak":"...","sk":"..."}`），或把两者放进 `KNOWLEDGE_SERVICE_MODEL_API_KEYS` 的 `BAIDU_OCR_AK` / `BAIDU_OCR_SK` 引用。单图限制 10 MB、最长边 8192 px；PDF 最多 500 页（自动按页分批提交），单文件 50 MB。`settings` 支持 `async_options` 覆盖 `merge_tables`、`relevel_titles`、`analysis_chart`、`recognize_seal`、`erase_seal`、`erase_watermark`、`unwarp`、`angle_adjust`、`angle_mode` 等解析开关；`auto` 模式不会自动选中该路径，需显式指定 `baidu_async`。

默认模型仍可通过 `KNOWLEDGE_SERVICE_DEFAULT_EMBEDDING_PROFILE_NAME`、`KNOWLEDGE_SERVICE_DEFAULT_RERANK_*` 和 `KNOWLEDGE_SERVICE_EMBEDDING_MODEL_PRESETS` 配置；真实 API Key 可继续放在 `KNOWLEDGE_SERVICE_MODEL_API_KEYS`。公司大库创建时按请求参数或默认档案绑定模型；已有大库不会因为后续修改 `.env` 或切换默认档案自动切换 Embedding。切换 Embedding 会改变向量空间，已有文档需要重新生成向量后才能使用新模型，因此模型只在创建大库时可选，之后锁定。

### 4. 旧版知识库兼容接口

这些接口仅用于兼容历史调用，新项目应使用上面的公司级大库和子库接口。

```http
POST $base/knowledge-bases
Content-Type: application/json
```

```json
{
  "name": "历史知识库",
  "description": "旧版兼容数据"
}
```

此请求也不能指定模型，服务端使用默认模型配置。

其他兼容管理请求：

```http
GET    $base/knowledge-bases?company_id={company_id}&offset=0&limit=50
GET    $base/knowledge-bases/{knowledge_base_id}
PATCH  $base/knowledge-bases/{knowledge_base_id}
DELETE $base/knowledge-bases/{knowledge_base_id}
```

`PATCH` 请求体只能包含名称或说明：

```json
{"name":"新的名称","description":"新的说明"}
```

`DELETE` 是归档操作，返回 `204`，不会清理向量和文件。

### 5. 文件入库

#### 直接上传文件

```http
POST $base/knowledge-bases/{knowledge_base_id}/documents?company_id={company_id}&filename=说明书.docx&file_id=file-001
Content-Type: application/octet-stream
```

请求体是原始文件二进制内容，不是 JSON，也不是 Base64：

```powershell
curl.exe -X POST "$base/knowledge-bases/$kbId/documents?company_id=$companyId&filename=说明书.docx&file_id=file-001" `
  -H "Authorization: Bearer $token" `
  -H "Content-Type: application/octet-stream" `
  --data-binary "@C:/files/说明书.docx"
```

必填查询参数：`company_id`、`filename`。`file_id` 在接口校验中是可选的，但后台调用建议始终传入稳定文件 ID；如果省略，服务端会依次使用 `document_key`、文件名作为文档标识，可能导致同名文件被归并为同一文档。其他可选参数：`document_key`、`source_directory_id`、`source_parent_id`、`source_path`。目录参数只作为元数据保存，不创建新的子库。

#### 通过文件 URL 入库

```http
POST $base/knowledge-bases/{knowledge_base_id}/documents/from-url?company_id={company_id}
Content-Type: application/json
```

```json
{
  "file_id": "file-001",
  "file_url": "https://files.example.com/documents/guide.docx",
  "filename": "说明书.docx",
  "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "source_directory_id": "dir-001",
  "source_parent_id": "dir-root",
  "source_path": "产品资料/说明书.docx"
}
```

`company_id` 是必填查询参数；`file_id`、`file_url`、`filename` 是必填 JSON 字段。`media_type`、`source_directory_id`、`source_parent_id`、`source_path` 可选。`file_url` 必须能被知识库服务端访问；后台原文件仍由后台文件系统管理。两个入库接口都返回 `202`，表示任务已登记，不表示向量已经生成完成。

#### 查询文档列表

```http
GET $base/knowledge-bases/{knowledge_base_id}/documents?offset=0&limit=50&q=说明书
```

无请求体；`offset` 默认 `0`，`limit` 默认 `50`，最大 `200`。可选 `folder_id` 只返回该目录下的文档；`root_only=true` 只返回不属于任何目录的文档（传 `folder_id` 时忽略）；`q` 按文件名或文档标识模糊搜索。响应体为文档数组，响应头 `X-Total-Count` 返回应用过滤条件后的文档总数，供分页计算。

#### 查询文档、版本和任务

```http
GET  $base/documents/{document_id}
GET  $base/documents/{document_id}/versions
GET  $base/documents/{document_id}/ingestion
POST $base/documents/{document_id}/retry
```

前三个接口无请求体；`POST .../retry` 也无请求体，只能重试最新版本的失败任务。所有 `{document_id}` 都使用上传接口返回的文档 UUID。

#### 下载文档图片资产

```http
GET $base/document-assets/{asset_id}
```

无请求体，成功时响应体是图片二进制数据，`Content-Type` 为图片实际 MIME 类型。`asset_id` 可从检索响应 `results[].assets[].asset_id` 获取；独立服务请求必须带对应公司或管理员 Bearer Token，跨公司图片 ID 返回 `404`。

检索响应中的 `results[].assets[].url` 是由 `KNOWLEDGE_SERVICE_PUBLIC_BASE_URL` 和
`KNOWLEDGE_SERVICE_API_PREFIX` 拼接出的完整地址。当前本机及模板配置类似：
`http://127.0.0.1:8333/api/v1/document-assets/{asset_id}`（代码默认端口 `8000`）；部署到服务器时应把
`KNOWLEDGE_SERVICE_PUBLIC_BASE_URL` 改成调用方能够访问的域名或网关地址。浏览器或 Markdown
组件加载独立服务图片时也必须携带对应公司 Token，或通过受控后端代理加载；不要暴露管理员
Token，也不要把 Token 放进图片 URL。嵌入模式按宿主鉴权规则处理。

#### 删除文档全部版本

```http
DELETE $base/documents/{document_id}
```

无请求体。该兼容接口按知识库内部文档 UUID 删除全部版本和知识库侧数据；后台原文件不删除。

### 6. 文件向量删除

#### 删除单个文件向量

```http
DELETE $base/knowledge-bases/{knowledge_base_id}/file-vectors/{file_id}?company_id={company_id}
```

无请求体。`file_id` 是后台文件 ID，会删除该文件的全部历史版本索引。

#### 批量删除文件向量

```http
POST $base/knowledge-bases/{knowledge_base_id}/file-vectors/batch-delete?company_id={company_id}
Content-Type: application/json
```

```json
{
  "file_ids": ["file-001", "file-002"]
}
```

### 7. 在线检索和问答

#### 混合检索

```http
POST $base/knowledge-bases/{knowledge_base_id}/search?company_id={company_id}
Content-Type: application/json
```

```json
{
  "query": "高血压患者日常饮食需要注意什么？",
  "top_k": 5,
  "document_ids": ["<可选文档 UUID>"],
  "retrieval_mode": "hybrid",
  "include_context": true
}
```

`query` 必填，长度为 1–4000 个字符；`top_k` 可省略，默认 `5`，并且不能超过服务端 `.env` 配置上限；`retrieval_mode` 可选 `hybrid`、`bm25`、`vector`，默认 `hybrid`；`document_ids` 可省略，指定时最多 50 个且都必须属于当前子库；`include_context` 默认 `false`。

#### 检索并生成答案

```http
POST $base/knowledge-bases/{knowledge_base_id}/ask?company_id={company_id}
Content-Type: application/json
```

```json
{
  "query": "高血压患者日常饮食需要注意什么？",
  "top_k": 5,
  "retrieval_mode": "hybrid",
  "include_context": true,
  "session_id": "<可选会话 UUID>"
}
```

问答接口的请求体与检索接口相同，`include_context` 默认值为 `true`。上传文件时可通过 `product_name` 和 `model_name` 记录产品及型号元数据；当一次检索命中多个不同型号且用户问题未指定型号时，接口会返回 `clarification_required=true`、`clarification_question` 和 `clarification_options`，不会混合多个型号生成答案。每个 `sources[].source` 都包含 `file_id`，对应上传时提交的后台文件 ID，后台可据此拼接原文件下载地址。

#### 查询问答记录

```http
GET $base/knowledge-bases/{knowledge_base_id}/qa-records?company_id={company_id}&page=1&page_size=20
GET $base/qa-records/{qa_record_id}?company_id={company_id}
```

两个接口均无请求体；第一个接口的 `page` 从 `1` 开始，`page_size` 最大为 `100`。

## 健康检查

### `GET /health`

返回模块状态、模块名称和版本。它只检查应用是否能响应，不会验证 MySQL、文件存储或 Embedding 服务连接。

## 知识库

### `POST /knowledge-bases`（旧版兼容接口）

此接口仅为兼容历史调用保留。新项目不要使用此接口，因为它无法建立“公司 → 公司级大库 → 子库”的完整归属关系。
新项目应按以下顺序调用：

1. `POST /companies/{company_id}/vector-library` 创建公司级向量大库并自动绑定服务端默认模型；
2. `POST /companies/{company_id}/vector-library/knowledge-bases` 创建大库下的子库；
3. 上传文件、检索和问答时使用返回的子库 `knowledge_base_id`，并传入 `company_id`。

历史调用如仍需保留，可继续使用本接口，但创建的数据属于旧版兼容数据，后续应迁移到公司和大库体系。

创建逻辑知识库，不会新建 MySQL database 或 Chroma 文件库。请求示例：

```json
{
  "name": "健康知识库",
  "description": "健康科普资料"
}
```

`name` 必填，最多 160 字符。模型参数不接受调用方传入，服务端自动使用开发配置中的默认模型并在创建时锁定。

成功返回 `201` 和知识库对象，含 `id`、名称、模型档案 ID、`models_locked_at`、状态和时间戳。名称冲突返回 `409`，默认模型未登记或不可用返回 `422`。

### `GET /knowledge-bases`

`company_id` 为必填 UUID 查询参数；`offset` 默认 `0`，最小 `0`；`limit` 默认 `50`，范围 `1–200`。先按 `company_id` 和 `status=active` 过滤，再分页，不返回其他公司或已归档数据。

```http
GET $base/knowledge-bases?company_id={company_id}&offset=0&limit=50
Authorization: Bearer <对应公司 Token 或管理员 Token>
```

成功返回 `200` 和知识库对象数组（不是分页包装对象），对象包含 `id`、`company_id`、`vector_library_id`、名称、模型档案 ID、分块参数、状态和时间戳。无匹配项返回 `[]`，管理员查询未知公司也返回 `[]`。公司凭证不得冒用其他 `company_id`（`403`）；缺失 `company_id`、UUID 无效或分页参数越界返回 `422`。

### `GET /knowledge-bases/{knowledge_base_id}`

查询一个未归档知识库。不存在或已归档返回 `404`。

### `PATCH /knowledge-bases/{knowledge_base_id}`

至少传一个字段，只允许修改 `name`、`description` 和三个分块参数：

```json
{
  "name": "健康科普资料库",
  "description": null,
  "chunk_parent_chars": 1800,
  "chunk_child_chars": 600,
  "chunk_child_overlap_chars": 80
}
```

`description: null` 可清空说明；模型 ID 字段不允许通过此接口更新（模型在大库创建时锁定）。分块参数可以只传部分字段，与现有值和 `.env` 全局默认合并后校验：重叠长度必须小于子块长度、父块长度不能小于子块长度，违反返回 `422`。分块参数在文档入库时读取，修改只影响之后入库的新文档，已有向量不受影响。名称冲突返回 `409`。

### `DELETE /knowledge-bases/{knowledge_base_id}`

归档知识库，返回 `204`。此操作保留数据库记录、原始文件和 Chroma 向量；归档后不能继续上传、调整目录、移动文档、重试入库或生成 FAQ（返回 `409`），删除文档等清理操作仍允许。它与文档物理删除是两个不同操作。

## 上传文件和离线入库

### `POST /knowledge-bases/{knowledge_base_id}/documents`

每次上传一个文件，使用原始二进制 body，不是 JSON、Base64 或 multipart。参数如下：

| 位置 | 名称 | 说明 |
| --- | --- | --- |
| 路径 | `knowledge_base_id` | 目标知识库 UUID |
| 查询 | `company_id` | 必填公司租户 UUID，必须与子库归属一致 |
| 查询 | `filename` | 文件名，需有支持的扩展名 |
| 查询 | `file_id` | 后台文件系统中的稳定文件 ID；建议必填 |
| 查询 | `document_key` | 兼容字段；未传 `file_id` 时可作为稳定文档标识 |
| 查询 | `product_name` | 可选产品名称，保存为文档元数据 |
| 查询 | `model_name` | 可选产品型号；在线问答遇到多个型号时用于澄清追问 |
| 查询 | `folder_id` | 可选目录 UUID，上传后文档归入该目录；必须属于本知识库，不传放在根目录 |
| 请求头 | `Content-Type` | 可选 MIME 类型；Apifox 可选择 `application/octet-stream`，也可以删除该请求头 |
| 请求体 | 文件字节 | 非空原文件内容 |

支持扩展名、最大上传大小和文件存储配置见项目 `.env`。默认写入 `data/knowledge_files`；部署服务器时可切换到阿里云 OSS。当前支持 TXT、Markdown、PDF、DOC/DOCX/DOCM、XLS/XLSX/XLSM、PPT/PPTX/PPTM、ODT/ODS/ODP、EPUB、RTF、HTML/HTM/XML、CSV、JSON、LOG 以及常见图片（JPG、JPEG、PNG、BMP、GIF、TIFF、WEBP）。Markdown 中的 GFM 表格会按表格块切分，子块自动携带列头。开启 `KNOWLEDGE_SERVICE_OCR_LAYOUT_ANALYSIS_ENABLED` 后，PDF 会由百炼视觉模型进行版面分析：`auto` 只处理没有文字层的扫描页，`all` 才处理每一页，支持双栏、三栏、表格和图文混排；图片说明进入检索文本，图片对象记录在 `document_assets` 并保存到 `assets/`。模型、接口地址和密钥配置见 [OCR_HTTP_SERVICE.md](OCR_HTTP_SERVICE.md)；服务会优先使用库内默认 OCR 档案（可在管理页模型管理中维护，模式与预设见上文），未配置时回落到 `.env` 的 `KNOWLEDGE_SERVICE_OCR_*`，其中 `KNOWLEDGE_SERVICE_OCR_MODE` 可选 `auto`、`vision`、`paddleocr`、`baidu_async`。

旧版二进制 Office 文件（`.doc`、`.xls`、`.ppt`）以及 OpenDocument 文件（`.odt`、`.ods`、`.odp`）需要本机安装 LibreOffice。转换器按「`KNOWLEDGE_SERVICE_OFFICE_CONVERTER_PATH` → 系统 PATH → Windows 标准安装目录」的顺序自动查找，标准安装通常无需额外配置。新版 Office 文件直接由 Python 解析。文件解析始终依据 `filename` 的扩展名，`Content-Type` 只保存为文件的媒体类型，不参与格式判断。

### `GET /documents/{document_id}/preview`

把文档最新版本转换为浏览器可直接内嵌的内容，响应体为二进制（不是 JSON），用于旧版格式的在线预览：

| 输入格式 | 输出 | 说明 |
| --- | --- | --- |
| `.doc`、`.docx`、`.docm`、`.rtf`、`.odt`、`.ppt`、`.pptx`、`.pptm`、`.odp`、`.xls`、`.xlsx`、`.xlsm`、`.ods` | `application/pdf` | 调用 LibreOffice 无头模式转换；未安装或未配置转换器时返回 `503` |
| `.tif`、`.tiff` | `image/png` | 取第一帧并转为 RGB PNG，不依赖 LibreOffice |

其余格式（PDF、图片、TXT/LOG/JSON/XML、Markdown、CSV、HTML）不需要调用此接口，前端直接读取 `/documents/{document_id}/file?disposition=inline` 后由浏览器或客户端渲染器展示。新版 Word、Excel、PowerPoint 在管理页也优先使用客户端渲染，转换接口作为渲染失败时的兜底。

转换结果按不可变的文档版本 ID 缓存在系统临时目录（`<临时目录>/knowledge-service-previews`），同一版本重复请求直接复用；升级为新版本后按新版本 ID 重新转换。错误语义：`415` 表示该格式没有服务端转换、`503` 表示缺少 LibreOffice、`422` 表示文件损坏或转换失败；前端在这些情况下仍提供原文件下载。

HTML 文件在管理页中以空 `sandbox` 属性的 iframe 渲染，脚本不会执行，也无法访问管理页同源内容。

PowerShell 示例：

```powershell
$base = "http://127.0.0.1:8333/api/v1" # 当前本机独立服务；代码默认端口为 8000
$kbId = "<知识库 UUID>"
$companyId = "<公司 UUID>"
$token = "<管理员通过 API 签发的对应公司 Token>"
curl.exe -X POST "$base/knowledge-bases/$kbId/documents?company_id=$companyId&filename=guide.md&file_id=guide-001" `
  -H "Authorization: Bearer $token" `
  -H "Content-Type: text/markdown" --data-binary "@E:/documents/guide.md"
```

在 Apifox 中请把文件名写在 `filename` 查询参数中，例如 `健康指南.doc`；如果文件属于明确型号，建议同时填写 `model_name=A型号`。Office 文件的 `Content-Type` 选 `application/octet-stream` 即可；下拉列表没有 Word 专用 MIME 类型不影响上传。接口不会把文件内容当 JSON，也不会根据 `Content-Type` 判断扩展名。

独立服务必须传 `Authorization: Bearer <token>`，不能通过留空服务端配置关闭鉴权；嵌入模式按宿主规则处理。上传成功返回 `202`，只表示服务端临时副本和任务已登记，后台仍在处理。再次上传相同 `document_key` 会创建递增版本，不覆盖历史对象。

### 查询文档和任务

- `GET /knowledge-bases/{knowledge_base_id}/documents?offset=0&limit=50`：分页查询知识库中的逻辑文档；可选 `folder_id` 只返回该目录下的文档（不传返回全部），`root_only=true` 只返回根目录文档，`q` 按文件名或文档标识模糊搜索。响应头 `X-Total-Count` 为过滤后的总数。
- `GET /documents/{document_id}`：查询逻辑文档及最新版本状态。
- `GET /documents/{document_id}/versions`：按版本号倒序返回历史版本；`is_current=true` 表示当前成功版本。
- `GET /documents/{document_id}/ingestion`：查询最新版本最近一次任务。
- `POST /documents/{document_id}/retry`：仅可重试最新版本的失败任务，返回 `200` 表示已重新排队；知识库已归档返回 `409`。
- `DELETE /documents/{document_id}`：兼容按知识库内部文档 UUID 删除全部版本、MySQL 分块、服务端临时副本和 Chroma 向量，成功返回 `204`。后台原文件不受影响；如果外部临时副本或向量清理暂时失败，接口返回 `500`，文档会保持 `deleting` 状态，重复调用即可继续清理。

### 目录管理

知识库目录是多级树结构（目录可以挂在目录下面），只影响文档在管理页的归组展示，不参与检索过滤。同一级目录内不允许重名，移动或重命名时会校验；循环移动（把目录挂到自己的子孙下面）会被拒绝。

- `GET /knowledge-bases/{knowledge_base_id}/folders`：返回该知识库全部目录的扁平列表，每项含 `id`、`parent_id`、`name`；前端自行组装成树，根目录不返回记录。
- `POST /knowledge-bases/{knowledge_base_id}/folders`：请求体 `{"name": "产品手册", "parent_id": null}`；`parent_id` 不传或传 null 创建在根目录，传目录 UUID 创建为子目录。同名冲突返回 `409`，上级目录不存在或不属于该知识库返回 `404`。
- `PATCH /knowledge-bases/{knowledge_base_id}/folders/{folder_id}`：请求体只提交要修改的字段，`{"name": "新名称"}` 重命名、`{"parent_id": null}` 移到根目录、`{"parent_id": "<目录 UUID>"}` 移动；可同时提交。移动到自己的子孙目录返回 `422`，目标同级重名返回 `409`。
- `DELETE /knowledge-bases/{knowledge_base_id}/folders/{folder_id}`：只删除空目录，成功返回 `204`；目录下仍有文件或子目录时返回 `409`，需先移走或删除内容。
- `PATCH /documents/{document_id}`：请求体 `{"folder_id": "<目录 UUID>"}` 移动文档，传 `null` 移到根目录；目录必须属于文档所在知识库，否则 `404`。

知识库归档后，目录的创建、重命名、移动、删除以及文档移动统一返回 `409`；恢复为 `active` 后可继续操作。

### 全局产品目录

产品目录独立于知识库，保存允许上传和检索的产品名称与型号组合；上传接口会校验标签必须已在目录中登记。

- `GET /product-catalog?q=...&product_name=...`：管理员或任一有效公司 Token 均可读取，供外部问答客户端对齐产品/型号标签。`q` 对产品名称与型号做不区分大小写的模糊匹配，`product_name` 精确过滤；按产品名称、型号、创建时间排序返回数组。
- 新增、修改、批量删除、单条删除仅限管理员；修改名称会级联更新引用旧标签的文档元数据。
- 公司 Token 只有读权限，不能通过目录接口新增或修改条目。

### 切块预览

```http
GET $base/documents/{document_id}/versions/{document_version_id}/chunks?limit=100
```

无请求体；`limit` 默认 `100`，最大 `500`，表示最多返回的父块数量，子块全部返回。响应按父块组织：

```json
[
  {
    "ordinal": 1,
    "content": "父块文本……",
    "char_count": 1200,
    "page_number": 1,
    "children": [
      {"ordinal": 1, "content": "子块文本……", "char_count": 280}
    ]
  }
]
```

`ordinal` 是文档版本内父块、子块统一连续编号；`page_number` 由解析器提供，解析器未提供时为空。文档或版本不存在返回 `404`，版本必须属于该文档。

### 删除公司级大库、子库和文件向量

删除接口的边界如下：

1. `DELETE /companies/{company_id}/vector-library`：仅删除空大库。存在任意子库时返回 `409`，公司记录保留。
2. `DELETE /companies/{company_id}/vector-library/knowledge-bases/{knowledge_base_id}`：删除一个子库及其全部文档索引、分块、BM25 词项、图片资产、入库任务和服务端临时副本；后台原文件保留。
3. `POST /companies/{company_id}/vector-library/knowledge-bases/batch-delete`：请求体传 `knowledge_base_ids` 数组，批量删除子库。
4. `DELETE /knowledge-bases/{knowledge_base_id}/file-vectors/{file_id}?company_id=...`：按后台文件 ID 删除一个文件的所有历史版本索引。
5. `POST /knowledge-bases/{knowledge_base_id}/file-vectors/batch-delete?company_id=...`：请求体传 `file_ids` 数组，批量删除文件索引；返回删除数量和文件 ID。

批量删除在开始前会校验所有 ID 都属于当前公司和目标范围；存在未知 ID 时返回 `404`，存在排队或运行中的入库任务时返回 `409`。Chroma 集合按 Embedding 模型共享，删除操作只按文档版本过滤向量，不会删除整个集合，也不会影响其他公司或子库。

批量删除子库请求示例：

```json
{
  "knowledge_base_ids": [
    "<子库 UUID 1>",
    "<子库 UUID 2>"
  ]
}
```

批量删除文件向量请求示例：

```json
{
  "file_ids": ["file-001", "file-002"]
}
```

任务状态为 `queued`、`running`、`completed`、`failed` 或 `cancelled`。阶段为 `load`、`parse`、`chunk`、`embed`、`index`、`complete`。轮询建议每 2～5 秒一次；完成或失败后停止。

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
| 200 | 查询、检索或公司凭据重置成功 |
| 201 | 公司/知识库创建或公司凭据签发成功 |
| 202 | 文件和入库任务已登记，任务仍在后台处理 |
| 204 | 公司凭据停用（含重复停用）、知识库归档、大库/子库删除或文件向量删除成功 |
| 400 | 非鉴权请求头格式错误 |
| 401 | Bearer Token 缺失、无效、停用或被替换；嵌入普通业务按宿主规则，凭据管理仍必须管理员 Bearer |
| 403 | 公司凭证与显式 company_id 不符、调用仅管理员接口，或使用归档公司旧有效凭据 |
| 404 | 资源不存在、知识库已归档、资源 ID 属于其他公司；凭据签发/重置的公司未知或归档，重置/停用时无公司或凭据 |
| 409 | 凭据已存在（含停用）时再次签发、名称冲突（含同级目录重名）、目录非空、大库仍有子库、存在运行中的入库任务、模型绑定锁定或任务不可重试 |
| 500 | 服务端临时文件或 Chroma 清理失败；文档可重复调用删除接口重试 |
| 503 | 独立服务管理员环境 Token 为空（即使已有公司凭据），或依赖服务暂不可用 |
| 413 | 文件超过配置大小 |
| 415 | 文件扩展名不支持 |
| 422 | 必填 company_id 缺失、UUID 无效、分页参数越界，或其他参数/模型配置不合法 |

## 在线检索

### `POST /knowledge-bases/{knowledge_base_id}/search?company_id={company_id}`

`company_id` 是必填 UUID Query 参数，`knowledge_base_id` 放路径；此检索契约不变。`test_agent` 只按 `source` 映射调用 `/search`，不调用 `/ask`。

```json
{
  "query": "高血压患者日常生活需要注意什么？",
  "top_k": 5,
  "retrieval_mode": "hybrid",
  "include_context": true
}
```

`retrieval_mode` 可选 `hybrid`（默认）、`bm25` 或 `vector`。`document_ids` 可省略；指定时最多 50 个且必须属于该知识库。`top_k` 默认 5，最大值由 `.env` 的 `KNOWLEDGE_SERVICE_SEARCH_MAX_TOP_K` 控制。`include_context` 默认 `false`；设为 `true` 时，会为每条命中父块附加同章节的前后父块。

混合模式并行执行 MySQL BM25 和 Chroma 向量召回，再按配置权重做 RRF 融合；知识库绑定 Rerank 后会继续重排。响应只返回父块正文和当前成功文档版本来源，不生成答案。开启上下文后，`context` 是按 `before`、`hit`、`after` 顺序拼接的文本，`context_sources` 列出每段的父块 ID 和关系；相邻块数量由 `.env` 的 `KNOWLEDGE_SERVICE_SEARCH_CONTEXT_BEFORE`、`KNOWLEDGE_SERVICE_SEARCH_CONTEXT_AFTER` 控制，总字符数由 `.env` 的 `KNOWLEDGE_SERVICE_SEARCH_MAX_CONTEXT_CHARS` 控制。文档中的图片、表格图片、PDF 原生图片和 HTML 内嵌图片会按原位置返回 Markdown 图片链接，`assets[].location` 记录页码、阅读顺序、单元格或版面坐标。每条结果的 `assets[].url` 可用于显示对应图片。分数和版本过滤说明见 [在线检索设计与接口](ONLINE_RETRIEVAL_DESIGN.md)。

### `POST /knowledge-bases/{knowledge_base_id}/ask?company_id={company_id}`

请求参数与检索接口相同，`include_context` 默认值为 `true`，另支持可选 `session_id` UUID。接口先执行检索和可选 Rerank，再将命中的知识片段及其图片 Markdown 地址交给聊天模型生成 `answer`；`answer` 字段返回标准 Markdown 文本，标题、步骤、列表、表格和已提供的图片引用会保留。响应中的 `sources` 保留 Top-K 原始检索结果及来源，其中 `sources[].source.file_id` 是上传时提交的后台文件 ID，后台可据此拼接原文件下载地址；`sources[].assets[].url` 是文档图片地址。生成模型优先使用管理页配置的默认启用 Chat 模型档案（`kind=chat` 且 `is_default`），未配置时回落 `.env` 的 `KNOWLEDGE_SERVICE_CHAT_*` 配置，密钥复用 `KNOWLEDGE_SERVICE_MODEL_API_KEYS` 中的 `dashscope`。每次请求都会保存问答主记录、来源快照和检索/生成阶段日志；返回的 `qa_record_id` 可直接查询详情，`request_id` 用于日志排查。

```json
{
  "query": "高血压患者日常饮食需要注意什么？",
  "top_k": 5,
  "retrieval_mode": "hybrid",
  "include_context": true
}
```

如果需要把多次问题归入同一个会话，可额外传入：

```json
{
  "session_id": "00000000-0000-0000-0000-000000000001"
}
```

查询记录（仍需携带对应公司或管理员 Bearer Token）：

```http
GET /knowledge-bases/{knowledge_base_id}/qa-records?company_id={company_id}&page=1&page_size=20
GET /qa-records/{qa_record_id}?company_id={company_id}
```

问答失败时接口仍会将 `failed` 状态、阶段、错误代码和耗时写入 MySQL；接口只返回稳定错误摘要，不返回 API Key 或供应商原始响应。

## 项目问答轨迹

项目问答轨迹记录外部项目（例如 `test_agent`）每次问答的完整链路：问题、最终回答、每次检索尝试、召回片段正文快照和文档版本来历。只记录接入后携带轨迹请求头的新请求，不回溯历史问答。

### 记录检索

`POST /knowledge-bases/{knowledge_base_id}/search?company_id={company_id}` 支持以下可选请求头；`X-Project-Key` 与 `X-Trace-Id` 必须同时出现，否则不记录轨迹：

| 请求头 | 长度上限 | 说明 |
| --- | --- | --- |
| `X-Project-Key` | 80 | 已登记且启用的项目标识，例如 `test_agent` |
| `X-Trace-Id` | 128 | 调用方请求 ID；回填时作为路径参数 |
| `X-Session-Id` | 128 | 调用方会话标识，可选 |
| `X-User-Ref` | 64 | 调用方用户标识，建议传哈希，可选 |

检索完成（无论成功或失败）后写入 `running` 轨迹和本次全部召回片段快照；轨迹写入失败只记日志，不影响检索响应。未登记或停用的项目不会产生轨迹。

### 回填回答

外部项目完成回答后回填最终结果。`X-Project-Key` 必填且需与检索时一致，鉴权使用对应公司的 Bearer Token（不接受管理员 Token）：

```http
PUT /knowledge-bases/{knowledge_base_id}/project-traces/{external_request_id}?company_id={company_id}
X-Project-Key: test_agent
```

```json
{
  "query": "红外体温计怎么校准？",
  "answer": "……",
  "status": "success",
  "answer_mode": "rag",
  "intent": {"product_name": "红外体温计"},
  "chat_provider": "dashscope",
  "chat_model_name": "qwen3.8-flash",
  "generation_duration_ms": 3200,
  "total_duration_ms": 4100,
  "prompt_tokens": 1800,
  "completion_tokens": 260,
  "total_tokens": 2060
}
```

`query` 必填；`status` 可选 `success`、`no_results`、`failed`、`clarification`。其余字段均可省略；`extra_metadata` 可保存直出分数、命中规则等项目侧信息。检索记录不存在时按请求内容补建轨迹；成功返回轨迹 ID、最终状态和完成时间。项目未登记或停用、知识库不属于该公司时返回 404，参数不合法返回 422。

### 管理员查询

管理端使用管理员 Bearer Token 跨项目查询：

```http
GET /admin/projects
POST /admin/projects
GET /admin/project-traces?project_id=&company_id=&knowledge_base_id=&status=&answer_mode=&q=&start_time=&end_time=&page=1&page_size=20
GET /admin/project-traces/{trace_id}
```

登记新项目时提交 `project_key`、`display_name`，可选 `description` 和 `retention_days`（默认 90）：

```json
{
  "project_key": "health_agent",
  "display_name": "健康助手",
  "description": "C 端健康问答",
  "retention_days": 90
}
```

`project_key` 仅支持字母、数字及 `_ . : -`，长度 2–80，且必须与调用方 `X-Project-Key` 完全一致；登记后只记录新请求。成功返回 `201` 和项目对象，标识重复返回 `409`，参数不合法返回 `422`。管理页「项目记录」页头的「添加项目」提供同一入口。

列表按创建时间倒序，`page_size` 最大 100；`q` 对问题文本做不区分大小写的模糊匹配，时间过滤基于 `created_at`。列表返回问题、状态、回答路径、模型、耗时、token、累计召回数和答案摘要；详情额外返回完整 `answer`、每次检索尝试摘要 `retrieval_attempts`、意图 `intent`，以及全部召回快照 `sources`（`content_snapshot`、`context_snapshot`、文件名/版本/页码/章节、BM25/RRF/Rerank 分数、`asset_ids`）。快照正文中的图片保留为受鉴权的资产链接，管理页会自动换取可显示的图片地址。

### 保留与清理

每个项目按 `app_projects.retention_days` 保留轨迹（默认 90 天）。清理是惰性的：新轨迹写入时顺带删除该项目超过保留期的记录，同一项目最多每 24 小时清理一次（`last_purged_at`），不影响检索与回填。

`test_agent` 通过 `KNOWLEDGE_TRACE_PROJECT_KEY=test_agent` 启用轨迹上报；留空则保持无状态检索，只检索不记录。

## 本地运行

独立验证时从 `E:\llmpython\knowledge_base` 执行，使用本项目 `.env` 的 `KNOWLEDGE_SERVICE_DATABASE_URL`；嵌入 `test_agent` 时使用宿主原有的数据库配置。真实数据库迁移及实际 `.env` 更新仍待核对目标与备份，以下为待执行的部署步骤，不表示已完成。确认目标并备份后执行迁移和模型初始化；`0014_company_credentials`（父版本 `0013_document_asset_markers`）只建空表，不生成或导入公司凭据；`0025_multi_company_credentials`（父版本 `0024_qa_faq_matched`）把单凭据升级为多凭据，已有凭据保留为「默认凭据」：

```powershell
uv run alembic upgrade head
uv run provision-knowledge-catalog --env
```

独立运行前先配置非空管理员 Token，创建公司后通过管理员 API 为每个项目签发一枚公司凭据；已有单凭据在迁移 `0025_multi_company_credentials` 后保留为「默认凭据」，旧 Token 继续有效。凭据签发/重置/停用无需重启服务端（详见 [README](README.md)）。`test_agent` 仍填 `KNOWLEDGE_SERVICE_URL=http://127.0.0.1:8333/api/v1`、API 签发的对应项目公司凭据（禁止管理员 Token）、真实公司 UUID 和 `source -> 子库 UUID` 映射，并设 `CHAT_KNOWLEDGE_RETRIEVAL_ENABLED=true`；多枚凭据共享公司全部数据权限；客户端配置更新后重启。

执行 `uv run python main.py`，当前本机及模板访问 `http://127.0.0.1:8333/docs`（代码默认端口 `8000`）；嵌入 `test_agent` 时启动宿主原有的 `main.py`，访问宿主的 `/docs`，公司凭据管理 API 仍必须管理员 Bearer。入库任务循环随 FastAPI 应用自动启动，无需额外终端；当前本地文件存储不需要外部对象存储。不要为此模块启动 Redis、Milvus、etcd 或 Celery。
