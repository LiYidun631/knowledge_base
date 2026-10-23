"""产品目录全局化：去掉知识库归属，产品+型号全局唯一。"""

import sqlalchemy as sa
from alembic import op

revision = "0020_product_catalog_global"
down_revision = "0019_product_catalog"
branch_labels = None
depends_on = None

# MySQL 的 FK 由 0019 未命名创建，按 InnoDB 惯例自动命名为 {表名}_ibfk_1。
_MYSQL_FK_NAME = "product_catalog_ibfk_1"

_DEDUP_SQL = (
    "DELETE FROM product_catalog WHERE id NOT IN ("
    "SELECT keep_id FROM ("
    "SELECT MIN(id) AS keep_id FROM product_catalog "
    "GROUP BY product_name, model_name"
    ") AS keep_ids)"
)


def upgrade() -> None:
    # 同一（产品名称, 型号）跨库重复时只保留 id 最小的一条，保证新唯一约束可建立。
    op.execute(_DEDUP_SQL)
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(f"ALTER TABLE product_catalog DROP FOREIGN KEY {_MYSQL_FK_NAME}")
        op.execute(
            "ALTER TABLE product_catalog DROP INDEX ix_product_catalog_knowledge_base_id"
        )
        op.execute(
            "ALTER TABLE product_catalog DROP KEY uq_product_catalog_kb_product_model"
        )
        op.execute("ALTER TABLE product_catalog DROP COLUMN knowledge_base_id")
        op.execute(
            "ALTER TABLE product_catalog ADD UNIQUE KEY uq_product_catalog_product_model "
            "(product_name, model_name)"
        )
        op.execute("ALTER TABLE product_catalog COMMENT '全局产品名称与型号目录'")
        return

    with op.batch_alter_table(
        "product_catalog",
        naming_convention={
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        },
    ) as batch:
        batch.drop_constraint(
            "fk_product_catalog_knowledge_base_id_knowledge_bases", type_="foreignkey"
        )
        batch.drop_constraint("uq_product_catalog_kb_product_model", type_="unique")
        batch.drop_index("ix_product_catalog_knowledge_base_id")
        batch.drop_column("knowledge_base_id")
        batch.create_unique_constraint(
            "uq_product_catalog_product_model", ["product_name", "model_name"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute("ALTER TABLE product_catalog DROP KEY uq_product_catalog_product_model")
        op.execute(
            "ALTER TABLE product_catalog ADD COLUMN knowledge_base_id CHAR(32) NULL "
            "COMMENT '所属知识库'"
        )
        op.execute(
            "ALTER TABLE product_catalog ADD UNIQUE KEY uq_product_catalog_kb_product_model "
            "(knowledge_base_id, product_name, model_name)"
        )
        op.execute(
            "CREATE INDEX ix_product_catalog_knowledge_base_id "
            "ON product_catalog (knowledge_base_id)"
        )
        op.execute(
            f"ALTER TABLE product_catalog ADD CONSTRAINT {_MYSQL_FK_NAME} "
            "FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases (id) "
            "ON DELETE CASCADE"
        )
        op.execute("ALTER TABLE product_catalog COMMENT '知识库产品名称与型号目录'")
        return

    # 库归属信息已丢失，降级只能恢复可空列与旧约束。
    with op.batch_alter_table(
        "product_catalog",
        naming_convention={
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        },
    ) as batch:
        batch.drop_constraint("uq_product_catalog_product_model", type_="unique")
        batch.add_column(sa.Column("knowledge_base_id", sa.Uuid(), nullable=True))
        batch.create_unique_constraint(
            "uq_product_catalog_kb_product_model",
            ["knowledge_base_id", "product_name", "model_name"],
        )
        batch.create_index(
            "ix_product_catalog_knowledge_base_id", ["knowledge_base_id"]
        )
        batch.create_foreign_key(
            "fk_product_catalog_knowledge_base_id_knowledge_bases",
            "knowledge_bases",
            ["knowledge_base_id"],
            ["id"],
            ondelete="CASCADE",
        )
