"""0017 迁移：model_profiles 的 kind 检查约束放宽到 chat。"""

import importlib
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError


def test_chat_model_profiles_migration_switches_kind_check():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    profiles = sa.Table(
        "model_profiles",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.CheckConstraint(
            "kind IN ('embedding', 'rerank')", name="ck_model_profiles_kind"
        ),
    )
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(profiles.insert(), [{"id": uuid4(), "kind": "embedding"}])

    migration = importlib.import_module(
        "migrations.versions.0017_chat_model_profiles"
    )
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        chat = sa.Table(
            "model_profiles",
            sa.MetaData(),
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("kind", sa.String(20)),
        )
        # 升级后 chat 可写入，未知类型仍被拒绝。
        connection.execute(chat.insert(), [{"id": uuid4(), "kind": "chat"}])
        with pytest.raises(IntegrityError):
            connection.execute(chat.insert(), [{"id": uuid4(), "kind": "bogus"}])
        # 存在 chat 行时降级会被旧约束挡住；清空后降级恢复旧约束。
        connection.execute(chat.delete().where(chat.c.kind == "chat"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        with pytest.raises(IntegrityError):
            connection.execute(chat.insert(), [{"id": uuid4(), "kind": "chat"}])


def test_chat_model_profiles_migration_mysql_version_gate():
    """MySQL 8.0.16 以下解析即忽略 CHECK，约束实际不存在，迁移不得发 DROP CHECK。"""
    migration = importlib.import_module(
        "migrations.versions.0017_chat_model_profiles"
    )

    def run_with_version(version: tuple[int, ...]) -> list[str]:
        statements: list[str] = []

        def capture(sql, *args, **kwargs):
            statements.append(str(sql.compile(dialect=sa.dialects.mysql.dialect())))

        engine = sa.create_mock_engine("mysql://u:p@localhost/db", capture)
        engine.dialect.server_version_info = version
        connection = engine.connect()
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        return statements

    assert run_with_version((8, 0, 12)) == []
    assert run_with_version((8, 0, 16)) == [
        "ALTER TABLE model_profiles DROP CHECK ck_model_profiles_kind",
        "ALTER TABLE model_profiles ADD CONSTRAINT ck_model_profiles_kind "
        "CHECK (kind IN ('embedding', 'rerank', 'chat'))",
    ]
