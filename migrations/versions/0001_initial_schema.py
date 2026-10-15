"""Initial service schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_innodb_table(name, *columns, **options) -> None:
    """统一用 InnoDB 建表，确保 MySQL 外键和事务约束生效。"""
    options.setdefault("mysql_engine", "InnoDB")
    op.create_table(name, *columns, **options)


def upgrade() -> None:
    _create_innodb_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_projects_slug"),
    )
    _create_innodb_table(
        "model_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=True),
        sa.Column(
            "settings",
            sa.JSON(),
            nullable=False,
        ),
        sa.Column("secret_ref", sa.String(length=200), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
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
        sa.CheckConstraint("kind IN ('embedding', 'rerank')", name="ck_model_profiles_kind"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "name", name="uq_model_profiles_kind_name"),
    )
    op.create_index(
        "ix_model_profiles_kind_enabled",
        "model_profiles",
        ["kind", "is_enabled"],
        unique=False,
    )
    _create_innodb_table(
        "vector_store_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("backend", sa.String(length=30), nullable=False),
        sa.Column(
            "settings",
            sa.JSON(),
            nullable=False,
        ),
        sa.Column("secret_ref", sa.String(length=200), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
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
        sa.CheckConstraint("backend = 'milvus'", name="ck_vector_store_profiles_backend"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_vector_store_profiles_name"),
    )
    _create_innodb_table(
        "api_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("key_prefix", sa.String(length=20), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "scopes",
            sa.JSON(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("secret_hash", name="uq_api_credentials_secret_hash"),
    )
    op.create_index(
        "ix_api_credentials_project_id", "api_credentials", ["project_id"], unique=False
    )
    _create_innodb_table(
        "knowledge_bases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("embedding_model_id", sa.Uuid(), nullable=False),
        sa.Column("rerank_model_id", sa.Uuid(), nullable=True),
        sa.Column("vector_store_profile_id", sa.Uuid(), nullable=False),
        sa.Column("models_locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_knowledge_bases_status"),
        sa.ForeignKeyConstraint(["embedding_model_id"], ["model_profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rerank_model_id"], ["model_profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["vector_store_profile_id"],
            ["vector_store_profiles.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", name="uq_knowledge_bases_project_name"),
    )
    op.create_index(
        "ix_knowledge_bases_project_id", "knowledge_bases", ["project_id"], unique=False
    )
    _create_innodb_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("media_type", sa.String(length=160), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=1000), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "extra_metadata",
            sa.JSON(),
            nullable=False,
        ),
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
            "status IN ('queued', 'processing', 'completed', 'failed', 'deleting')",
            name="ck_documents_status",
        ),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_documents_knowledge_base_id", "documents", ["knowledge_base_id"], unique=False
    )
    op.create_index("ix_documents_status", "documents", ["status"], unique=False)
    _create_innodb_table(
        "ingestion_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "error_details",
            sa.JSON(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_ingestion_jobs_status",
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ingestion_jobs_status_created_at",
        "ingestion_jobs",
        ["status", "created_at"],
        unique=False,
    )
    _create_innodb_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column(
            "extra_metadata",
            sa.JSON(),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_document_chunks_document_ordinal"),
    )
    op.create_index(
        "ix_document_chunks_document_id", "document_chunks", ["document_id"], unique=False
    )
    _create_innodb_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("aggregate_type", sa.String(length=80), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON(),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('pending', 'processing', 'published', 'failed')",
            name="ck_outbox_events_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_events_status_created_at",
        "outbox_events",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_status_created_at", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_document_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_ingestion_jobs_status_created_at", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_index("ix_documents_knowledge_base_id", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_knowledge_bases_project_id", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
    op.drop_index("ix_api_credentials_project_id", table_name="api_credentials")
    op.drop_table("api_credentials")
    op.drop_table("vector_store_profiles")
    op.drop_index("ix_model_profiles_kind_enabled", table_name="model_profiles")
    op.drop_table("model_profiles")
    op.drop_table("projects")
