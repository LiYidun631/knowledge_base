"""公司凭据入库，仅保存不可回读的 Token 哈希。"""

import sqlalchemy as sa
from alembic import op

revision = "0014_company_credentials"
down_revision = "0013_document_asset_markers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 不为已有公司自动签发凭据，避免迁移时产生无人接收的明文密钥。
    op.create_table(
        "company_credentials",
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
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
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("company_id"),
        sa.UniqueConstraint("token_hash", name="uq_company_credentials_token_hash"),
        mysql_engine="InnoDB",
        comment="公司访问凭据，仅保存哈希",
    )


def downgrade() -> None:
    op.drop_table("company_credentials")
