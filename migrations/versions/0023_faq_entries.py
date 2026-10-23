"""FAQ 表：库级与文档级问答对，含问题向量用于前置匹配直出。"""

import sqlalchemy as sa
from alembic import op

revision = "0023_faq_entries"
down_revision = "0022_admin_prompt_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增 FAQ 表；UUID 列与既有表一致由 SQLAlchemy 按方言生成，保证外键类型匹配。"""
    op.create_table(
        "faq_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("document_version_id", sa.Uuid(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False, comment="问题原文"),
        sa.Column("answer", sa.Text(), nullable=False, comment="答案原文"),
        sa.Column(
            "question_embedding",
            sa.JSON(),
            nullable=False,
            comment="问题向量，用于余弦匹配",
        ),
        sa.Column(
            "source",
            sa.String(16),
            nullable=False,
            comment="来源：auto 自动生成 / manual 手动添加",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="库级与文档级 FAQ",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_faq_entries_kb_id", "faq_entries", ["knowledge_base_id"])
    op.create_index("ix_faq_entries_document_id", "faq_entries", ["document_id"])
    op.create_index("ix_faq_entries_version_id", "faq_entries", ["document_version_id"])


def downgrade() -> None:
    op.drop_index("ix_faq_entries_version_id", table_name="faq_entries")
    op.drop_index("ix_faq_entries_document_id", table_name="faq_entries")
    op.drop_index("ix_faq_entries_kb_id", table_name="faq_entries")
    op.drop_table("faq_entries")
