"""迁移 0033：管理员角色列与审计日志表。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable


def _legacy_admin_table() -> sa.Table:
    metadata = sa.MetaData()
    return sa.Table(
        "admin_credentials",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
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


def test_admin_roles_audit_migration_upgrade_and_downgrade() -> None:
    """升级后存量凭据为 admin，审计表带索引；降级移除角色列与审计表。"""
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        table = _legacy_admin_table()
        table.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO admin_credentials (id, name, token_hash, is_enabled) "
                    "VALUES ('a' || substr(hex(randomblob(15)), 1, 31), '老管理员', :hash, 1)"
                ),
                {"hash": "f" * 64},
            )
            migration = importlib.import_module("migrations.versions.0033_admin_roles_audit")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

            inspector = sa.inspect(connection)
            assert "role" in {
                column["name"] for column in inspector.get_columns("admin_credentials")
            }
            assert connection.execute(
                sa.text("SELECT role FROM admin_credentials")
            ).scalar_one() == "admin"

            audit_columns = {
                column["name"] for column in inspector.get_columns("audit_logs")
            }
            assert {
                "id",
                "actor_name",
                "actor_role",
                "credential_id",
                "method",
                "path",
                "status_code",
                "duration_ms",
                "client_ip",
                "created_at",
                "updated_at",
            } <= audit_columns
            assert {
                index["name"] for index in inspector.get_indexes("audit_logs")
            } >= {"ix_audit_logs_created_at", "ix_audit_logs_actor_name"}

            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            inspector = sa.inspect(connection)
            assert "role" not in {
                column["name"] for column in inspector.get_columns("admin_credentials")
            }
            assert "audit_logs" not in inspector.get_table_names()
    finally:
        engine.dispose()


def test_audit_log_mysql_ddl_matches_model() -> None:
    """审计表在 MySQL 上使用 CHAR(32) 主键与 InnoDB。"""
    from knowledge_service.infrastructure.persistence.models import (
        AdminCredential,
        AuditLog,
    )

    audit_ddl = str(CreateTable(AuditLog.__table__).compile(dialect=mysql.dialect()))
    assert "CHAR(32)" in audit_ddl
    assert "VARCHAR(120)" in audit_ddl
    assert "VARCHAR(500)" in audit_ddl
    assert "ENGINE=InnoDB" in audit_ddl

    credential_ddl = str(
        CreateTable(AdminCredential.__table__).compile(dialect=mysql.dialect())
    )
    assert "`role` VARCHAR(20)" in credential_ddl
    assert "DEFAULT 'admin'" in credential_ddl
