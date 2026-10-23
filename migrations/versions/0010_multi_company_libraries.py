"""新增公司、公司级大库和子库归属字段。"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0010_multi_company_libraries"
down_revision: str | None = "0009_qa_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_innodb_table(name, *columns, **options) -> None:
    """统一使用 InnoDB，确保公司归属外键和事务约束生效。"""
    options.setdefault("mysql_engine", "InnoDB")
    op.create_table(name, *columns, **options)


def upgrade() -> None:
    """创建公司级向量大库，并把历史知识库归入兼容公司。"""
    _create_innodb_table(
        "companies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_companies_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_companies_code"),
        sa.UniqueConstraint("name", name="uq_companies_name"),
        comment="公司租户主表",
    )
    op.create_index("ix_companies_status", "companies", ["status"], unique=False)

    _create_innodb_table(
        "vector_libraries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("embedding_model_id", sa.Uuid(), nullable=False),
        sa.Column("rerank_model_id", sa.Uuid(), nullable=True),
        sa.Column(
            "models_locked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_vector_libraries_status"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["embedding_model_id"], ["model_profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["rerank_model_id"], ["model_profiles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", name="uq_vector_libraries_company_id"),
        comment="公司级向量大库",
    )
    op.create_index("ix_vector_libraries_status", "vector_libraries", ["status"], unique=False)

    op.add_column("knowledge_bases", sa.Column("company_id", sa.Uuid(), nullable=True))
    op.add_column("knowledge_bases", sa.Column("vector_library_id", sa.Uuid(), nullable=True))
    op.add_column("documents", sa.Column("company_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_knowledge_bases_company_id", "knowledge_bases", ["company_id"], unique=False
    )
    op.create_index(
        "ix_knowledge_bases_vector_library_id",
        "knowledge_bases",
        ["vector_library_id"],
        unique=False,
    )
    op.create_index("ix_documents_company_id", "documents", ["company_id"], unique=False)
    # 公共服务按公司隔离子库名称；历史版本的全局唯一约束需要调整。
    op.drop_constraint("uq_knowledge_bases_name", "knowledge_bases", type_="unique")
    op.create_unique_constraint(
        "uq_knowledge_bases_company_name",
        "knowledge_bases",
        ["company_id", "name"],
    )
    op.create_foreign_key(
        "fk_knowledge_bases_company_id",
        "knowledge_bases",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_knowledge_bases_vector_library_id",
        "knowledge_bases",
        "vector_libraries",
        ["vector_library_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_documents_company_id",
        "documents",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    connection = op.get_bind()
    legacy_company_id = uuid4()
    legacy_library_id = uuid4()
    model_row = connection.execute(
        sa.text(
            "SELECT embedding_model_id, rerank_model_id "
            "FROM knowledge_bases ORDER BY created_at LIMIT 1"
        )
    ).first()
    if model_row is None:
        model_row = connection.execute(
            sa.text(
                "SELECT id AS embedding_model_id, NULL AS rerank_model_id "
                "FROM model_profiles WHERE kind = 'embedding' ORDER BY created_at LIMIT 1"
            )
        ).first()
    if model_row is not None:
        connection.execute(
            sa.text(
                "INSERT INTO companies "
                "(id, code, name, status) VALUES (:id, :code, :name, 'active')"
            ),
            {
                # MySQL 的 UUID 列是 CHAR(32)，原始 SQL 使用无连字符形式。
                "id": legacy_company_id.hex,
                "code": "legacy-company",
                "name": "历史兼容公司",
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO vector_libraries "
                "(id, company_id, name, embedding_model_id, rerank_model_id, status) "
                "VALUES (:id, :company_id, :name, :embedding_model_id, :rerank_model_id, 'active')"
            ),
            {
                "id": legacy_library_id.hex,
                "company_id": legacy_company_id.hex,
                "name": "历史兼容大库",
                "embedding_model_id": model_row.embedding_model_id,
                "rerank_model_id": model_row.rerank_model_id,
            },
        )
        connection.execute(
            sa.text(
                "UPDATE knowledge_bases SET company_id = :company_id, "
                "vector_library_id = :library_id"
            ),
            {
                "company_id": legacy_company_id.hex,
                "library_id": legacy_library_id.hex,
            },
        )
        connection.execute(
            sa.text(
                "UPDATE documents d JOIN knowledge_bases k "
                "ON k.id = d.knowledge_base_id "
                "SET d.company_id = k.company_id"
            )
        )


def downgrade() -> None:
    """移除公司归属字段和公司级大库，保留原知识库数据。"""
    op.drop_constraint("fk_documents_company_id", "documents", type_="foreignkey")
    op.drop_constraint(
        "fk_knowledge_bases_vector_library_id", "knowledge_bases", type_="foreignkey"
    )
    op.drop_constraint("fk_knowledge_bases_company_id", "knowledge_bases", type_="foreignkey")
    op.drop_index("ix_documents_company_id", table_name="documents")
    op.drop_index("ix_knowledge_bases_vector_library_id", table_name="knowledge_bases")
    op.drop_index("ix_knowledge_bases_company_id", table_name="knowledge_bases")
    op.drop_constraint(
        "uq_knowledge_bases_company_name", "knowledge_bases", type_="unique"
    )
    op.create_unique_constraint("uq_knowledge_bases_name", "knowledge_bases", ["name"])
    op.drop_column("documents", "company_id")
    op.drop_column("knowledge_bases", "vector_library_id")
    op.drop_column("knowledge_bases", "company_id")
    op.drop_index("ix_vector_libraries_status", table_name="vector_libraries")
    op.drop_table("vector_libraries")
    op.drop_index("ix_companies_status", table_name="companies")
    op.drop_table("companies")
