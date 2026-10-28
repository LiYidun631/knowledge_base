"""迁移 0032：生成模型单价表。"""

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable


def test_model_prices_migration_creates_priced_table() -> None:
    """重建单价表、模型名唯一，降级后整表移除。"""
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            migration = importlib.import_module("migrations.versions.0032_model_prices")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            columns = {
                column["name"] for column in sa.inspect(connection).get_columns("model_prices")
            }
            assert {
                "id",
                "model_name",
                "provider",
                "input_price_per_million",
                "output_price_per_million",
                "created_at",
                "updated_at",
            } <= columns
            prices = sa.Table("model_prices", sa.MetaData(), autoload_with=connection)
            connection.execute(
                prices.insert(),
                {
                    "id": "a" * 32,
                    "model_name": "qwen3.8-flash",
                    "input_price_per_million": 2,
                    "output_price_per_million": 6,
                },
            )
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    prices.insert(),
                    {
                        "id": "b" * 32,
                        "model_name": "qwen3.8-flash",
                        "input_price_per_million": 1,
                        "output_price_per_million": 1,
                    },
                )
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert "model_prices" not in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()


def test_model_prices_mysql_ddl_matches_model() -> None:
    """MySQL 建表语句保留 6 位小数精度与模型名唯一约束。"""
    from knowledge_service.infrastructure.persistence.models import ModelPrice

    ddl = str(CreateTable(ModelPrice.__table__).compile(dialect=mysql.dialect()))
    assert "input_price_per_million NUMERIC(12, 6) NOT NULL" in ddl
    assert "output_price_per_million NUMERIC(12, 6) NOT NULL" in ddl
    assert "uq_model_prices_model_name" in ddl
