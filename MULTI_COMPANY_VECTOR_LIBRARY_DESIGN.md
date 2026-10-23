# 多公司公共知识库服务设计

版本：v1.0  
更新日期：2026-10-16

## 1. 建设目标

知识库改造成独立的公共服务，供多个业务项目调用。服务以公司为租户边界，每个公司拥有一个公司级大库，大库下面可以创建多个子库。文件只能归属于子库，检索必须同时校验公司和子库，避免不同公司的数据串库。

```text
company（公司）
└── vector_library（公司级大库，一个公司一个）
    ├── knowledge_base（子库）
    │   ├── documents（文件）
    │   └── document_chunks（父块、子块）
    └── knowledge_base（子库）
        ├── documents
        └── document_chunks
```

## 2. 核心设计决定

### 2.1 公司级大库

`vector_libraries` 是逻辑上的公司级向量大库，不要求在 Chroma 中为每个公司创建一个独立 collection。当前 Chroma 采用 Embedding 模型维度隔离 collection，并使用 metadata 过滤公司、大库和子库，可以减少 collection 数量，方便后续迁移到服务端向量库。

每个公司最多一个 active 大库。大库创建时自动绑定开发配置中的默认 Embedding 和默认 Rerank；模型绑定一经创建就锁定，不能因为新增子库或文件而改变，业务调用方不能选择模型。

### 2.2 子库

现有 `knowledge_bases` 表保留并定义为子库。子库只保存名称、描述、公司和大库归属，不单独选择向量存储。为了兼容现有历史字段，`embedding_model_id` 和 `rerank_model_id` 暂时保留，但新数据由大库模型同步写入；后续迁移稳定后再考虑删除旧字段。

同一公司下子库名称唯一，不同公司可以使用相同的子库名称。

### 2.3 数据隔离

所有文件、文档版本、分块、图片资产和问答记录都通过子库间接归属公司。为降低查询遗漏风险，`documents`、`document_versions`、`document_chunks`、`document_chunk_terms`、`document_assets` 和问答来源快照增加 `company_id` 冗余字段；写入时由服务端根据子库校验并填充，调用方不能自行指定成另一个公司。

Chroma 每条向量的 metadata 必须包含：

```json
{
  "company_id": "公司 UUID",
  "vector_library_id": "大库 UUID",
  "knowledge_base_id": "子库 UUID",
  "document_id": "文档 UUID",
  "document_version_id": "版本 UUID"
}
```

检索时至少使用 `company_id + vector_library_id + knowledge_base_id` 过滤。即使调用方错误传入其他公司的子库 ID，也必须在 MySQL 校验阶段返回 `404` 或 `422`，不能只依赖 Chroma 过滤。

## 3. 数据表

### `companies`

公司租户主表。字段包括 `id`、`code`、`name`、`description`、`status`、时间字段。`code` 全局唯一，作为外部项目稳定传递的公司标识；`name` 用于展示。

### `vector_libraries`

公司级大库。字段包括 `id`、`company_id`、`name`、`description`、`embedding_model_id`、`rerank_model_id`、`models_locked_at`、`status`、时间字段。`company_id` 唯一，保证一个公司只有一个 active 大库。

### `knowledge_bases`

子库。新增 `company_id`、`vector_library_id`，并将名称约束调整为 `company_id + name` 唯一。现有文档和检索接口继续使用 `knowledge_base_id`，但增加公司校验。

### 现有业务表

`documents` 增加 `company_id`。后续可按相同方式为 `document_versions`、`document_chunks`、`document_chunk_terms`、`document_assets` 和问答审计表增加冗余公司字段；第一阶段至少保证文件入库和 Chroma metadata 带有公司标识，第二阶段补齐所有历史审计表。

## 4. 接口设计

### 4.1 创建公司

```http
POST /api/v1/companies
```

请求：

```json
{
  "name": "永康医疗科技有限公司",
  "description": "公司说明"
}
```

返回 `201`，返回公司 `id`、服务端自动生成的公司编码、名称和状态。公司创建只建立租户，不自动创建模型或文件。调用方应保存返回的 `id`，后续使用它作为 `company_id`；不需要自行生成或维护 `code`。

### 4.2 创建公司级大库

```http
POST /api/v1/companies/{company_id}/vector-library
```

请求：

```json
{
  "name": "永康医疗知识大库",
  "description": "公司统一知识向量空间",
  "embedding_model_id": "Embedding 模型 UUID",
  "rerank_model_id": "Rerank 模型 UUID"
}
```

返回 `201`。Embedding 必填，Rerank 可以传 `null` 关闭。公司已有 active 大库时返回 `409`。大库创建后模型绑定锁定。

### 4.3 创建子库

```http
POST /api/v1/companies/{company_id}/vector-library/knowledge-bases
```

请求：

```json
{
  "name": "产品说明书",
  "description": "血氧仪、体温计等产品资料"
}
```

