"""将知识库收敛为单应用内模块，并以 Chroma 本地存储替代 Milvus。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_embedded_chroma"
down_revision: str | None = "0003_document_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """放宽旧项目字段并建立全局唯一名称；旧表和历史值全部保留。"""
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            "SELECT name FROM knowledge_bases "
            "GROUP BY name HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError(
            "知识库迁移中止：不同旧项目存在同名知识库，请先人工重命名后再迁移。"
        )

    op.drop_constraint(
        "uq_knowledge_bases_project_name", "knowledge_bases", type_="unique"
    )
    op.alter_column(
        "knowledge_bases",
        "project_id",
        existing_type=sa.Uuid(),
        existing_nullable=False,
        nullable=True,
    )
    op.alter_column(
        "knowledge_bases",
        "vector_store_profile_id",
        existing_type=sa.Uuid(),
        existing_nullable=False,
        nullable=True,
    )
    op.create_unique_constraint("uq_knowledge_bases_name", "knowledge_bases", ["name"])


def downgrade() -> None:
    """恢复旧名称约束；历史字段保持可空，避免删除新建知识库的有效记录。"""
    op.drop_constraint("uq_knowledge_bases_name", "knowledge_bases", type_="unique")
    op.create_unique_constraint(
        "uq_knowledge_bases_project_name",
        "knowledge_bases",
        ["project_id", "name"],
    )
