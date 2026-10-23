"""0020 迁移：产品目录去掉知识库归属，改为产品+型号全局唯一。"""

import importlib
from datetime import datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

_MIGRATION = importlib.import_module("migrations.versions.0020_product_catalog_global")


def _build_old_schema(engine: sa.Engine) -> sa.MetaData:
    """按 0019 的旧结构手工建表：目录归属知识库，跨库重复允许。"""
    metadata = sa.MetaData()
    kbs = sa.Table(
        "knowledge_bases",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
    )
    catalog = sa.Table(
        "product_catalog",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("product_name", sa.String(200), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
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
        # 与 0019 一致：FK 未命名，由 SQLite 自动处理。
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "knowledge_base_id", "product_name", "model_name",
            name="uq_product_catalog_kb_product_model",
        ),
        sa.Index("ix_product_catalog_knowledge_base_id", "knowledge_base_id"),
    )
    metadata.create_all(engine)
    return metadata


def test_0020_globalizes_catalog_and_dedupes() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    _build_old_schema(engine)
    kb_a, kb_b = uuid4(), uuid4()
    catalog = sa.table(
        "product_catalog",
        sa.column("id", sa.Uuid()),
        sa.column("knowledge_base_id", sa.Uuid()),
        sa.column("product_name", sa.String()),
        sa.column("model_name", sa.String()),
        sa.column("created_at", sa.DateTime()),
        sa.column("updated_at", sa.DateTime()),
    )
    kbs = sa.table("knowledge_bases", sa.column("id", sa.Uuid()))
    with engine.begin() as connection:
        connection.execute(kbs.insert(), [{"id": kb_a}, {"id": kb_b}])
        connection.execute(
            catalog.insert(),
            [
                {
                    "id": uuid4(),
                    "knowledge_base_id": kb_a,
                    "product_name": "体温计",
                    "model_name": "额式体温计",
                    "created_at": datetime.now(),
                    "updated_at": datetime.now(),
                },
                {
                    "id": uuid4(),
                    "knowledge_base_id": kb_b,
                    "product_name": "体温计",
                    "model_name": "额式体温计",
                    "created_at": datetime.now(),
                    "updated_at": datetime.now(),
                },
                {
                    "id": uuid4(),
                    "knowledge_base_id": kb_a,
                    "product_name": "血氧仪",
                    "model_name": "指夹血氧仪",
                    "created_at": datetime.now(),
                    "updated_at": datetime.now(),
                },
            ],
        )

    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            _MIGRATION.upgrade()

        columns = {column["name"] for column in sa.inspect(connection).get_columns("product_catalog")}
        assert "knowledge_base_id" not in columns

        # 跨库重复条目已去重，仅剩两条全局条目。
        kept = connection.execute(
            sa.text("SELECT product_name, model_name FROM product_catalog")
        ).fetchall()
        assert sorted(kept) == [("体温计", "额式体温计"), ("血氧仪", "指夹血氧仪")]

        # 全局唯一约束生效：重复组合被拒，新组合可写入。
        with pytest.raises(IntegrityError):
            connection.execute(
                catalog.insert(),
                [
                    {
                        "id": uuid4(),
                        "product_name": "体温计",
                        "model_name": "额式体温计",
                        "created_at": datetime.now(),
                        "updated_at": datetime.now(),
                    }
                ],
            )
        connection.execute(
            catalog.insert(),
            [
                {
                    "id": uuid4(),
                    "product_name": "体温计",
                    "model_name": "耳式体温计",
                    "created_at": datetime.now(),
                    "updated_at": datetime.now(),
                }
            ],
        )

    # 降级恢复可空库归属列与旧约束。
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            _MIGRATION.downgrade()
        columns = {column["name"] for column in sa.inspect(connection).get_columns("product_catalog")}
        assert "knowledge_base_id" in columns


def test_0020_mysql_branch_statements() -> None:
    """MySQL 分支按 0019 未命名 FK 的惯例名删除外键，重建全局唯一键。"""
    statements: list[str] = []

    def capture(sql, *args, **kwargs):
        if isinstance(sql, str):
            statements.append(sql)
        else:
            statements.append(str(sql.compile(dialect=sa.dialects.mysql.dialect())))

    engine = sa.create_mock_engine("mysql://u:p@localhost/db", capture)
    connection = engine.connect()
    with Operations.context(MigrationContext.configure(connection)):
        _MIGRATION.upgrade()
    joined = "\n".join(statements)
    assert "DROP FOREIGN KEY product_catalog_ibfk_1" in joined
    assert "DROP COLUMN knowledge_base_id" in joined
    assert "uq_product_catalog_product_model" in joined
