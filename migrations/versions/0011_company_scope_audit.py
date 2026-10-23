"""为在线问答会话、记录、来源和阶段日志补充公司租户归属。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_company_scope_audit"
down_revision: str | None = "0010_multi_company_libraries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增可空公司字段并回填历史问答数据，保留原审计记录。"""
    for table in ("qa_sessions", "qa_records", "qa_sources", "qa_events"):
        op.add_column(table, sa.Column("company_id", sa.Uuid(), nullable=True))
        op.create_index(
            f"ix_{table}_company_created",
            table,
            ["company_id", "created_at"],
            unique=False,
        )
        op.create_foreign_key(
            f"fk_{table}_company_id",
            table,
            "companies",
            ["company_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    connection = op.get_bind()
    # 公司归属从问答记录绑定的知识库回填，避免信任历史日志中的业务参数。
    connection.execute(
        sa.text(
            "UPDATE qa_sessions s JOIN knowledge_bases k "
            "ON k.id = s.knowledge_base_id SET s.company_id = k.company_id"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE qa_records r JOIN knowledge_bases k "
            "ON k.id = r.knowledge_base_id SET r.company_id = k.company_id"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE qa_sources s JOIN qa_records r ON r.id = s.qa_record_id "
            "SET s.company_id = r.company_id"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE qa_events e JOIN qa_records r ON r.id = e.qa_record_id "
            "SET e.company_id = r.company_id"
        )
    )


def downgrade() -> None:
    """移除公司归属字段，不删除问答审计数据。"""
    for table in ("qa_events", "qa_sources", "qa_records", "qa_sessions"):
        op.drop_constraint(f"fk_{table}_company_id", table, type_="foreignkey")
        op.drop_index(f"ix_{table}_company_created", table_name=table)
        op.drop_column(table, "company_id")
