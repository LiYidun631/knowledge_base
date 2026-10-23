import importlib
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable


def _base_tables(metadata: sa.MetaData) -> tuple[sa.Table, sa.Table]:
    profiles = sa.Table("model_profiles", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    bases = sa.Table(
        "knowledge_bases",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
    )
    return profiles, bases


def test_admin_parameters_migration_adds_columns_and_enforces_chunk_rules():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    profiles, bases = _base_tables(metadata)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            metadata.create_all(connection)
            connection.execute(profiles.insert(), [{"id": uuid4()}])
            connection.execute(bases.insert(), [{"id": uuid4(), "name": "库"}])
            migration = importlib.import_module("migrations.versions.0015_admin_parameters")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            inspector = sa.inspect(connection)
            assert "api_key_cipher" in {
                column["name"] for column in inspector.get_columns("model_profiles")
            }
            for name in ("chunk_parent_chars", "chunk_child_chars", "chunk_child_overlap_chars"):
                assert name in {
                    column["name"] for column in inspector.get_columns("knowledge_bases")
                }
            chunked = sa.Table("knowledge_bases", sa.MetaData(), autoload_with=connection)
            # 合法组合：全部为空、只有子块、父块等于子块、重叠小于子块。
            connection.execute(
                chunked.insert(),
                [
                    {"id": "c" * 32, "name": "全空"},
                    {"id": "d" * 32, "name": "只有子块", "chunk_child_chars": 300},
                    {
                        "id": "e" * 32,
                        "name": "相等",
                        "chunk_parent_chars": 600,
                        "chunk_child_chars": 600,
                    },
                    {
                        "id": "f" * 32,
                        "name": "合法",
                        "chunk_parent_chars": 1800,
                        "chunk_child_chars": 600,
                        "chunk_child_overlap_chars": 80,
                    },
                ],
            )
            for values in (
                {
                    "id": "1" * 32,
                    "name": "重叠过大",
                    "chunk_child_chars": 600,
                    "chunk_child_overlap_chars": 600,
                },
                {
                    "id": "2" * 32,
                    "name": "重叠超限",
                    "chunk_child_chars": 600,
                    "chunk_child_overlap_chars": 601,
                },
                {
                    "id": "3" * 32,
                    "name": "父小于子",
                    "chunk_parent_chars": 300,
                    "chunk_child_chars": 600,
                },
            ):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(chunked.insert(), values)
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            # Inspector 会缓存反射结果，降级后需重新反射表结构。
            columns = set(
                sa.Table("knowledge_bases", sa.MetaData(), autoload_with=connection).c.keys()
            )
            assert not columns & {
                "chunk_parent_chars",
                "chunk_child_chars",
                "chunk_child_overlap_chars",
            }
            profile_columns = set(
                sa.Table("model_profiles", sa.MetaData(), autoload_with=connection).c.keys()
            )
            assert "api_key_cipher" not in profile_columns
    finally:
        engine.dispose()


def test_admin_parameters_mysql_ddl_has_no_plaintext_key_column():
    from knowledge_service.infrastructure.persistence.models import KnowledgeBase, ModelProfile

    profiles_ddl = str(CreateTable(ModelProfile.__table__).compile(dialect=mysql.dialect()))
    assert "api_key_cipher TEXT" in profiles_ddl
    assert "api_key " not in profiles_ddl
    bases_ddl = str(CreateTable(KnowledgeBase.__table__).compile(dialect=mysql.dialect()))
    assert "chunk_parent_chars INTEGER" in bases_ddl
    assert "ck_knowledge_bases_chunk_overlap_lt_child" in bases_ddl
    assert "ck_knowledge_bases_chunk_parent_gte_child" in bases_ddl
