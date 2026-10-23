"""问答记录增加复核状态列，支撑待补资料闭环。"""

import sqlalchemy as sa
from alembic import op

revision = "0021_qa_review_status"
down_revision = "0020_product_catalog_global"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            "ALTER TABLE qa_records ADD COLUMN review_status VARCHAR(20) NOT NULL "
            "DEFAULT 'none' COMMENT '复核状态：none/pending/resolved'"
        )
        op.execute(
            "ALTER TABLE qa_records ADD CONSTRAINT ck_qa_records_review_status "
            "CHECK (review_status IN ('none', 'pending', 'resolved'))"
        )
        op.execute(
            "CREATE INDEX ix_qa_records_review_created "
            "ON qa_records (review_status, created_at)"
        )
        return

    with op.batch_alter_table("qa_records") as batch:
        batch.add_column(
            sa.Column("review_status", sa.String(20), nullable=False, server_default="none")
        )
        batch.create_check_constraint(
            "ck_qa_records_review_status",
            "review_status IN ('none', 'pending', 'resolved')",
        )
        batch.create_index(
            "ix_qa_records_review_created", ["review_status", "created_at"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("ALTER TABLE qa_records DROP INDEX ix_qa_records_review_created")
        op.execute(
            "ALTER TABLE qa_records DROP CHECK ck_qa_records_review_status"
        )
        op.execute("ALTER TABLE qa_records DROP COLUMN review_status")
        return

    with op.batch_alter_table("qa_records") as batch:
        batch.drop_index("ix_qa_records_review_created")
        batch.drop_constraint("ck_qa_records_review_status", type_="check")
        batch.drop_column("review_status")
