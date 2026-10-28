"""管理员角色与写操作审计：凭据增加三档角色，新增审计日志表。"""

import sqlalchemy as sa
from alembic import op

revision = "0033_admin_roles_audit"
down_revision = "0032_model_prices"
branch_labels = None
depends_on = None

_ROLE_DEFAULT = "admin"


def upgrade() -> None:
    # 存量凭据全部按 admin 保留原有权限，避免升级后突然失去管理能力。
    op.add_column(
        "admin_credentials",
        sa.Column(
            "role",
            sa.String(20),
            nullable=False,
            server_default=sa.text(f"'{_ROLE_DEFAULT}'"),
            comment="角色：admin 全权、operator 运营、viewer 只读",
        ),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_name", sa.String(120), nullable=False, comment="操作者名称快照"),
        sa.Column("actor_role", sa.String(20), nullable=False, comment="操作者角色快照"),
        sa.Column(
            "credential_id",
            sa.Uuid(),
            nullable=True,
            comment="凭据 ID；环境引导 Token 为空",
        ),
        sa.Column("method", sa.String(10), nullable=False, comment="HTTP 方法"),
        sa.Column("path", sa.String(500), nullable=False, comment="请求路径，不含查询串"),
        sa.Column("status_code", sa.Integer(), nullable=False, comment="响应状态码"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, comment="处理耗时（毫秒）"),
        sa.Column("client_ip", sa.String(64), nullable=True, comment="客户端 IP"),
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
        mysql_engine="InnoDB",
        comment="管理端写操作审计日志",
    )
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_actor_name", "audit_logs", ["actor_name"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_actor_name", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_column("admin_credentials", "role")
