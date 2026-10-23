"""模型档案 kind 约束放宽到 chat，支持生成模型配置。"""

from alembic import op

revision = "0017_chat_model_profiles"
down_revision = "0016_document_folders"
branch_labels = None
depends_on = None

NEW_KIND_CHECK = "kind IN ('embedding', 'rerank', 'chat')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite 通过整表重建修改约束。
        with op.batch_alter_table("model_profiles") as batch_op:
            batch_op.drop_constraint("ck_model_profiles_kind", type_="check")
            batch_op.create_check_constraint("ck_model_profiles_kind", NEW_KIND_CHECK)
        return
    # MySQL 8.0.16 以下解析但忽略 CHECK 约束，约束实际不存在，跳过避免 DROP CHECK 报错。
    if bind.dialect.server_version_info >= (8, 0, 16):
        op.drop_constraint("ck_model_profiles_kind", "model_profiles", type_="check")
        op.create_check_constraint(
            "ck_model_profiles_kind", "model_profiles", NEW_KIND_CHECK
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("model_profiles") as batch_op:
            batch_op.drop_constraint("ck_model_profiles_kind", type_="check")
            batch_op.create_check_constraint(
                "ck_model_profiles_kind", "kind IN ('embedding', 'rerank')"
            )
        return
    if bind.dialect.server_version_info >= (8, 0, 16):
        op.drop_constraint("ck_model_profiles_kind", "model_profiles", type_="check")
        op.create_check_constraint(
            "ck_model_profiles_kind", "model_profiles", "kind IN ('embedding', 'rerank')"
        )
