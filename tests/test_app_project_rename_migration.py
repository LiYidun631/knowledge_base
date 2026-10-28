"""0031 迁移：接入项目 test_agent 就地改名为 health_manager_ai。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION = importlib.import_module("migrations.versions.0031_rename_app_project")


def _build_app_projects(engine: sa.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "app_projects",
        metadata,
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_key", sa.String(80), nullable=False, unique=True),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("description", sa.String(1000)),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    metadata.create_all(engine)


def _run(connection: sa.engine.Connection, action: str) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_MIGRATION, action)()


def test_0031_renames_project_key_in_place_and_keeps_id() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    _build_app_projects(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO app_projects (id, project_key, display_name, description) "
                "VALUES ('4b9a5c1e8d3f4f6a9b2e7c1d0a3e5f21', 'test_agent', '健康管家', "
                "'test_agent 健康管家问答项目；经 /search 检索并自行生成回答。')"
            )
        )

    with engine.begin() as connection:
        _run(connection, "upgrade")
        row = connection.execute(
            sa.text("SELECT id, project_key, display_name FROM app_projects")
        ).one()
        # 同一行就地改名，历史轨迹按 project_id 仍归属原项目。
        assert row == ("4b9a5c1e8d3f4f6a9b2e7c1d0a3e5f21", "health_manager_ai", "健康管家")

    # 重复执行应保持幂等。
    with engine.begin() as connection:
        _run(connection, "upgrade")
        count = connection.execute(sa.text("SELECT COUNT(*) FROM app_projects")).scalar_one()
        assert count == 1

    with engine.begin() as connection:
        _run(connection, "downgrade")
        row = connection.execute(
            sa.text("SELECT project_key, display_name FROM app_projects")
        ).one()
        assert row == ("test_agent", "健康管家")
