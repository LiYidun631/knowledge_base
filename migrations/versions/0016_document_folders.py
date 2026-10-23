"""知识库内多级文件目录表与文档目录归属列。"""

import sqlalchemy as sa
from alembic import op

revision = "0016_document_folders"
down_revision = "0015_admin_parameters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite 不支持在线加列，用 batch 模式重建；MySQL 保持原生 ALTER 避免整表拷贝。
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("documents") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "folder_id",
                    sa.Uuid(),
                    nullable=True,
                    comment="所在目录；目录删除后文件落回根目录",
                )
            )
        op.create_table(
            "document_folders",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column(
                "knowledge_base_id",
                sa.Uuid(),
                sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "parent_id",
                sa.Uuid(),
                sa.ForeignKey("document_folders.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=False),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=False),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sqlite_autoincrement=True,
            comment="知识库内多级文件目录",
        )
        op.create_index(
            "ix_document_folders_kb_parent",
            "document_folders",
            ["knowledge_base_id", "parent_id"],
        )
        return

    op.create_table(
        "document_folders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "knowledge_base_id",
            sa.Uuid(),
            nullable=False,
            comment="所属知识库 ID",
        ),
        sa.Column("parent_id", sa.Uuid(), nullable=True, comment="上级目录 ID；空表示根目录"),
        sa.Column("name", sa.String(length=160), nullable=False, comment="目录名称"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=False),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["document_folders.id"],
            ondelete="CASCADE",
        ),
        mysql_engine="InnoDB",
        comment="知识库内多级文件目录",
    )
    op.create_index(
        "ix_document_folders_kb_parent",
        "document_folders",
        ["knowledge_base_id", "parent_id"],
    )
    op.add_column(
        "documents",
        sa.Column(
            "folder_id",
            sa.Uuid(),
            nullable=True,
            comment="所在目录；目录删除后文件落回根目录",
        ),
    )
    op.create_foreign_key(
        "fk_documents_folder_id",
        "documents",
        "document_folders",
        ["folder_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("documents") as batch_op:
            batch_op.drop_column("folder_id")
        op.drop_index("ix_document_folders_kb_parent", table_name="document_folders")
        op.drop_table("document_folders")
        return
    op.drop_constraint("fk_documents_folder_id", "documents", type_="foreignkey")
    op.drop_column("documents", "folder_id")
    op.drop_index("ix_document_folders_kb_parent", table_name="document_folders")
    op.drop_table("document_folders")
