"""为所有数据表的全部字段补齐中文注释。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_column_comments"
down_revision: str | None = "0027_project_qa_traces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 每张表已有注释的字段保持原文，其余字段由本表补充。
COLUMN_COMMENTS: dict[str, dict[str, str]] = {
    "model_profiles": {
        "id": "主键 ID（UUID）",
        "name": "档案名称；同一模型类型内唯一",
        "kind": "模型类型：embedding/rerank/chat/ocr",
        "provider": "供应商标识，如 dashscope、qianfan、openai_compatible",
        "model_name": "供应商侧的模型名称",
        "dimensions": "向量维度；非 Embedding 类型可为空",
        "settings": "模型普通参数 JSON，如接口地址、超时时间",
        "secret_ref": "API Key 在 .env 中的引用名",
        "api_key_cipher": "Fernet 加密的模型 API Key；管理接口不回读",
        "is_enabled": "是否启用",
        "is_default": "是否为该类型的默认档案",
        "default_kind": "生成列：默认档案的模型类型，用于唯一约束",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "companies": {
        "id": "主键 ID（UUID）",
        "code": "公司唯一编码",
        "name": "公司名称；全局唯一",
        "description": "公司描述",
        "status": "状态：active 正常 / archived 归档",
        "archived_at": "归档时间（UTC）；空表示未归档",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "company_credentials": {
        "id": "主键 ID（UUID）",
        "company_id": "所属公司 ID",
        "name": "凭据名称或用途；同一公司内唯一",
        "token_hash": "公司 Token 的 SHA-256 哈希，不保存明文",
        "is_enabled": "是否启用",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "admin_credentials": {
        "id": "主键 ID（UUID）",
        "name": "管理员标识",
        "token_hash": "凭据哈希",
        "is_enabled": "是否启用",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "admin_prompt_overrides": {
        "name": "提示词名称（主键）",
        "content": "覆写内容",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "vector_libraries": {
        "id": "主键 ID（UUID）",
        "company_id": "所属公司 ID；每公司一个大库",
        "name": "大库名称",
        "description": "大库描述",
        "embedding_model_id": "绑定的 Embedding 模型档案 ID",
        "rerank_model_id": "绑定的 Rerank 模型档案 ID；空表示不重排",
        "models_locked_at": "模型绑定锁定时间（UTC）",
        "status": "状态：active 正常 / archived 归档",
        "archived_at": "归档时间（UTC）；空表示未归档",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "knowledge_bases": {
        "id": "主键 ID（UUID）",
        "project_id": "历史字段：原所属项目 ID，新建知识库不再写入",
        "name": "知识库名称；同一公司内唯一",
        "description": "知识库描述",
        "embedding_model_id": "绑定的 Embedding 模型档案 ID",
        "rerank_model_id": "绑定的 Rerank 模型档案 ID；空表示不重排",
        "vector_store_profile_id": "历史字段：原向量存储档案 ID，新建知识库不再写入",
        "models_locked_at": "模型绑定锁定时间（UTC）",
        "status": "状态：active 正常 / archived 归档",
        "archived_at": "归档时间（UTC）；空表示未归档",
        "company_id": "所属公司 ID；历史数据可为空",
        "vector_library_id": "所属公司大库 ID；历史数据可为空",
        "chunk_parent_chars": "父块字符上限；空表示使用全局默认",
        "chunk_child_chars": "子块字符上限；空表示使用全局默认",
        "chunk_child_overlap_chars": "子块重叠字符数；空表示使用全局默认",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "document_folders": {
        "id": "主键 ID（UUID）",
        "knowledge_base_id": "所属知识库 ID",
        "parent_id": "上级目录 ID；空表示根目录",
        "name": "目录名称",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "product_catalog": {
        "id": "主键 ID（UUID）",
        "product_name": "产品名称",
        "model_name": "产品型号",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "documents": {
        "id": "主键 ID（UUID）",
        "company_id": "所属公司 ID；由子库归属推导",
        "knowledge_base_id": "所属知识库 ID",
        "folder_id": "所在目录；目录删除后文件落回根目录",
        "document_key": "调用方稳定文档键；同一知识库内唯一",
        "filename": "最新版本文件名（仅用于展示）",
        "latest_version_number": "最新版本号；尚无版本时为 0",
        "status": "文档状态：queued/processing/completed/failed/deleting",
        "error_message": "最近一次失败原因摘要",
        "extra_metadata": "文档扩展信息 JSON，如产品与型号标签",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "document_versions": {
        "id": "主键 ID（UUID）",
        "document_id": "所属逻辑文档 ID",
        "version_number": "版本号；同一文档内从 1 递增",
        "filename": "该版本的原始文件名",
        "media_type": "文件媒体类型",
        "size_bytes": "文件大小（字节）",
        "content_sha256": "原始文件 SHA-256 校验值",
        "object_key": "原始文件在对象存储中的键",
        "status": "版本处理状态：queued/processing/completed/failed/deleting",
        "is_current": "是否为当前生效版本",
        "current_document_id": "生成列：当前版本对应的文档 ID，用于唯一约束",
        "error_message": "入库失败原因",
        "extra_metadata": "版本扩展信息 JSON",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "ingestion_jobs": {
        "id": "主键 ID（UUID）",
        "document_id": "所属逻辑文档 ID",
        "document_version_id": "处理的文档版本 ID",
        "status": "任务状态：queued/running/completed/failed/cancelled",
        "stage": "当前处理阶段，如 parse、embed、index",
        "attempts": "已尝试处理次数",
        "error_details": "失败详情 JSON",
        "started_at": "开始处理时间（UTC）",
        "finished_at": "处理结束时间（UTC）",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "document_chunks": {
        "id": "主键 ID（UUID）",
        "document_id": "所属逻辑文档 ID",
        "document_version_id": "所属文档版本 ID",
        "parent_chunk_id": "父块 ID；父块自身为空",
        "ordinal": "同一版本内的分块顺序号",
        "level": "分块层级：parent 父块 / child 子块",
        "content_sha256": "分块文本 SHA-256 校验值",
        "content": "分块文本内容",
        "token_count": "子块分词数，用于 BM25 长度归一化",
        "page_number": "起始页码",
        "extra_metadata": "分块扩展信息 JSON，如章节标题",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "document_chunk_terms": {
        "chunk_id": "子块 ID",
        "term": "词项；中文保存字符二元组",
        "document_id": "所属逻辑文档 ID",
        "document_version_id": "所属文档版本 ID",
        "frequency": "该词项在子块中出现次数",
    },
    "document_assets": {
        "id": "主键 ID（UUID）",
        "document_id": "所属逻辑文档 ID",
        "document_version_id": "所属文档版本 ID",
        "chunk_id": "关联的子块 ID；空表示未挂到具体块",
        "object_key": "图片在对象存储中的键",
        "media_type": "图片媒体类型",
        "description": "图片文字说明或 OCR 描述",
        "marker": "图片在原文中的稳定占位符",
        "location": "图片在原文中的位置元数据",
        "page_number": "所在页码",
        "bbox": "图片坐标框 [x0, y0, x1, y1]",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "faq_entries": {
        "id": "主键 ID（UUID）",
        "knowledge_base_id": "所属知识库 ID",
        "document_id": "来源文档 ID；空表示库级 FAQ",
        "document_version_id": "来源文档版本 ID",
        "question": "问题原文",
        "answer": "答案原文",
        "question_embedding": "问题向量，用于余弦匹配",
        "source": "来源：auto 自动生成 / manual 手动添加",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "qa_sessions": {
        "id": "主键 ID（UUID）",
        "company_id": "所属公司 ID",
        "knowledge_base_id": "所属知识库 ID",
        "status": "会话状态：active 进行中 / closed 已关闭",
        "title": "会话标题",
        "extra_metadata": "会话扩展信息 JSON",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "qa_records": {
        "id": "主键 ID（UUID）",
        "company_id": "所属公司 ID",
        "request_id": "调用方请求 ID；全局唯一",
        "knowledge_base_id": "所属知识库 ID",
        "session_id": "所属会话 ID",
        "query": "用户问题原文",
        "answer": "生成的答案",
        "retrieval_mode": "检索模式：hybrid/bm25/vector",
        "top_k": "召回条数上限",
        "include_context": "是否在答案中附带上下文",
        "status": "问答状态：running/success/no_results/failed",
        "review_status": "复核状态：none/pending/resolved",
        "faq_matched": "答案是否来自 FAQ 直接命中",
        "result_count": "召回结果数量",
        "chat_provider": "回答模型供应商",
        "chat_model_name": "回答模型名称",
        "retrieval_duration_ms": "检索耗时（毫秒）",
        "generation_duration_ms": "生成耗时（毫秒）",
        "total_duration_ms": "总耗时（毫秒）",
        "prompt_tokens": "输入 token 数",
        "completion_tokens": "输出 token 数",
        "total_tokens": "总 token 数",
        "error_code": "失败错误码",
        "error_message": "失败错误详情",
        "extra_metadata": "问答扩展信息 JSON",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "qa_sources": {
        "id": "主键 ID（UUID）",
        "company_id": "所属公司 ID",
        "qa_record_id": "所属问答记录 ID",
        "rank": "引用排序号，从 1 开始",
        "document_id": "来源文档 ID；文档删除后置空",
        "document_version_id": "来源文档版本 ID；文档删除后置空",
        "chunk_id": "来源分块 ID；分块删除后置空",
        "filename": "回答时的文件名快照",
        "version_number": "回答时的版本号快照",
        "page_number": "来源页码",
        "section": "来源章节标题",
        "content_snapshot": "命中片段文本快照",
        "context_snapshot": "父块上下文文本快照",
        "cosine_distance": "向量余弦距离",
        "bm25_score": "BM25 检索得分",
        "rrf_score": "RRF 融合得分",
        "rerank_score": "重排得分",
        "asset_ids": "片段关联的图片资产 ID 列表",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "qa_events": {
        "id": "主键 ID（UUID）",
        "company_id": "所属公司 ID",
        "qa_record_id": "所属问答记录 ID",
        "request_id": "调用方请求 ID",
        "stage": "处理阶段，如 retrieval、generation",
        "status": "阶段状态：started/success/failed",
        "duration_ms": "阶段耗时（毫秒）",
        "error_code": "失败错误码",
        "message": "阶段说明或错误信息",
        "details": "阶段详情 JSON",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "app_projects": {
        "id": "主键 ID（UUID）",
        "project_key": "项目稳定标识（X-Project-Key）；创建后不建议修改",
        "display_name": "项目展示名称",
        "description": "项目描述",
        "is_enabled": "是否启用",
        "retention_days": "轨迹保留天数",
        "last_purged_at": "最近一次过期轨迹清理时间（UTC）",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "project_qa_traces": {
        "id": "主键 ID（UUID）",
        "company_id": "所属公司 ID",
        "project_id": "所属接入项目 ID",
        "knowledge_base_id": "所属知识库 ID",
        "external_request_id": "调用方请求 ID；同一项目内唯一",
        "session_ref": "调用方会话标识",
        "user_ref": "调用方用户标识的哈希，不保存明文",
        "query": "用户问题原文",
        "answer": "最终答案；检索阶段为空",
        "status": "轨迹状态：running/success/no_results/failed/clarification",
        "answer_mode": "回答路径，如 rag/direct/clarification/chat",
        "intent": "意图识别结果 JSON",
        "chat_provider": "回答模型供应商",
        "chat_model_name": "回答模型名称",
        "retrieval_mode": "检索模式",
        "top_k": "召回条数上限",
        "include_context": "是否附带上下文",
        "product_name": "产品名称过滤条件",
        "model_name": "产品型号过滤条件",
        "retrieval_attempts": "每次检索尝试的摘要列表",
        "result_count": "最终召回结果数量",
        "source_count": "召回来源条数",
        "top_rerank_score": "最高重排得分",
        "retrieval_duration_ms": "检索耗时（毫秒）",
        "generation_duration_ms": "生成耗时（毫秒）",
        "total_duration_ms": "总耗时（毫秒）",
        "prompt_tokens": "输入 token 数",
        "completion_tokens": "输出 token 数",
        "total_tokens": "总 token 数",
        "error_code": "失败错误码",
        "error_message": "失败错误详情",
        "finished_at": "完成时间（UTC）",
        "extra_metadata": "调用方补充信息，如 KB 答案直出分数",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "project_qa_sources": {
        "id": "主键 ID（UUID）",
        "company_id": "所属公司 ID",
        "trace_id": "所属项目轨迹 ID",
        "attempt_index": "同一次项目请求内的第几次检索，从 0 开始",
        "rank": "本次检索内的排序号",
        "document_id": "来源文档 ID；文档删除后置空",
        "document_version_id": "来源文档版本 ID；文档删除后置空",
        "chunk_id": "来源分块 ID；分块删除后置空",
        "filename": "文件名快照",
        "version_number": "版本号快照",
        "page_number": "来源页码",
        "section": "来源章节标题",
        "content_snapshot": "命中片段文本快照",
        "context_snapshot": "父块上下文文本快照",
        "cosine_distance": "向量余弦距离",
        "bm25_score": "BM25 检索得分",
        "rrf_score": "RRF 融合得分",
        "rerank_score": "重排得分",
        "asset_ids": "片段关联的图片资产 ID 列表",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "outbox_events": {
        "id": "主键 ID（UUID）",
        "event_type": "事件类型",
        "aggregate_type": "聚合根类型",
        "aggregate_id": "聚合根 ID",
        "payload": "事件负载 JSON",
        "status": "事件状态：pending/processing/published/failed",
        "attempts": "已投递尝试次数",
        "available_at": "最早可投递时间（UTC）",
        "published_at": "成功投递时间（UTC）",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    # 以下为历史兼容表，仅补注释，不改动结构和数据。
    "projects": {
        "id": "主键 ID（UUID）",
        "name": "项目名称",
        "slug": "项目唯一短标识",
        "is_active": "是否启用",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "api_credentials": {
        "id": "主键 ID（UUID）",
        "project_id": "所属历史项目 ID",
        "name": "凭据名称",
        "key_prefix": "密钥前缀，用于识别",
        "secret_hash": "密钥 SHA-256 哈希",
        "scopes": "权限范围 JSON",
        "expires_at": "过期时间（UTC）；空表示不过期",
        "revoked_at": "吊销时间（UTC）；空表示未吊销",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "vector_store_profiles": {
        "id": "主键 ID（UUID）",
        "name": "档案名称；全局唯一",
        "backend": "向量存储后端类型（历史字段）",
        "settings": "连接参数 JSON（历史字段）",
        "secret_ref": "密钥在 .env 中的引用名",
        "is_enabled": "是否启用",
        "created_at": "创建时间（UTC）",
        "updated_at": "更新时间（UTC）",
    },
    "alembic_version": {
        "version_num": "当前 Alembic 迁移版本号",
    },
    "knowledge_alembic_version": {
        "version_num": "当前知识库迁移版本号",
    },
}

# 迁移前已有的字段注释，用于 downgrade 原样恢复。
PREVIOUS_COMMENTS: dict[str, dict[str, str]] = {
    "admin_credentials": {
        "name": "管理员标识",
        "token_hash": "凭据哈希",
        "is_enabled": "是否启用",
    },
    "admin_prompt_overrides": {"name": "提示词名称", "content": "覆写内容"},
    "document_assets": {
        "marker": "图片在原文中的稳定占位符",
        "location": "图片在原文中的位置元数据",
    },
    "document_folders": {
        "knowledge_base_id": "所属知识库 ID",
        "parent_id": "上级目录 ID；空表示根目录",
        "name": "目录名称",
    },
    "documents": {"folder_id": "所在目录；目录删除后文件落回根目录"},
    "faq_entries": {
        "question": "问题原文",
        "answer": "答案原文",
        "question_embedding": "问题向量，用于余弦匹配",
        "source": "来源：auto 自动生成 / manual 手动添加",
    },
    "knowledge_bases": {
        "chunk_parent_chars": "父块字符上限；空表示使用全局默认",
        "chunk_child_chars": "子块字符上限；空表示使用全局默认",
        "chunk_child_overlap_chars": "子块重叠字符数；空表示使用全局默认",
    },
    "model_profiles": {"api_key_cipher": "Fernet 加密的模型 API Key；管理接口不回读"},
    "product_catalog": {"product_name": "产品名称", "model_name": "产品型号"},
    "project_qa_sources": {
        "attempt_index": "同一次项目请求内的第几次检索，从 0 开始",
    },
    "project_qa_traces": {
        "retrieval_attempts": "每次检索尝试的摘要列表",
        "extra_metadata": "调用方补充信息，如 KB 答案直出分数",
    },
    "qa_records": {
        "review_status": "复核状态：none/pending/resolved",
        "faq_matched": "答案是否来自 FAQ 直接命中",
    },
}

# 仅两张版本表缺少表注释，其余业务表的表注释已存在。
TABLE_COMMENTS = {
    "alembic_version": "Alembic 迁移版本记录（旧版本表）",
    "knowledge_alembic_version": "Alembic 迁移版本记录",
}

_NUMERIC_TYPES = {
    "tinyint",
    "smallint",
    "mediumint",
    "int",
    "integer",
    "bigint",
    "decimal",
    "numeric",
    "float",
    "double",
    "real",
    "bit",
}


def _quote_literal(value: str) -> str:
    """转义字符串字面量；内容全部来自本文件常量。"""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _format_default(column_type: str, default: object) -> str:
    """按列类型还原 DEFAULT 子句，避免改写现有默认值语义。"""
    raw = str(default)
    if raw.upper().startswith("CURRENT_TIMESTAMP"):
        return raw
    base = column_type.split("(", 1)[0].lower()
    if base in _NUMERIC_TYPES:
        return raw
    return _quote_literal(raw)


def _column_spec(row, comment: str, table_collation: str | None) -> str:
    """按 information_schema 还原完整列定义，只替换注释。"""
    name, column_type, nullable, default, extra, generation, collation = row
    parts = [f"`{name}`", column_type]
    if collation and collation != table_collation:
        parts.append(f"COLLATE {collation}")
    if generation:
        parts.append(f"GENERATED ALWAYS AS {generation}")
        parts.append("VIRTUAL" if "VIRTUAL" in extra else "STORED")
    parts.append("NULL" if nullable == "YES" else "NOT NULL")
    if not generation and default is not None:
        parts.append("DEFAULT " + _format_default(column_type, default))
    if "auto_increment" in extra.lower():
        parts.append("AUTO_INCREMENT")
    parts.append("COMMENT " + _quote_literal(comment))
    return " ".join(parts)


def _apply_comments(resolve_comment) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    for table, comments in COLUMN_COMMENTS.items():
        rows = bind.execute(
            sa.text(
                "SELECT column_name, column_type, is_nullable, column_default, extra,"
                " generation_expression, collation_name"
                " FROM information_schema.columns"
                " WHERE table_schema = DATABASE() AND table_name = :table"
                " ORDER BY ordinal_position"
            ),
            {"table": table},
        ).all()
        if not rows:
            continue
        table_collation = bind.execute(
            sa.text(
                "SELECT table_collation FROM information_schema.tables"
                " WHERE table_schema = DATABASE() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
        specs = [
            _column_spec(row, resolve_comment(table, row[0]), table_collation)
            for row in rows
            if row[0] in comments
        ]
        if specs:
            clauses = ", ".join(f"MODIFY COLUMN {spec}" for spec in specs)
            bind.exec_driver_sql(f"ALTER TABLE `{table}` {clauses}")
        table_comment = TABLE_COMMENTS.get(table)
        if table_comment:
            bind.exec_driver_sql(f"ALTER TABLE `{table}` COMMENT = {_quote_literal(table_comment)}")


def upgrade() -> None:
    """为每张表的每个字段写入中文注释。"""
    _apply_comments(lambda table, column: COLUMN_COMMENTS[table][column])


def downgrade() -> None:
    """还原为迁移前的注释状态。"""
    _apply_comments(lambda table, column: PREVIOUS_COMMENTS.get(table, {}).get(column, ""))
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    for table in TABLE_COMMENTS:
        bind.exec_driver_sql(f"ALTER TABLE `{table}` COMMENT = ''")
