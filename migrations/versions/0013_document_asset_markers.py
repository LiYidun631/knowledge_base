"""保存图片在原文中的稳定占位符。"""

import sqlalchemy as sa
from alembic import op

revision = "0013_document_asset_markers"
down_revision = "0012_company_name_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_assets",
        sa.Column(
            "marker",
            sa.String(length=300),
            nullable=True,
            comment="图片在原文中的稳定占位符",
        ),
    )
    op.add_column(
        "document_assets",
        sa.Column("location", sa.JSON(), nullable=True, comment="图片在原文中的位置元数据"),
    )


def downgrade() -> None:
    op.drop_column("document_assets", "location")
    op.drop_column("document_assets", "marker")
