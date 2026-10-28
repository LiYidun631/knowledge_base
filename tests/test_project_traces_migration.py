"""0027 迁移：项目轨迹表创建、test_agent 预置与降级。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION = importlib.import_module("migrations.versions.0027_project_qa_traces")


def _build_referenced_tables(engine: sa.Engine) -> None:
    metadata = sa.MetaData()
    for name in (
        "companies",
        "knowledge_bases",
        "documents",
        "document_versions",
        "document_chunks",
    ):
        sa.Table(name, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    metadata.create_all(engine)


def test_0027_creates_tables_and_seeds_test_agent() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    _build_referenced_tables(engine)
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            _MIGRATION.upgrade()

        tables = set(sa.inspect(connection).get_table_names())
        assert {"app_projects", "project_qa_traces", "project_qa_sources"} <= tables
        seeded = connection.execute(
            sa.text(
                "SELECT project_key, display_name, retention_days, is_enabled FROM app_projects"
            )
        ).one()
        assert seeded == ("test_agent", "健康管家", 90, True)
        columns = {
            column["name"] for column in sa.inspect(connection).get_columns("project_qa_traces")
        }
        assert {
            "external_request_id",
            "retrieval_attempts",
            "answer",
            "finished_at",
        } <= columns

    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            _MIGRATION.downgrade()
        tables = set(sa.inspect(connection).get_table_names())
        assert not tables & {"app_projects", "project_qa_traces", "project_qa_sources"}
