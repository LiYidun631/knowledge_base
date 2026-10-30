"""管理员账号密码登录：凭据增加登录账号与密码哈希，新增登录会话表。"""

import sqlalchemy as sa
from alembic import op

revision = "0034_admin_login_accounts"
down_revision = "0033_admin_roles_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 存量凭据账号与密码留空：仍用原有长期 Token 登录，由管理员在页面开通账号密码。
    op.add_column(
        "admin_credentials",
        sa.Column(
            "username",
            sa.String(60),
            nullable=True,
            comment="登录账号，唯一；NULL 表示未开通账号密码登录",
        ),
    )
    op.add_column(
        "admin_credentials",
        sa.Column(
            "password_hash",
            sa.String(255),
            nullable=True,
            comment="PBKDF2 密码哈希，不保存明文",
        ),
    )
    op.create_index(
        "ix_admin_credentials_username", "admin_credentials", ["username"], unique=True
    )
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "credential_id",
            sa.Uuid(),
            nullable=False,
            comment="所属管理员凭据 ID",
        ),
        sa.Column(
            "token_hash",
            sa.String(64),
            nullable=False,
            comment="会话 Token 的 SHA-256 哈希，不保存明文",
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False, comment="会话过期时间（UTC）"),
        sa.Column("client_ip", sa.String(64), nullable=True, comment="登录时的客户端 IP"),
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
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["admin_credentials.id"],
            name="fk_admin_sessions_credential_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_admin_sessions_token_hash"),
        mysql_engine="InnoDB",
        comment="管理端登录会话",
    )
    op.create_index("ix_admin_sessions_credential_id", "admin_sessions", ["credential_id"])
    op.create_index("ix_admin_sessions_expires_at", "admin_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_sessions_expires_at", table_name="admin_sessions")
    op.drop_index("ix_admin_sessions_credential_id", table_name="admin_sessions")
    op.drop_table("admin_sessions")
    op.drop_index("ix_admin_credentials_username", table_name="admin_credentials")
    op.drop_column("admin_credentials", "password_hash")
    op.drop_column("admin_credentials", "username")
