"""管理员访问凭据表；支持多管理员 Token，仅保存哈希。"""

import sqlalchemy as sa
from alembic import op

revision = "0018_admin_credentials"
down_revision = "0017_chat_model_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.create_table(
            "admin_credentials",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "is_enabled",
                sa.Boolean(),
                server_default=sa.text("1"),
                nullable=False,
            ),
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
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash", name="uq_admin_credentials_token_hash"),
            sqlite_autoincrement=True,
            comment="管理员访问凭据，仅保存哈希",
        )
        return

    op.create_table(
        "admin_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False, comment="管理员标识"),
        sa.Column("token_hash", sa.String(length=64), nullable=False, comment="凭据哈希"),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
            comment="是否启用",
        ),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_admin_credentials_token_hash"),
        mysql_engine="InnoDB",
        comment="管理员访问凭据，仅保存哈希",
    )


def downgrade() -> None:
    op.drop_table("admin_credentials")
