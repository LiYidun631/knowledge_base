"""为知识库业务表补充中文表注释，便于在数据库工具中识别。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_schema_comments"
down_revision: str | None = "0004_embedded_chroma"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """MySQL 写入中文表注释；SQLite 测试库不执行 MySQL 专属 DDL。"""
    if op.get_bind().dialect.name != "mysql":
        return
    comments = {
        "model_profiles": "Embedding 与 Rerank 模型档案",
        "knowledge_bases": "知识库主表，记录模型绑定和生命周期",
        "documents": "逻辑文档主表，记录文档标识和最新状态",
        "document_versions": "文档不可变版本和原始文件信息",
        "ingestion_jobs": "离线入库任务及处理阶段",
        "document_chunks": "文档父块和子块文本",
        "outbox_events": "事务 Outbox 事件，供应用内任务循环消费",
    }
    for table_name, comment in comments.items():
        # 表名和注释均来自本文件固定字典，不接收用户输入。
        escaped_comment = comment.replace("'", "''")
        op.execute(f"ALTER TABLE `{table_name}` COMMENT = '{escaped_comment}'")


def downgrade() -> None:
    """清除本次增加的中文表注释。"""
    if op.get_bind().dialect.name != "mysql":
        return
    for table_name in (
        "model_profiles",
        "knowledge_bases",
        "documents",
        "document_versions",
        "ingestion_jobs",
        "document_chunks",
        "outbox_events",
    ):
        op.execute(f"ALTER TABLE `{table_name}` COMMENT = ''")
