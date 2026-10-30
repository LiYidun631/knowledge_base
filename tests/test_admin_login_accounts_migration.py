"""迁移 0034：管理员登录账号、密码哈希与登录会话表。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable


def _legacy_admin_table() -> sa.Table:
    """0033 之后的管理员表，用于验证升级只追加不破坏。"""
    metadata = sa.MetaData()
    return sa.Table(
        "admin_credentials",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("role", sa.String(20), nullable=False, server_default=sa.text("'admin'")),
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


def test_admin_login_accounts_migration_upgrade_and_downgrade() -> None:
    """升级后可用账号登录且存量凭据不丢会话列；降级移除账号列与会话表。"""
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
            migration = importlib.import_module("migrations.versions.0034_admin_login_accounts")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

            inspector = sa.inspect(connection)
            columns = {
                column["name"]: column
                for column in inspector.get_columns("admin_credentials")
            }
            assert {"username", "password_hash"} <= set(columns)
            assert columns["username"]["nullable"] is True
            assert columns["password_hash"]["nullable"] is True
            assert connection.execute(
                sa.text("SELECT username FROM admin_credentials")
            ).scalar_one() is None
            assert {
                index["name"] for index in inspector.get_indexes("admin_credentials")
            } >= {"ix_admin_credentials_username"}

            session_columns = {
                column["name"] for column in inspector.get_columns("admin_sessions")
            }
            assert {
                "id",
                "credential_id",
                "token_hash",
                "expires_at",
                "client_ip",
                "created_at",
                "updated_at",
            } <= session_columns
            assert {
                index["name"] for index in inspector.get_indexes("admin_sessions")
            } >= {"ix_admin_sessions_credential_id", "ix_admin_sessions_expires_at"}
            assert [
                foreign_key["name"]
                for foreign_key in inspector.get_foreign_keys("admin_sessions")
            ] == ["fk_admin_sessions_credential_id"]

            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            inspector = sa.inspect(connection)
            assert not {"username", "password_hash"} & {
                column["name"] for column in inspector.get_columns("admin_credentials")
            }
            assert "admin_sessions" not in inspector.get_table_names()
    finally:
        engine.dispose()


def test_admin_login_accounts_mysql_ddl_matches_model() -> None:
    """会话表与账号列在 MySQL 上的类型与引擎形状和模型一致。"""
    from knowledge_service.infrastructure.persistence.models import (
        AdminCredential,
        AdminSession,
    )

    session_ddl = str(CreateTable(AdminSession.__table__).compile(dialect=mysql.dialect()))
    assert "CHAR(32)" in session_ddl
    assert "VARCHAR(64)" in session_ddl
    assert "ON DELETE CASCADE" in session_ddl
    assert "ENGINE=InnoDB" in session_ddl

    credential_ddl = str(
        CreateTable(AdminCredential.__table__).compile(dialect=mysql.dialect())
    )
    assert "username VARCHAR(60)" in credential_ddl
    assert "password_hash VARCHAR(255)" in credential_ddl
