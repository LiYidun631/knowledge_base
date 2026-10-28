"""项目问答轨迹：接入项目登记、请求轨迹与召回片段快照。"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0027_project_qa_traces"
down_revision = "0026_ocr_model_profiles"
branch_labels = None
depends_on = None

# 预置 test_agent 项目，固定 UUID 便于各环境排查与对接。
TEST_AGENT_PROJECT_ID = uuid.UUID("4b9a5c1e-8d3f-4f6a-9b2e-7c1d0a3e5f21")


def upgrade() -> None:
    op.create_table(
        "app_projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_key", sa.String(80), nullable=False),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("description", sa.String(1000), nullable=True),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "retention_days",
            sa.Integer(),
            server_default=sa.text("90"),
            nullable=False,
        ),
        sa.Column("last_purged_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_key", name="uq_app_projects_project_key"),
        comment="接入项目登记与轨迹保留策略",
        mysql_engine="InnoDB",
    )

    project_table = sa.table(
        "app_projects",
        sa.column("id", sa.Uuid()),
        sa.column("project_key", sa.String(80)),
        sa.column("display_name", sa.String(160)),
        sa.column("description", sa.String(1000)),
        sa.column("is_enabled", sa.Boolean()),
        sa.column("retention_days", sa.Integer()),
    )
    op.bulk_insert(
        project_table,
        [
            {
                "id": TEST_AGENT_PROJECT_ID,
                "project_key": "test_agent",
                "display_name": "健康管家",
                "description": "test_agent 健康管家问答项目；经 /search 检索并自行生成回答。",
                "is_enabled": True,
                "retention_days": 90,
            }
        ],
    )

    op.create_table(
        "project_qa_traces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("external_request_id", sa.String(128), nullable=False),
        sa.Column("session_ref", sa.String(128), nullable=True),
        sa.Column("user_ref", sa.String(64), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("answer_mode", sa.String(40), nullable=True),
        sa.Column("intent", sa.JSON(), nullable=False),
        sa.Column("chat_provider", sa.String(80), nullable=True),
        sa.Column("chat_model_name", sa.String(200), nullable=True),
        sa.Column("retrieval_mode", sa.String(20), nullable=True),
        sa.Column("top_k", sa.Integer(), nullable=True),
        sa.Column("include_context", sa.Boolean(), nullable=False),
        sa.Column("product_name", sa.String(200), nullable=True),
        sa.Column("model_name", sa.String(200), nullable=True),
        sa.Column(
            "retrieval_attempts",
            sa.JSON(),
            nullable=False,
            comment="每次检索尝试的摘要列表",
        ),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("top_rerank_score", sa.Float(), nullable=True),
        sa.Column("retrieval_duration_ms", sa.Integer(), nullable=True),
        sa.Column("generation_duration_ms", sa.Integer(), nullable=True),
        sa.Column("total_duration_ms", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "extra_metadata",
            sa.JSON(),
            nullable=False,
            comment="调用方补充信息，如 KB 答案直出分数",
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'no_results', 'failed', 'clarification')",
            name="ck_project_qa_traces_status",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["app_projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "project_id",
            "external_request_id",
            name="uq_project_qa_traces_request",
        ),
        comment="外部项目问答轨迹与检索摘要",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_project_qa_traces_company_created", "project_qa_traces", ["company_id", "created_at"]
    )
    op.create_index(
        "ix_project_qa_traces_project_created", "project_qa_traces", ["project_id", "created_at"]
    )
    op.create_index(
        "ix_project_qa_traces_kb_created", "project_qa_traces", ["knowledge_base_id", "created_at"]
    )
    op.create_index(
        "ix_project_qa_traces_status_created", "project_qa_traces", ["status", "created_at"]
    )
    op.create_index(
        "ix_project_qa_traces_session_created", "project_qa_traces", ["session_ref", "created_at"]
    )

    op.create_table(
        "project_qa_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "attempt_index",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="同一次项目请求内的第几次检索，从 0 开始",
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("document_version_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(500), nullable=True),
        sa.Column("content_snapshot", sa.Text(), nullable=False),
        sa.Column("context_snapshot", sa.Text(), nullable=True),
        sa.Column("cosine_distance", sa.Float(), nullable=True),
        sa.Column("bm25_score", sa.Float(), nullable=True),
        sa.Column("rrf_score", sa.Float(), nullable=True),
        sa.Column("rerank_score", sa.Float(), nullable=True),
        sa.Column("asset_ids", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["trace_id"], ["project_qa_traces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", "attempt_index", "rank", name="uq_project_qa_sources_rank"),
        comment="项目轨迹召回片段与文本快照",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_project_qa_sources_trace_attempt", "project_qa_sources", ["trace_id", "attempt_index"]
    )
    op.create_index(
        "ix_project_qa_sources_company_created",
        "project_qa_sources",
        ["company_id", "created_at"],
    )
    op.create_index("ix_project_qa_sources_document", "project_qa_sources", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_project_qa_sources_document", table_name="project_qa_sources")
    op.drop_index("ix_project_qa_sources_company_created", table_name="project_qa_sources")
    op.drop_index("ix_project_qa_sources_trace_attempt", table_name="project_qa_sources")
    op.drop_table("project_qa_sources")
    op.drop_index("ix_project_qa_traces_session_created", table_name="project_qa_traces")
    op.drop_index("ix_project_qa_traces_status_created", table_name="project_qa_traces")
    op.drop_index("ix_project_qa_traces_kb_created", table_name="project_qa_traces")
    op.drop_index("ix_project_qa_traces_project_created", table_name="project_qa_traces")
    op.drop_index("ix_project_qa_traces_company_created", table_name="project_qa_traces")
    op.drop_table("project_qa_traces")
    op.drop_table("app_projects")
