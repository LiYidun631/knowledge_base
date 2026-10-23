"""为子块增加 BM25 词频索引，并为历史文档回填数据。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from knowledge_service.application.search.bm25 import term_frequencies

revision: str = "0006_bm25_terms"
down_revision: str | None = "0005_schema_comments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立倒排词项表，并分批为已有子块计算词频。"""
    op.add_column(
        "document_chunks",
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "document_chunk_terms",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("term", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("frequency", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("chunk_id", "term"),
        comment="BM25 子块词项倒排索引",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_document_chunk_terms_term_version",
        "document_chunk_terms",
        ["term", "document_version_id"],
    )
    op.create_index(
        "ix_document_chunk_terms_document_version",
        "document_chunk_terms",
        ["document_id", "document_version_id"],
    )

    bind = op.get_bind()
    chunks = sa.table(
        "document_chunks",
        sa.column("id", sa.Uuid()),
        sa.column("document_id", sa.Uuid()),
        sa.column("document_version_id", sa.Uuid()),
        sa.column("level", sa.String()),
        sa.column("content", sa.Text()),
        sa.column("token_count", sa.Integer()),
    )
    terms = sa.table(
        "document_chunk_terms",
        sa.column("chunk_id", sa.Uuid()),
        sa.column("term", sa.String(length=128)),
        sa.column("document_id", sa.Uuid()),
        sa.column("document_version_id", sa.Uuid()),
        sa.column("frequency", sa.Integer()),
    )
    last_id = None
    while True:
        statement = (
            sa.select(
                chunks.c.id,
                chunks.c.document_id,
                chunks.c.document_version_id,
                chunks.c.content,
            )
            .where(chunks.c.level == "child")
            .order_by(chunks.c.id)
            .limit(500)
        )
        if last_id is not None:
            statement = statement.where(chunks.c.id > last_id)
        batch = bind.execute(statement).mappings().all()
        if not batch:
            break

        updates = []
        term_rows = []
        for row in batch:
            length, frequencies = term_frequencies(row["content"])
            updates.append({"_chunk_id": row["id"], "_token_count": length})
            term_rows.extend(
                {
                    "chunk_id": row["id"],
                    "term": term,
                    "document_id": row["document_id"],
                    "document_version_id": row["document_version_id"],
                    "frequency": frequency,
                }
                for term, frequency in frequencies.items()
            )
        bind.execute(
            sa.update(chunks)
            .where(chunks.c.id == sa.bindparam("_chunk_id"))
            .values(token_count=sa.bindparam("_token_count")),
            updates,
        )
        for start in range(0, len(term_rows), 5000):
            bind.execute(terms.insert(), term_rows[start : start + 5000])
        last_id = batch[-1]["id"]


def downgrade() -> None:
    """移除 BM25 倒排词项及子块词数。"""
    op.drop_index("ix_document_chunk_terms_document_version", table_name="document_chunk_terms")
    op.drop_index("ix_document_chunk_terms_term_version", table_name="document_chunk_terms")
    op.drop_table("document_chunk_terms")
    op.drop_column("document_chunks", "token_count")
