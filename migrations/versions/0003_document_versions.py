"""增加不可覆盖的文档版本，并按版本隔离任务、分块和向量。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_document_versions"
down_revision: str | None = "0002_model_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_innodb_table(name, *columns, **options) -> None:
    """文档版本表使用 InnoDB，保证外键和唯一生成列索引生效。"""
    options.setdefault("mysql_engine", "InnoDB")
    op.create_table(name, *columns, **options)


def upgrade() -> None:
    # 先给已有逻辑文档分配独立键，避免旧数据中的同名文件发生冲突。
    op.add_column("documents", sa.Column("document_key", sa.String(length=500), nullable=True))
    op.add_column(
        "documents",
        sa.Column("latest_version_number", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE documents "
        "SET document_key = CONCAT('legacy:', id), latest_version_number = 1"
    )
    op.alter_column(
        "documents",
        "document_key",
        existing_type=sa.String(length=500),
        nullable=False,
    )
    op.alter_column(
        "documents",
        "latest_version_number",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("0"),
    )
    op.create_unique_constraint(
        "uq_documents_kb_document_key", "documents", ["knowledge_base_id", "document_key"]
    )

    _create_innodb_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("media_type", sa.String(length=160), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=1000), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column(
            "current_document_id",
            sa.Uuid(),
            sa.Computed("CASE WHEN is_current THEN document_id ELSE NULL END"),
            nullable=True,
        ),
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
        sa.CheckConstraint("version_number > 0", name="ck_document_versions_positive_number"),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed', 'deleting')",
            name="ck_document_versions_status",
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "version_number", name="uq_document_versions_number"),
    )
    op.create_index(
        "ix_document_versions_document_id", "document_versions", ["document_id"], unique=False
    )
    op.create_index("ix_document_versions_status", "document_versions", ["status"], unique=False)
    op.create_index(
        "uq_document_versions_one_current",
        "document_versions",
        ["current_document_id"],
        unique=True,
    )

    # 将旧 documents 行复制为 V1；当前已完成的记录继续作为当前版本。
    op.execute(
        """
        INSERT INTO document_versions (
            id, document_id, version_number, filename, media_type, size_bytes,
            content_sha256, object_key, status, is_current, error_message,
            extra_metadata, created_at, updated_at
        )
        SELECT id, id, 1, filename, media_type, size_bytes, content_sha256,
               object_key, status, (status = 'completed'), error_message,
               extra_metadata, created_at, updated_at
        FROM documents
        """
    )

    # 每个旧任务和分块都属于对应文档的 V1。
    op.add_column("ingestion_jobs", sa.Column("document_version_id", sa.Uuid(), nullable=True))
    op.execute("UPDATE ingestion_jobs SET document_version_id = document_id")
    op.alter_column(
        "ingestion_jobs", "document_version_id", existing_type=sa.Uuid(), nullable=False
    )
    op.create_foreign_key(
        "fk_ingestion_jobs_document_version_id_document_versions",
        "ingestion_jobs",
        "document_versions",
        ["document_version_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.add_column("document_chunks", sa.Column("document_version_id", sa.Uuid(), nullable=True))
    op.add_column("document_chunks", sa.Column("parent_chunk_id", sa.Uuid(), nullable=True))
    op.add_column("document_chunks", sa.Column("level", sa.String(length=10), nullable=True))
    op.add_column(
        "document_chunks", sa.Column("content_sha256", sa.String(length=64), nullable=True)
    )
    op.execute("UPDATE document_chunks SET document_version_id = document_id")
    op.execute(
        """
        UPDATE document_chunks
        SET level = COALESCE(JSON_UNQUOTE(JSON_EXTRACT(extra_metadata, '$.level')), 'child'),
            parent_chunk_id = NULLIF(REPLACE(JSON_UNQUOTE(
                JSON_EXTRACT(extra_metadata, '$.parent_chunk_id')
            ), '-', ''), 'null'),
            content_sha256 = SHA2(content, 256)
        """
    )
    op.alter_column(
        "document_chunks", "document_version_id", existing_type=sa.Uuid(), nullable=False
    )
    op.alter_column(
        "document_chunks", "level", existing_type=sa.String(length=10), nullable=False
    )
    op.alter_column(
        "document_chunks", "content_sha256", existing_type=sa.String(length=64), nullable=False
    )
    op.drop_constraint("uq_document_chunks_document_ordinal", "document_chunks", type_="unique")
    op.create_unique_constraint(
        "uq_document_chunks_version_ordinal",
        "document_chunks",
        ["document_version_id", "ordinal"],
    )
    op.create_foreign_key(
        "fk_document_chunks_document_version_id_document_versions",
        "document_chunks",
        "document_versions",
        ["document_version_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_document_chunks_parent_chunk_id_document_chunks",
        "document_chunks",
        "document_chunks",
        ["parent_chunk_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_document_chunks_level", "document_chunks", "level IN ('parent', 'child')"
    )
    op.create_index(
        "ix_document_chunks_version_id", "document_chunks", ["document_version_id"], unique=False
    )
    op.create_index(
        "ix_document_chunks_parent_id", "document_chunks", ["parent_chunk_id"], unique=False
    )

    # 文件信息只属于版本，逻辑文档不再重复保存某一版的内容信息。
    for column in ("media_type", "size_bytes", "content_sha256", "object_key"):
        op.drop_column("documents", column)

    # 旧的 Outbox 记录补上 V1 标识，保持消息数据结构一致。
    op.execute(
        """
        UPDATE outbox_events
        SET payload = JSON_SET(payload, '$.document_version_id', aggregate_id)
        WHERE event_type = 'document.ingestion_requested'
          AND JSON_CONTAINS_PATH(payload, 'one', '$.document_version_id') = 0
        """
    )


def downgrade() -> None:
    # 旧表每篇文档只允许一套分块，优先保留当前版本；没有当前版本时保留最新上传版。
    op.execute(
        """
        DELETE chunk_row
        FROM document_chunks AS chunk_row
        JOIN document_versions AS version ON version.id = chunk_row.document_version_id
        JOIN documents AS document ON document.id = version.document_id
        WHERE version.is_current = 0
          AND EXISTS (
              SELECT 1 FROM document_versions AS current_version
              WHERE current_version.document_id = document.id
                AND current_version.is_current = 1
          )
        """
    )
    op.execute(
        """
        DELETE chunk_row
        FROM document_chunks AS chunk_row
        JOIN document_versions AS version ON version.id = chunk_row.document_version_id
        JOIN documents AS document ON document.id = version.document_id
        WHERE version.version_number <> document.latest_version_number
          AND NOT EXISTS (
              SELECT 1 FROM document_versions AS current_version
              WHERE current_version.document_id = document.id
                AND current_version.is_current = 1
          )
        """
    )
    # 恢复当前版本；从未成功入库的文档则退回最新上传版本的信息。
    for column, kind in (
        ("media_type", sa.String(length=160)),
        ("size_bytes", sa.Integer()),
        ("content_sha256", sa.String(length=64)),
        ("object_key", sa.String(length=1000)),
    ):
        op.add_column("documents", sa.Column(column, kind, nullable=True))
    op.execute(
        """
        UPDATE documents AS document
        JOIN document_versions AS version ON version.document_id = document.id
        SET document.filename = version.filename,
            document.status = version.status,
            document.error_message = version.error_message,
            document.media_type = version.media_type,
            document.size_bytes = version.size_bytes,
            document.content_sha256 = version.content_sha256,
            document.object_key = version.object_key
        WHERE version.is_current = 1
        """
    )
    op.execute(
        """
        UPDATE documents AS document
        JOIN document_versions AS version
          ON version.document_id = document.id
         AND version.version_number = document.latest_version_number
        SET document.filename = version.filename,
            document.status = version.status,
            document.error_message = version.error_message,
            document.media_type = version.media_type,
            document.size_bytes = version.size_bytes,
            document.content_sha256 = version.content_sha256,
            document.object_key = version.object_key
        WHERE NOT EXISTS (
            SELECT 1 FROM document_versions AS current_version
            WHERE current_version.document_id = document.id
              AND current_version.is_current = 1
        )
        """
    )
    op.alter_column("documents", "size_bytes", existing_type=sa.Integer(), nullable=False)
    op.alter_column(
        "documents", "content_sha256", existing_type=sa.String(length=64), nullable=False
    )
    op.alter_column("documents", "object_key", existing_type=sa.String(length=1000), nullable=False)

    op.drop_index("ix_document_chunks_version_id", table_name="document_chunks")
    op.drop_index("ix_document_chunks_parent_id", table_name="document_chunks")
    op.drop_constraint("ck_document_chunks_level", "document_chunks", type_="check")
    op.drop_constraint(
        "fk_document_chunks_parent_chunk_id_document_chunks",
        "document_chunks",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_document_chunks_document_version_id_document_versions",
        "document_chunks",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_document_chunks_version_ordinal", "document_chunks", type_="unique"
    )
    op.create_unique_constraint(
        "uq_document_chunks_document_ordinal", "document_chunks", ["document_id", "ordinal"]
    )
    op.drop_column("document_chunks", "document_version_id")
    op.drop_column("document_chunks", "parent_chunk_id")
    op.drop_column("document_chunks", "level")
    op.drop_column("document_chunks", "content_sha256")

    op.drop_constraint(
        "fk_ingestion_jobs_document_version_id_document_versions",
        "ingestion_jobs",
        type_="foreignkey",
    )
    op.drop_column("ingestion_jobs", "document_version_id")

    op.drop_constraint("uq_documents_kb_document_key", "documents", type_="unique")
    op.drop_column("documents", "latest_version_number")
    op.drop_column("documents", "document_key")
    op.drop_index("uq_document_versions_one_current", table_name="document_versions")
    op.drop_index("ix_document_versions_status", table_name="document_versions")
    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.drop_table("document_versions")
