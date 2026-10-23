"""问答记录增加 FAQ 直接命中标记，便于统计直出占比。"""

import sqlalchemy as sa
from alembic import op

revision = "0024_qa_faq_matched"
down_revision = "0023_faq_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            "ALTER TABLE qa_records ADD COLUMN faq_matched TINYINT(1) NOT NULL "
            "DEFAULT 0 COMMENT '答案是否来自 FAQ 直接命中'"
        )
        return

    with op.batch_alter_table("qa_records") as batch:
        batch.add_column(
            sa.Column(
                "faq_matched",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("ALTER TABLE qa_records DROP COLUMN faq_matched")
        return

    with op.batch_alter_table("qa_records") as batch:
        batch.drop_column("faq_matched")
