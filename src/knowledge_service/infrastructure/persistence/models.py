from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from knowledge_service.infrastructure.persistence.base import Base

json_type = JSON()


class UTCDateTime(TypeDecorator[datetime]):
    """跨 MySQL/SQLite 保留 UTC 语义，避免数据库往返后丢失时区。"""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        """使用无时区列保存 UTC 时间值。"""
        return dialect.type_descriptor(DateTime(timezone=False))

    def process_bind_param(self, value: datetime | None, dialect):
        """写入前统一换算到 UTC，MySQL/SQLite 列中保存 UTC 时间值。"""
        if value is None:
            return None
        normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return normalized.replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        """读取无时区数据库值时明确标记为 UTC。"""
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class ModelProfile(TimestampMixin, Base):
    __tablename__ = "model_profiles"
    __table_args__ = (
        CheckConstraint("kind IN ('embedding', 'rerank')", name="ck_model_profiles_kind"),
        UniqueConstraint("kind", "name", name="uq_model_profiles_kind_name"),
        Index("ix_model_profiles_kind_enabled", "kind", "is_enabled"),
        Index("uq_model_profiles_kind_default", "default_kind", unique=True),
        {"mysql_engine": "InnoDB"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    dimensions: Mapped[int | None] = mapped_column(Integer)
    settings: Mapped[dict[str, Any]] = mapped_column(
        json_type,
        default=dict,
        nullable=False,
    )
    secret_ref: Mapped[str | None] = mapped_column(String(200))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # MySQL 与 SQLite 均可用生成列实现每类模型最多一个默认项。
    default_kind: Mapped[str | None] = mapped_column(
        String(20),
        Computed("CASE WHEN is_default THEN kind ELSE NULL END"),
        nullable=True,
    )


class KnowledgeBase(TimestampMixin, Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="ck_knowledge_bases_status"),
        UniqueConstraint("name", name="uq_knowledge_bases_name"),
        {"mysql_engine": "InnoDB"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    embedding_model_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("model_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    rerank_model_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("model_profiles.id", ondelete="RESTRICT"),
    )
    models_locked_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed', 'deleting')",
            name="ck_documents_status",
        ),
        UniqueConstraint(
            "knowledge_base_id", "document_key", name="uq_documents_kb_document_key"
        ),
        Index("ix_documents_knowledge_base_id", "knowledge_base_id"),
        Index("ix_documents_status", "status"),
        {"mysql_engine": "InnoDB"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 调用方重复上传时用此稳定键归并为同一逻辑文档；文件名仅作展示，不作为唯一标识。
    document_key: Mapped[str] = mapped_column(String(500), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    latest_version_number: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        json_type,
        default=dict,
        nullable=False,
    )


class DocumentVersion(TimestampMixin, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_versions_number"),
        CheckConstraint("version_number > 0", name="ck_document_versions_positive_number"),
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed', 'deleting')",
            name="ck_document_versions_status",
        ),
        Index("ix_document_versions_document_id", "document_id"),
        Index("ix_document_versions_status", "status"),
        Index("uq_document_versions_one_current", "current_document_id", unique=True),
        {"mysql_engine": "InnoDB"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(160))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # MySQL/SQLite 生成唯一键，保证每个逻辑文档至多有一个当前版本。
    current_document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        Computed("CASE WHEN is_current THEN document_id ELSE NULL END"),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        json_type,
        default=dict,
        nullable=False,
    )


class IngestionJob(TimestampMixin, Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_ingestion_jobs_status",
        ),
        Index("ix_ingestion_jobs_status_created_at", "status", "created_at"),
        {"mysql_engine": "InnoDB"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    stage: Mapped[str | None] = mapped_column(String(40))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_details: Mapped[dict[str, Any]] = mapped_column(
        json_type,
        default=dict,
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class DocumentChunk(TimestampMixin, Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        CheckConstraint("level IN ('parent', 'child')", name="ck_document_chunks_level"),
        UniqueConstraint(
            "document_version_id", "ordinal", name="uq_document_chunks_version_ordinal"
        ),
        Index("ix_document_chunks_document_id", "document_id"),
        Index("ix_document_chunks_version_id", "document_version_id"),
        Index("ix_document_chunks_parent_id", "parent_chunk_id"),
        {"mysql_engine": "InnoDB"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_chunk_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_chunks.id", ondelete="CASCADE"),
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        json_type,
        default=dict,
        nullable=False,
    )


class OutboxEvent(TimestampMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'published', 'failed')",
            name="ck_outbox_events_status",
        ),
        Index("ix_outbox_events_status_created_at", "status", "created_at"),
        {"mysql_engine": "InnoDB"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        json_type,
        default=dict,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
