"""补齐所有知识库业务表的中文表注释。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_schema_comments_complete"
down_revision: str | None = "0007_document_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_COMMENTS = {
    "projects": "项目主表（历史兼容表）",
    "model_profiles": "Embedding 与 Rerank 模型档案",
    "vector_store_profiles": "向量存储配置档案（历史兼容表）",
    "api_credentials": "接口访问凭据（历史兼容表）",
    "knowledge_bases": "知识库主表，记录模型绑定和生命周期",
    "documents": "逻辑文档主表，记录文档标识和最新状态",
    "document_versions": "文档不可变版本和原始文件信息",
    "ingestion_jobs": "离线入库任务及处理阶段",
    "document_chunks": "文档父块和子块文本",
    "document_chunk_terms": "BM25 子块词项倒排索引",
    "document_assets": "文档图片及版面资产",
    "outbox_events": "事务 Outbox 事件，供应用内任务循环消费",
}


def upgrade() -> None:
    """在 MySQL 中为所有已知业务表写入中文表注释。"""
    if op.get_bind().dialect.name != "mysql":
        return
    for table_name, comment in TABLE_COMMENTS.items():
        # 表名和注释均来自固定字典，不接收外部输入。
        escaped_comment = comment.replace("'", "''")
        op.execute(f"ALTER TABLE `{table_name}` COMMENT = '{escaped_comment}'")


def downgrade() -> None:
    """移除本次补充的表注释。"""
    if op.get_bind().dialect.name != "mysql":
        return
    for table_name in TABLE_COMMENTS:
        op.execute(f"ALTER TABLE `{table_name}` COMMENT = ''")
