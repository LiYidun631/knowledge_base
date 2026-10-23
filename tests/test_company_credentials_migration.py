import importlib
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable


def test_company_credentials_migration_preserves_companies_and_enforces_scope():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    companies = sa.Table("companies", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    owner, other = uuid4(), uuid4()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            metadata.create_all(connection)
            connection.execute(companies.insert(), [{"id": owner}, {"id": other}])
            migration = importlib.import_module("migrations.versions.0014_company_credentials")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            credentials = sa.Table("company_credentials", sa.MetaData(), autoload_with=connection)
            assert set(credentials.c.keys()) == {
                "company_id",
                "token_hash",
                "is_enabled",
                "created_at",
                "updated_at",
            }
            assert connection.scalar(sa.select(sa.func.count()).select_from(credentials)) == 0
            connection.execute(
                credentials.insert(), {"company_id": owner.hex, "token_hash": "a" * 64}
            )
            for values in (
                {"company_id": owner.hex, "token_hash": "b" * 64},
                {"company_id": other.hex, "token_hash": "a" * 64},
                {"company_id": uuid4().hex, "token_hash": "c" * 64},
            ):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(credentials.insert(), values)
            connection.execute(companies.delete().where(companies.c.id == owner))
            assert connection.scalar(sa.select(sa.func.count()).select_from(credentials)) == 0
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert "company_credentials" not in sa.inspect(connection).get_table_names()
            assert connection.scalar(sa.select(companies.c.id)) == other
    finally:
        engine.dispose()


def test_company_credentials_mysql_ddl_stores_only_hash():
    from knowledge_service.infrastructure.persistence.models import CompanyCredential

    ddl = str(CreateTable(CompanyCredential.__table__).compile(dialect=mysql.dialect()))
    assert "ENGINE=InnoDB" in ddl
    assert "CHAR(32)" in ddl
    assert "VARCHAR(120)" in ddl
    assert "VARCHAR(64)" in ddl
    assert "PRIMARY KEY (id)" in ddl
    assert "UNIQUE (token_hash)" in ddl
    assert "UNIQUE (company_id, name)" in ddl
    assert {index.name for index in CompanyCredential.__table__.indexes} == {
        "ix_company_credentials_company_id"
    }
    assert "ON DELETE CASCADE" in ddl
    assert "token" not in CompanyCredential.__table__.c


def test_multi_company_credentials_migration_preserves_tokens():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    companies = sa.Table("companies", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    owner, other = uuid4(), uuid4()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            metadata.create_all(connection)
            connection.execute(companies.insert(), [{"id": owner}, {"id": other}])
            single = importlib.import_module("migrations.versions.0014_company_credentials")
            with Operations.context(MigrationContext.configure(connection)):
                single.upgrade()
            legacy = sa.Table(
                "company_credentials", sa.MetaData(), autoload_with=connection
            )
            connection.execute(
                legacy.insert(),
                [
                    {"company_id": owner.hex, "token_hash": "a" * 64, "is_enabled": True},
                    {"company_id": other.hex, "token_hash": "b" * 64, "is_enabled": False},
                ],
            )

            multi = importlib.import_module(
                "migrations.versions.0025_multi_company_credentials"
            )
            with Operations.context(MigrationContext.configure(connection)):
                multi.upgrade()

            credentials = sa.Table(
                "company_credentials", sa.MetaData(), autoload_with=connection
            )
            assert set(credentials.c.keys()) == {
                "id",
                "company_id",
                "name",
                "token_hash",
                "is_enabled",
                "created_at",
                "updated_at",
            }
            rows = {
                row.token_hash: row
                for row in connection.execute(sa.select(credentials)).mappings()
            }
            # 旧 Token 哈希原样保留，并补上项目名称与凭据 ID；不产生新明文。
            assert set(rows) == {"a" * 64, "b" * 64}
            assert all(row.name == "默认凭据" for row in rows.values())
            assert all(len(str(row.id)) == 32 for row in rows.values())
            assert not rows["b" * 64].is_enabled

            # 同公司可再签不同项目，但名称与哈希仍需唯一。
            connection.execute(
                credentials.insert(),
                {
                    "id": uuid4().hex,
                    "company_id": owner.hex,
                    "name": "项目 A",
                    "token_hash": "c" * 64,
                },
            )
            for values in (
                {
                    "id": uuid4().hex,
                    "company_id": owner.hex,
                    "name": "项目 A",
                    "token_hash": "d" * 64,
                },
                {
                    "id": uuid4().hex,
                    "company_id": other.hex,
                    "name": "默认凭据",
                    "token_hash": "e" * 64,
                },
                {
                    "id": uuid4().hex,
                    "company_id": owner.hex,
                    "name": "项目 B",
                    "token_hash": "a" * 64,
                },
                {
                    "id": uuid4().hex,
                    "company_id": uuid4().hex,
                    "name": "孤立",
                    "token_hash": "f" * 64,
                },
            ):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(credentials.insert(), values)

            connection.execute(companies.delete().where(companies.c.id == owner))
            assert connection.scalar(
                sa.select(sa.func.count())
                .select_from(credentials)
                .where(credentials.c.company_id == owner.hex)
            ) == 0

            with Operations.context(MigrationContext.configure(connection)):
                multi.downgrade()
            legacy = sa.Table(
                "company_credentials", sa.MetaData(), autoload_with=connection
            )
            assert set(legacy.c.keys()) == {
                "company_id",
                "token_hash",
                "is_enabled",
                "created_at",
                "updated_at",
            }
            # 降级到每公司一枚时只保留一枚；owner 已随公司删除，other 保留其停用凭据。
            remaining = connection.execute(sa.select(legacy)).mappings().all()
            assert [str(row.company_id).replace("-", "") for row in remaining] == [other.hex]
            assert remaining[0].token_hash == "b" * 64
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    legacy.insert(),
                    {"company_id": other.hex, "token_hash": "9" * 64},
                )
    finally:
        engine.dispose()
