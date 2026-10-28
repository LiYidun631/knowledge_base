"""接入项目改名：test_agent -> health_manager_ai（就地改名，保留历史轨迹归属）。"""

import sqlalchemy as sa
from alembic import op

revision = "0031_rename_app_project"
down_revision = "0030_retrieval_evaluation"
branch_labels = None
depends_on = None

OLD_KEY = "test_agent"
NEW_KEY = "health_manager_ai"
NEW_DISPLAY_NAME = "健康管家"
NEW_DESCRIPTION = (
    "health_manager_ai（原 test_agent）健康管家问答项目；经 /search 检索并自行生成回答。"
)


def _project_exists(bind: sa.engine.Connection, project_key: str) -> bool:
    """按项目标识查是否已存在，避免重复改名触发唯一约束冲突。"""
    row = bind.execute(
        sa.text("SELECT 1 FROM app_projects WHERE project_key = :key LIMIT 1"),
        {"key": project_key},
    ).first()
    return row is not None


def upgrade() -> None:
    # 轨迹表按 project_id 关联，就地改 project_key 可让历史轨迹继续归在同一个项目下。
    bind = op.get_bind()
    if not _project_exists(bind, OLD_KEY) or _project_exists(bind, NEW_KEY):
        return
    bind.execute(
        sa.text(
            "UPDATE app_projects "
            "SET project_key = :new_key, display_name = :display_name, "
            "description = :description, updated_at = CURRENT_TIMESTAMP "
            "WHERE project_key = :old_key"
        ),
        {
            "new_key": NEW_KEY,
            "display_name": NEW_DISPLAY_NAME,
            "description": NEW_DESCRIPTION,
            "old_key": OLD_KEY,
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _project_exists(bind, NEW_KEY) or _project_exists(bind, OLD_KEY):
        return
    bind.execute(
        sa.text(
            "UPDATE app_projects "
            "SET project_key = :old_key, "
            "description = :description, updated_at = CURRENT_TIMESTAMP "
            "WHERE project_key = :new_key"
        ),
        {
            "old_key": OLD_KEY,
            "new_key": NEW_KEY,
            "description": "test_agent 健康管家问答项目；经 /search 检索并自行生成回答。",
        },
    )
