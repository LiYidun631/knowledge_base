"""提示词覆写表：管理页编辑的系统提示词优先于提示词文件生效。"""

import sqlalchemy as sa
from alembic import op

revision = "0022_admin_prompt_overrides"
down_revision = "0021_qa_review_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            "CREATE TABLE admin_prompt_overrides ("
            "name VARCHAR(100) NOT NULL COMMENT '提示词名称', "
            "content TEXT NOT NULL COMMENT '覆写内容', "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (name)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='管理端提示词覆写'"
        )
        return

    op.create_table(
        "admin_prompt_overrides",
        sa.Column("name", sa.String(100), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    op.drop_table("admin_prompt_overrides")
