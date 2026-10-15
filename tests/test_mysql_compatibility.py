"""验证 MySQL DDL 形态以及默认模型约束。"""

from datetime import UTC
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, CreateTable

from knowledge_service.infrastructure.persistence.base import Base
from knowledge_service.infrastructure.persistence.locks import ingestion_lock_name
from knowledge_service.infrastructure.persistence.models import (
    DocumentVersion,
    ModelProfile,
)


def test_mysql_model_profile_ddl_uses_json_and_generated_default_key():
    """MySQL 表结构使用 JSON、CHAR UUID 和生成列唯一键。"""
    ddl = str(CreateTable(ModelProfile.__table__).compile(dialect=mysql.dialect()))
    unique_index = next(index for index in ModelProfile.__table__.indexes if index.unique)
    index_ddl = str(CreateIndex(unique_index).compile(dialect=mysql.dialect()))

    assert "JSON" in ddl
    assert "JSONB" not in ddl
    assert "CHAR(32)" in ddl
    assert "GENERATED ALWAYS AS" in ddl
    assert "ENGINE=InnoDB" in ddl
    assert "UNIQUE INDEX" in index_ddl


def test_document_version_has_generated_unique_current_key():
    """数据库约束保证每个逻辑文档至多有一个当前版本。"""
    ddl = str(CreateTable(DocumentVersion.__table__).compile(dialect=mysql.dialect()))
    unique_index = next(
        index
        for index in DocumentVersion.__table__.indexes
        if index.name == "uq_document_versions_one_current"
    )
    index_ddl = str(CreateIndex(unique_index).compile(dialect=mysql.dialect()))

    assert "CASE WHEN is_current THEN document_id ELSE NULL END" in ddl
    assert "ENGINE=InnoDB" in ddl
    assert "UNIQUE INDEX" in index_ddl


def test_all_persistent_tables_use_innodb():
    """所有 MySQL 业务表都使用支持事务和外键的 InnoDB。"""
    assert all(
        table.dialect_options["mysql"]["engine"] == "InnoDB"
        for table in Base.metadata.tables.values()
    )


def test_default_model_is_unique_per_kind(tmp_path):
    """数据库约束阻止同类模型同时设置两个默认项。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add(
                ModelProfile(
                    id=uuid4(),
                    name="embedding-one",
                    kind="embedding",
                    provider="test",
                    model_name="model-one",
                    dimensions=3,
                    settings={},
                    is_default=True,
                )
            )
            session.commit()
            session.add(
                ModelProfile(
                    id=uuid4(),
                    name="embedding-two",
                    kind="embedding",
                    provider="test",
                    model_name="model-two",
                    dimensions=3,
                    settings={},
                    is_default=True,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        engine.dispose()


def test_ingestion_lock_name_fits_mysql_limit():
    """任务锁名稳定且不超过 MySQL 命名锁的 64 字节限制。"""
    job_id = uuid4()

    lock_name = ingestion_lock_name(job_id)

    assert lock_name == f"ingestion:{job_id.hex}"
    assert len(lock_name.encode("utf-8")) <= 64


def test_database_datetime_round_trip_is_utc(tmp_path):
    """无时区数据库字段读回后仍带明确的 UTC 时区。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'timestamps.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add(
                ModelProfile(
                    name="测试模型",
                    kind="embedding",
                    provider="test",
                    model_name="test-model",
                    dimensions=2,
                )
            )
            session.commit()
        with Session(engine) as session:
            profile = session.scalar(select(ModelProfile))
            assert profile is not None
            assert profile.created_at.tzinfo == UTC
    finally:
        engine.dispose()
