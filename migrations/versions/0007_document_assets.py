"""保存版面分析提取的图片资产，并关联到文档版本和父块。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_document_assets"
down_revision: str | None = "0006_bm25_terms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增图片资产表，不修改既有文档、向量和 BM25 数据。"""
    op.create_table(
        "document_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("object_key", sa.String(length=1000), nullable=False),
        sa.Column("media_type", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("bbox", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        comment="文档图片及版面资产",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_document_assets_version_id", "document_assets", ["document_version_id"])
    op.create_index("ix_document_assets_chunk_id", "document_assets", ["chunk_id"])


def downgrade() -> None:
    """删除图片资产表；不会删除原始文件和 Chroma 数据。"""
    op.drop_index("ix_document_assets_chunk_id", table_name="document_assets")
    op.drop_index("ix_document_assets_version_id", table_name="document_assets")
    op.drop_table("document_assets")