返回 `201`，包括 `company_id`、`vector_library_id` 和 `knowledge_base_id`。子库不接收 Embedding、Rerank 或 Milvus/Chroma 配置，统一继承公司大库配置。

### 4.4 上传文件并入库

```http
POST /api/v1/knowledge-bases/{knowledge_base_id}/documents?company_id={company_id}&file_id=file-001&filename=guide.pdf
```

`company_id` 为必填查询参数。服务端校验：

1. 公司存在且处于 active；
2. 子库存在且属于该公司；
3. 子库属于该公司的 active 大库；
4. 文件按子库写入，不能通过请求体修改归属。

文件对象、文档 metadata、入库任务和 Chroma metadata 均写入同一个服务端确定的 `company_id`。

### 4.5 检索

```http
POST /api/v1/knowledge-bases/{knowledge_base_id}/search?company_id={company_id}
POST /api/v1/knowledge-bases/{knowledge_base_id}/ask?company_id={company_id}
```

`company_id` 必填。服务端先校验公司与子库的归属，再在 MySQL 和 Chroma 中使用公司、大库、子库三个范围过滤。问答记录也保存公司和子库归属。

## 5. 项目调用方式

每个业务项目只需要保存：

```text
KNOWLEDGE_SERVICE_URL
COMPANY_ID
```

业务项目初始化时调用一次创建公司和大库接口，之后由管理端创建子库。业务项目上传文件和检索时始终传 `company_id + knowledge_base_id`。知识库服务负责鉴权、归属校验和数据过滤，项目不直接连接 MySQL 或 Chroma。

建议公共服务使用项目级 Bearer Token，并在服务端维护 token 与允许访问的 `company_id` 范围；不能仅依靠调用方传来的公司 ID 实现安全隔离。

## 6. 删除设计

### 6.1 删除公司级大库

```http
DELETE /api/v1/companies/{company_id}/vector-library
```

大库下存在任何子库时返回 `409`，包括已归档子库。只有子库全部删除后才能删除大库；公司记录和模型档案保留。

### 6.2 删除子库

```http
DELETE /api/v1/companies/{company_id}/vector-library/knowledge-bases/{knowledge_base_id}
POST /api/v1/companies/{company_id}/vector-library/knowledge-bases/batch-delete
```

单个或批量删除子库时，服务端先检查所有入库任务不在 `queued` 或 `running` 状态，再逐个删除子库下的所有文件向量、Chroma metadata、BM25 词项、分块、图片资产、任务和服务端临时文件。`qa_sources` 保留回答时的文本快照；后台文件管理系统中的原文件不删除。

批量请求体：

```json
{"knowledge_base_ids": ["<子库 UUID 1>", "<子库 UUID 2>"]}
```

### 6.3 删除文件向量

```http
DELETE /api/v1/knowledge-bases/{knowledge_base_id}/file-vectors/{file_id}?company_id={company_id}
POST /api/v1/knowledge-bases/{knowledge_base_id}/file-vectors/batch-delete?company_id={company_id}
```

文件删除使用后台传入的稳定 `file_id`，不是文件名。批量删除请求体为 `{"file_ids": ["file-001", "file-002"]}`。服务端同时校验公司、大库、子库和文件归属；未知 ID 返回 `404`，正在入库返回 `409`。Chroma 共享 collection 只按 `document_version_id` 删除，不会删除整个 collection，也不会影响其他公司或子库。

删除接口只清理知识库服务自己的索引和临时副本，不下挖后台的三级目录，也不删除后台原始文件。三级目录 ID、父目录 ID 和完整路径继续作为 `documents.extra_metadata` 与 Chroma metadata 的来源信息保存。

## 7. 迁移策略

新增迁移按以下顺序执行：

1. 创建 `companies` 和 `vector_libraries`，添加中文表注释；
2. 为历史知识库创建一个“历史兼容公司”和一个大库；
3. 将历史 `knowledge_bases`、`documents` 关联到该公司和大库；
4. 为新字段补齐索引和外键；
5. 新接口只允许使用公司、大库和子库关系创建新数据；
6. 旧接口保留一个过渡周期，之后再改为必须传 `company_id`。

迁移不删除历史文件、Chroma 向量、文档版本和问答记录。历史 Chroma metadata 没有 `company_id` 的记录不能直接进入新的公司检索范围；迁移脚本需要在重建向量时补写 metadata，不能通过猜测公司归属解决。

## 8. 分阶段实施

第一阶段：创建公司、大库、子库三张逻辑关系，上传和检索增加公司校验，Chroma 写入和检索增加公司 metadata 过滤。

第二阶段：为文档版本、分块、BM25、图片资产和问答审计表补齐 `company_id` 冗余字段，增加按公司查询和统计接口。

第三阶段：增加公共服务鉴权、项目授权范围、公司级配额和审计日志。
