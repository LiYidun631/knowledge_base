"""将子库名称唯一约束调整为公司范围内唯一。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_company_name_scope"
down_revision: str | None = "0011_company_scope_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _unique_names(connection) -> set[str]:
    """读取当前约束名称，兼容 0010 已提前完成调整的全新数据库。"""
    return {
        str(item["name"])
        for item in sa.inspect(connection).get_unique_constraints("knowledge_bases")
        if item.get("name")
    }


def upgrade() -> None:
    """允许不同公司创建同名子库。"""
    connection = op.get_bind()
    names = _unique_names(connection)
    if "uq_knowledge_bases_name" in names:
        op.drop_constraint("uq_knowledge_bases_name", "knowledge_bases", type_="unique")
    if "uq_knowledge_bases_company_name" not in names:
        op.create_unique_constraint(
            "uq_knowledge_bases_company_name",
            "knowledge_bases",
            ["company_id", "name"],
        )


def downgrade() -> None:
    """恢复历史全局唯一约束；存在跨公司重名时由数据库拒绝回滚。"""
    connection = op.get_bind()
    names = _unique_names(connection)
    if "uq_knowledge_bases_company_name" in names:
        op.drop_constraint(
            "uq_knowledge_bases_company_name", "knowledge_bases", type_="unique"
        )
    if "uq_knowledge_bases_name" not in names:
        op.create_unique_constraint("uq_knowledge_bases_name", "knowledge_bases", ["name"])
