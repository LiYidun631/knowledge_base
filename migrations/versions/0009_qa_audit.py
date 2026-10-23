"""创建在线问答记录、引用来源和阶段日志表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_qa_audit"
down_revision: str | None = "0008_schema_comments_complete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_innodb_table(name, *columns, **options) -> None:
    """统一使用 InnoDB，保证 MySQL 外键和事务约束生效。"""
    options.setdefault("mysql_engine", "InnoDB")
    op.create_table(name, *columns, **options)


def upgrade() -> None:
    """创建在线问答业务表和可追溯的阶段日志表。"""
    _create_innodb_table(
        "qa_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("extra_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'closed')", name="ck_qa_sessions_status"
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="在线问答会话",
    )
    op.create_index(
        "ix_qa_sessions_knowledge_base_created",
        "qa_sessions",
        ["knowledge_base_id", "created_at"],
        unique=False,
    )

    _create_innodb_table(
        "qa_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("retrieval_mode", sa.String(length=20), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("include_context", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("chat_provider", sa.String(length=80), nullable=True),
        sa.Column("chat_model_name", sa.String(length=200), nullable=True),
        sa.Column("retrieval_duration_ms", sa.Integer(), nullable=True),
        sa.Column("generation_duration_ms", sa.Integer(), nullable=True),
        sa.Column("total_duration_ms", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("extra_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'no_results', 'failed')",
            name="ck_qa_records_status",
        ),
        sa.CheckConstraint(
            "retrieval_mode IN ('hybrid', 'bm25', 'vector')",
            name="ck_qa_records_retrieval_mode",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["qa_sessions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_qa_records_request_id"),
        comment="在线问答主记录和结果审计",
    )
    op.create_index(
        "ix_qa_records_knowledge_base_created",
        "qa_records",
        ["knowledge_base_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_qa_records_session_created", "qa_records", ["session_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_qa_records_status_created", "qa_records", ["status", "created_at"], unique=False
    )

    _create_innodb_table(
        "qa_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("qa_record_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("document_version_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(length=500), nullable=True),
        sa.Column("content_snapshot", sa.Text(), nullable=False),
        sa.Column("context_snapshot", sa.Text(), nullable=True),
        sa.Column("cosine_distance", sa.Float(), nullable=True),
        sa.Column("bm25_score", sa.Float(), nullable=True),
        sa.Column("rrf_score", sa.Float(), nullable=True),
        sa.Column("rerank_score", sa.Float(), nullable=True),
        sa.Column("asset_ids", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["qa_record_id"], ["qa_records.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["document_chunks.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("qa_record_id", "rank", name="uq_qa_sources_record_rank"),
        comment="在线问答引用来源和文本快照",
    )
    op.create_index("ix_qa_sources_record", "qa_sources", ["qa_record_id"], unique=False)
    op.create_index("ix_qa_sources_document", "qa_sources", ["document_id"], unique=False)

    _create_innodb_table(
        "qa_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("qa_record_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("message", sa.String(length=1000), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('started', 'success', 'failed')", name="ck_qa_events_status"
        ),
        sa.ForeignKeyConstraint(
            ["qa_record_id"], ["qa_records.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="在线问答阶段日志",
    )
    op.create_index(
        "ix_qa_events_record_created", "qa_events", ["qa_record_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_qa_events_request_created", "qa_events", ["request_id", "created_at"], unique=False
    )


def downgrade() -> None:
    """按外键依赖倒序删除在线问答表。"""
    op.drop_index("ix_qa_events_request_created", table_name="qa_events")
    op.drop_index("ix_qa_events_record_created", table_name="qa_events")
    op.drop_table("qa_events")
    op.drop_index("ix_qa_sources_document", table_name="qa_sources")
    op.drop_index("ix_qa_sources_record", table_name="qa_sources")
    op.drop_table("qa_sources")
    op.drop_index("ix_qa_records_status_created", table_name="qa_records")
    op.drop_index("ix_qa_records_session_created", table_name="qa_records")
    op.drop_index("ix_qa_records_knowledge_base_created", table_name="qa_records")
    op.drop_table("qa_records")
    op.drop_index("ix_qa_sessions_knowledge_base_created", table_name="qa_sessions")
    op.drop_table("qa_sessions")
