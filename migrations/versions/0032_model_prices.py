"""生成模型计费单价表：按模型名记录每百万 token 的输入/输出价格，用于问答成本核算。"""

import sqlalchemy as sa
from alembic import op

revision = "0032_model_prices"
down_revision = "0031_rename_app_project"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_prices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False, comment="供应商模型名"),
        sa.Column("provider", sa.String(80), nullable=True, comment="模型供应商，仅用于展示"),
        sa.Column(
            "input_price_per_million",
            sa.Numeric(12, 6),
            nullable=False,
            server_default=sa.text("0"),
            comment="输入 token 单价（人民币/百万 token）",
        ),
        sa.Column(
            "output_price_per_million",
            sa.Numeric(12, 6),
            nullable=False,
            server_default=sa.text("0"),
            comment="输出 token 单价（人民币/百万 token）",
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_name", name="uq_model_prices_model_name"),
        mysql_engine="InnoDB",
        comment="生成模型 token 单价，用于问答成本核算",
    )


def downgrade() -> None:
    op.drop_table("model_prices")
