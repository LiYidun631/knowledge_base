"""验证 BM25 迁移可以在 SQLite 中补建已有子块索引。"""

import importlib
from uuid import uuid4

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, ForeignKey, MetaData, String, Table, Text, Uuid
from sqlalchemy.engine import create_engine


def test_bm25_migration_backfills_existing_child_chunks() -> None:
    """升级迁移后旧子块有 token_count 和词频，父块不生成倒排词项。"""
    metadata = MetaData()
    documents = Table("documents", metadata, Column("id", Uuid(), primary_key=True))
    versions = Table(
        "document_versions",
        metadata,
        Column("id", Uuid(), primary_key=True),
        Column("document_id", Uuid(), ForeignKey("documents.id"), nullable=False),
    )
    chunks = Table(
        "document_chunks",
        metadata,
        Column("id", Uuid(), primary_key=True),
        Column("document_id", Uuid(), ForeignKey("documents.id"), nullable=False),
        Column(
            "document_version_id",
            Uuid(),
            ForeignKey("document_versions.id"),
            nullable=False,
        ),
        Column("level", String(10), nullable=False),
        Column("content", Text(), nullable=False),
    )
    doc_id, version_id, parent_id, child_id = (uuid4() for _ in range(4))
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            metadata.create_all(connection)
            connection.execute(documents.insert(), {"id": doc_id})
            connection.execute(versions.insert(), {"id": version_id, "document_id": doc_id})
            connection.execute(
                chunks.insert(),
                [
                    {
                        "id": parent_id,
                        "document_id": doc_id,
                        "document_version_id": version_id,
                        "level": "parent",
                        "content": "父块文本不直接建倒排索引",
                    },
                    {
                        "id": child_id,
                        "document_id": doc_id,
                        "document_version_id": version_id,
                        "level": "child",
                        "content": "高血压高血压 care",
                    },
                ],
            )
            migration = importlib.import_module("migrations.versions.0006_bm25_terms")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

            token_count = connection.scalar(
                sa.text("SELECT token_count FROM document_chunks WHERE id=:id").bindparams(
                    sa.bindparam("id", type_=Uuid())
                ),
                {"id": child_id},
            )
            indexed_terms = connection.execute(
                sa.text(
                    "SELECT term, frequency FROM document_chunk_terms WHERE chunk_id=:id"
                ).bindparams(sa.bindparam("id", type_=Uuid())),
                {"id": child_id},
            ).all()
            assert token_count == 6
            assert dict(indexed_terms)["高血"] == 2
            assert dict(indexed_terms)["血压"] == 2
            assert dict(indexed_terms)["care"] == 1
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT COUNT(*) FROM document_chunk_terms WHERE chunk_id=:id"
                    ).bindparams(sa.bindparam("id", type_=Uuid())),
                    {"id": parent_id},
                )
                == 0
            )
    finally:
        engine.dispose()
