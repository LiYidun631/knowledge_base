"""管理页参数：模型档案密钥列与子库分块参数列。"""

import sqlalchemy as sa
from alembic import op

revision = "0015_admin_parameters"
down_revision = "0014_company_credentials"
branch_labels = None
depends_on = None


def _chunk_columns() -> list[sa.Column]:
    # 分块参数可空表示跟随全局默认；只在入库新文档时生效，历史分块不受影响。
    return [
        sa.Column(
            "chunk_parent_chars",
            sa.Integer(),
            nullable=True,
            comment="父块字符上限；空表示使用全局默认",
        ),
        sa.Column(
            "chunk_child_chars",
            sa.Integer(),
            nullable=True,
            comment="子块字符上限；空表示使用全局默认",
        ),
        sa.Column(
            "chunk_child_overlap_chars",
            sa.Integer(),
            nullable=True,
            comment="子块重叠字符数；空表示使用全局默认",
        ),
    ]


def _chunk_constraints(batch_op) -> None:
    batch_op.create_check_constraint(
        "ck_knowledge_bases_chunk_overlap_lt_child",
        "chunk_child_chars IS NULL OR chunk_child_overlap_chars IS NULL "
        "OR chunk_child_overlap_chars < chunk_child_chars",
    )
    batch_op.create_check_constraint(
        "ck_knowledge_bases_chunk_parent_gte_child",
        "chunk_parent_chars IS NULL OR chunk_child_chars IS NULL "
        "OR chunk_parent_chars >= chunk_child_chars",
    )


def upgrade() -> None:
    # SQLite 不支持在线加列，用 batch 模式重建；MySQL 保持原生 ALTER 避免整表拷贝。
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("model_profiles") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "api_key_cipher",
                    sa.Text(),
                    nullable=True,
                    comment="Fernet 加密的模型 API Key；管理接口不回读",
                )
            )
        with op.batch_alter_table("knowledge_bases") as batch_op:
            for column in _chunk_columns():
                batch_op.add_column(column)
            _chunk_constraints(batch_op)
        return
    op.add_column(
        "model_profiles",
        sa.Column(
            "api_key_cipher",
            sa.Text(),
            nullable=True,
            comment="Fernet 加密的模型 API Key；管理接口不回读",
        ),
    )
    for column in _chunk_columns():
        op.add_column("knowledge_bases", column)
    op.create_check_constraint(
        "ck_knowledge_bases_chunk_overlap_lt_child",
        "knowledge_bases",
        "chunk_child_chars IS NULL OR chunk_child_overlap_chars IS NULL "
        "OR chunk_child_overlap_chars < chunk_child_chars",
    )
    op.create_check_constraint(
        "ck_knowledge_bases_chunk_parent_gte_child",
        "knowledge_bases",
        "chunk_parent_chars IS NULL OR chunk_child_chars IS NULL "
        "OR chunk_parent_chars >= chunk_child_chars",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("knowledge_bases") as batch_op:
            batch_op.drop_constraint("ck_knowledge_bases_chunk_parent_gte_child")
            batch_op.drop_constraint("ck_knowledge_bases_chunk_overlap_lt_child")
            batch_op.drop_column("chunk_child_overlap_chars")
            batch_op.drop_column("chunk_child_chars")
            batch_op.drop_column("chunk_parent_chars")
        with op.batch_alter_table("model_profiles") as batch_op:
            batch_op.drop_column("api_key_cipher")
        return
    op.drop_constraint("ck_knowledge_bases_chunk_parent_gte_child", "knowledge_bases")
    op.drop_constraint("ck_knowledge_bases_chunk_overlap_lt_child", "knowledge_bases")
    op.drop_column("knowledge_bases", "chunk_child_overlap_chars")
    op.drop_column("knowledge_bases", "chunk_child_chars")
    op.drop_column("knowledge_bases", "chunk_parent_chars")
    op.drop_column("model_profiles", "api_key_cipher")
