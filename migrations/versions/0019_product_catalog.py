"""产品目录表；上传标签与检索过滤对齐的唯一来源。"""

import sqlalchemy as sa
from alembic import op

revision = "0019_product_catalog"
down_revision = "0018_admin_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.create_table(
            "product_catalog",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
            sa.Column("product_name", sa.String(length=200), nullable=False),
            sa.Column("model_name", sa.String(length=200), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=False),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=False),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "knowledge_base_id", "product_name", "model_name",
                name="uq_product_catalog_kb_product_model",
            ),
            sqlite_autoincrement=True,
            comment="知识库产品名称与型号目录",
        )
        op.create_index(
            "ix_product_catalog_knowledge_base_id",
            "product_catalog",
            ["knowledge_base_id"],
        )
        return

    op.create_table(
        "product_catalog",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "knowledge_base_id",
            sa.Uuid(),
            nullable=False,
            comment="所属知识库，随子库级联删除",
        ),
        sa.Column("product_name", sa.String(length=200), nullable=False, comment="产品名称"),
        sa.Column("model_name", sa.String(length=200), nullable=False, comment="产品型号"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "knowledge_base_id", "product_name", "model_name",
            name="uq_product_catalog_kb_product_model",
        ),
        mysql_engine="InnoDB",
        comment="知识库产品名称与型号目录",
    )
    op.create_index(
        "ix_product_catalog_knowledge_base_id",
        "product_catalog",
        ["knowledge_base_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_product_catalog_knowledge_base_id", table_name="product_catalog")
    op.drop_table("product_catalog")
