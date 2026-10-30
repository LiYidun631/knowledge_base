from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
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
        CheckConstraint(
            "kind IN ('embedding', 'rerank', 'chat', 'ocr')", name="ck_model_profiles_kind"
        ),
        UniqueConstraint("kind", "name", name="uq_model_profiles_kind_name"),
        Index("ix_model_profiles_kind_enabled", "kind", "is_enabled"),
        Index("uq_model_profiles_kind_default", "default_kind", unique=True),
        {"mysql_engine": "InnoDB", "comment": "Embedding、Rerank、Chat 与 OCR 模型档案"},
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
    # 管理页写入的 API Key 只存 Fernet 密文；接口不回读，明文仅在模型调用时解密。
    api_key_cipher: Mapped[str | None] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # MySQL 与 SQLite 均可用生成列实现每类模型最多一个默认项。
    default_kind: Mapped[str | None] = mapped_column(
        String(20),
        Computed("CASE WHEN is_default THEN kind ELSE NULL END"),
        nullable=True,
    )


class ModelPrice(TimestampMixin, Base):
    """生成模型计费单价（人民币/百万 token）；按模型名匹配问答记录，环境兜底模型也能定价。"""

    __tablename__ = "model_prices"
    __table_args__ = (
        UniqueConstraint("model_name", name="uq_model_prices_model_name"),
        {"mysql_engine": "InnoDB", "comment": "生成模型 token 单价，用于问答成本核算"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(80))
    # 单价允许 6 位小数，避免低价模型（如 0.15 元/百万 token）被截断。
    input_price_per_million: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    output_price_per_million: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )


class Company(TimestampMixin, Base):
    """公司租户；公共知识库服务以公司作为最外层数据隔离边界。"""

    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="ck_companies_status"),
        UniqueConstraint("code", name="uq_companies_code"),
        UniqueConstraint("name", name="uq_companies_name"),
        Index("ix_companies_status", "status"),
        {"mysql_engine": "InnoDB", "comment": "公司租户主表"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class CompanyCredential(TimestampMixin, Base):
    """公司访问凭据；同一公司可签发多枚，按项目命名并独立停用或重置。"""

    __tablename__ = "company_credentials"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_company_credentials_token_hash"),
        UniqueConstraint("company_id", "name", name="uq_company_credentials_company_name"),
        Index("ix_company_credentials_company_id", "company_id"),
        {"mysql_engine": "InnoDB", "comment": "公司访问凭据，仅保存哈希；每公司可多枚"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    # 项目/用途标识，同一公司内唯一；权限仍以公司为边界。
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )


class AdminCredential(TimestampMixin, Base):
    """管理员访问凭据，仅保存哈希；.env 引导 Token 始终有效作为兜底。"""

    __tablename__ = "admin_credentials"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_admin_credentials_token_hash"),
        Index("ix_admin_credentials_username", "username", unique=True),
        {"mysql_engine": "InnoDB", "comment": "管理员访问凭据，仅保存哈希"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 账号密码登录用；未设置账号或密码的管理员只能用长期 Token 进入。
    username: Mapped[str | None] = mapped_column(
        String(60), comment="登录账号，唯一；NULL 表示未开通账号密码登录"
    )
    password_hash: Mapped[str | None] = mapped_column(
        String(255), comment="PBKDF2 密码哈希，不保存明文"
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    # 角色三档：admin 全权、operator 运营、viewer 只读；环境引导凭据固定按 admin。
    role: Mapped[str] = mapped_column(
        String(20), default="admin", server_default=text("'admin'"), nullable=False
    )


class AdminSession(TimestampMixin, Base):
    """账号密码登录换发的短期会话凭据；只存哈希，停用凭据或改密即失效。"""

    __tablename__ = "admin_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_admin_sessions_token_hash"),
        Index("ix_admin_sessions_credential_id", "credential_id"),
        Index("ix_admin_sessions_expires_at", "expires_at"),
        {"mysql_engine": "InnoDB", "comment": "管理端登录会话"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    credential_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_credentials.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    # 记录来源 IP 便于排查异常登录，不参与鉴权判定。
    client_ip: Mapped[str | None] = mapped_column(String(64), comment="登录时的客户端 IP")


class AuditLog(TimestampMixin, Base):
    """管理端写操作审计；只记录操作事实，不保存请求体，避免密钥等敏感内容落库。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_actor_name", "actor_name"),
        {"mysql_engine": "InnoDB", "comment": "管理端写操作审计日志"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    # 操作者信息在写入时快照，凭据后续改名、停用或删除都不影响历史日志。
    actor_name: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(20), nullable=False)
    credential_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(64))


class AdminPromptOverride(TimestampMixin, Base):
    """管理端提示词覆写；存在时优先于提示词文件与内置默认生效。"""

    __tablename__ = "admin_prompt_overrides"
    __table_args__ = ({"mysql_engine": "InnoDB", "comment": "管理端提示词覆写"},)

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)


class VectorLibrary(TimestampMixin, Base):
    """公司级大库；同一公司下的子库共享此处锁定的向量模型。"""

    __tablename__ = "vector_libraries"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="ck_vector_libraries_status"),
        UniqueConstraint("company_id", name="uq_vector_libraries_company_id"),
        Index("ix_vector_libraries_status", "status"),
        {"mysql_engine": "InnoDB", "comment": "公司级向量大库"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    embedding_model_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    rerank_model_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_profiles.id", ondelete="RESTRICT")
    )
    models_locked_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class KnowledgeBase(TimestampMixin, Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="ck_knowledge_bases_status"),
        # 子库名称只需在同一公司内唯一，不同公司允许使用相同名称。
        UniqueConstraint("company_id", "name", name="uq_knowledge_bases_company_name"),
        Index("ix_knowledge_bases_company_id", "company_id"),
        Index("ix_knowledge_bases_vector_library_id", "vector_library_id"),
        # 分块参数约束允许任意一列为空（跟随全局默认），仅在两者同时填写时校验。
        CheckConstraint(
            "chunk_child_chars IS NULL OR chunk_child_overlap_chars IS NULL "
            "OR chunk_child_overlap_chars < chunk_child_chars",
            name="ck_knowledge_bases_chunk_overlap_lt_child",
        ),
        CheckConstraint(
            "chunk_parent_chars IS NULL OR chunk_child_chars IS NULL "
            "OR chunk_parent_chars >= chunk_child_chars",
            name="ck_knowledge_bases_chunk_parent_gte_child",
        ),
        {"mysql_engine": "InnoDB", "comment": "知识库主表，记录模型绑定和生命周期"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    # 新架构字段；历史知识库迁移完成前允许为空，新的公共接口会强制校验归属。
    company_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT")
    )
    vector_library_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("vector_libraries.id", ondelete="RESTRICT")
    )
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
    # 分块参数可空表示跟随全局默认；只影响新入库文档，历史分块保持原样。
    chunk_parent_chars: Mapped[int | None] = mapped_column(Integer)
    chunk_child_chars: Mapped[int | None] = mapped_column(Integer)
    chunk_child_overlap_chars: Mapped[int | None] = mapped_column(Integer)


class DocumentFolder(TimestampMixin, Base):
    """知识库内多级目录，仅作文件组织，不参与检索或权限。"""

    __tablename__ = "document_folders"
    __table_args__ = (
        Index("ix_document_folders_kb_parent", "knowledge_base_id", "parent_id"),
        {"mysql_engine": "InnoDB", "comment": "知识库内多级文件目录"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_folders.id", ondelete="CASCADE"),
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    # 同级重名由应用层校验：MySQL 唯一索引对 NULL parent_id 视为互不相同，
    # 无法用 (knowledge_base_id, parent_id, name) 同时约束根目录和子目录。


class ProductCatalog(TimestampMixin, Base):
    """全局产品目录；上传标签选择的唯一来源，与知识库无关。"""

    __tablename__ = "product_catalog"
    __table_args__ = (
        UniqueConstraint(
            "product_name",
            "model_name",
            name="uq_product_catalog_product_model",
        ),
        {"mysql_engine": "InnoDB", "comment": "全局产品名称与型号目录"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed', 'deleting')",
            name="ck_documents_status",
        ),
        UniqueConstraint("knowledge_base_id", "document_key", name="uq_documents_kb_document_key"),
        Index("ix_documents_knowledge_base_id", "knowledge_base_id"),
        Index("ix_documents_company_id", "company_id"),
        Index("ix_documents_status", "status"),
        {"mysql_engine": "InnoDB", "comment": "逻辑文档主表，记录文档标识和最新状态"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    # 公司 ID 由服务端根据子库归属填充，禁止由文件内容或 metadata 覆盖。
    company_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT")
    )
    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 所在目录；目录删除时文件落回根目录，目录本身仅作组织用途。
    folder_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document_folders.id", ondelete="SET NULL")
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
        {"mysql_engine": "InnoDB", "comment": "文档不可变版本和原始文件信息"},
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
        {"mysql_engine": "InnoDB", "comment": "离线入库任务及处理阶段"},
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
        {"mysql_engine": "InnoDB", "comment": "文档父块和子块文本"},
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
    # 子块的分词总数用于 BM25 文档长度归一化；旧数据由迁移补齐。
    token_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    page_number: Mapped[int | None] = mapped_column(Integer)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        json_type,
        default=dict,
        nullable=False,
    )


class DocumentChunkTerm(Base):
    """BM25 倒排词项；每行表示一个子块中某词项出现的次数。"""

    __tablename__ = "document_chunk_terms"
    __table_args__ = (
        Index("ix_document_chunk_terms_term_version", "term", "document_version_id"),
        Index("ix_document_chunk_terms_document_version", "document_id", "document_version_id"),
        {"mysql_engine": "InnoDB", "comment": "BM25 子块词项倒排索引"},
    )

    chunk_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_chunks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    term: Mapped[str] = mapped_column(String(128), primary_key=True)
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
    frequency: Mapped[int] = mapped_column(Integer, nullable=False)


class DocumentAsset(TimestampMixin, Base):
    """文档中的图片资产及其在原文中的稳定位置标识。"""

    __tablename__ = "document_assets"
    __table_args__ = (
        Index("ix_document_assets_version_id", "document_version_id"),
        Index("ix_document_assets_chunk_id", "chunk_id"),
        {"mysql_engine": "InnoDB", "comment": "文档图片及版面资产"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    document_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document_chunks.id", ondelete="SET NULL")
    )
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    media_type: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # 解析器生成的占位符，用于把图片确定性还原到原文位置。
    marker: Mapped[str | None] = mapped_column(String(300))
    location: Mapped[dict[str, Any] | None] = mapped_column(
        json_type, comment="图片原始位置：块序号、页码、单元格、坐标与阅读顺序"
    )
    page_number: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[list[float] | None] = mapped_column(json_type)


class FaqEntry(TimestampMixin, Base):
    """库级或文档级的问答对；问题向量用于问答前置匹配直出。"""

    __tablename__ = "faq_entries"
    __table_args__ = (
        Index("ix_faq_entries_kb_id", "knowledge_base_id"),
        Index("ix_faq_entries_document_id", "document_id"),
        Index("ix_faq_entries_version_id", "document_version_id"),
        {"mysql_engine": "InnoDB", "comment": "库级与文档级 FAQ"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    document_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    question_embedding: Mapped[list[float]] = mapped_column(json_type, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")


class QASession(TimestampMixin, Base):
    """在线问答会话；调用方可通过 UUID 将多轮问题归到同一会话。"""

    __tablename__ = "qa_sessions"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'closed')", name="ck_qa_sessions_status"),
        Index("ix_qa_sessions_knowledge_base_created", "knowledge_base_id", "created_at"),
        Index("ix_qa_sessions_company_created", "company_id", "created_at"),
        {"mysql_engine": "InnoDB", "comment": "在线问答会话"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    # 公司归属用于公共服务的租户审计和查询隔离。
    company_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT")
    )
    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)


class QARecord(TimestampMixin, Base):
    """在线问答主记录；保存问题、答案、状态、模型和耗时摘要。"""

    __tablename__ = "qa_records"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'no_results', 'failed')",
            name="ck_qa_records_status",
        ),
        CheckConstraint(
            "retrieval_mode IN ('hybrid', 'bm25', 'vector')",
            name="ck_qa_records_retrieval_mode",
        ),
        CheckConstraint(
            "review_status IN ('none', 'pending', 'resolved')",
            name="ck_qa_records_review_status",
        ),
        CheckConstraint(
            "feedback IS NULL OR feedback IN ('up', 'down')",
            name="ck_qa_records_feedback",
        ),
        UniqueConstraint("request_id", name="uq_qa_records_request_id"),
        Index("ix_qa_records_knowledge_base_created", "knowledge_base_id", "created_at"),
        Index("ix_qa_records_company_created", "company_id", "created_at"),
        Index("ix_qa_records_session_created", "session_id", "created_at"),
        Index("ix_qa_records_status_created", "status", "created_at"),
        Index("ix_qa_records_review_created", "review_status", "created_at"),
        Index("ix_qa_records_feedback_created", "feedback", "created_at"),
        {"mysql_engine": "InnoDB", "comment": "在线问答主记录和结果审计"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT")
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("qa_sessions.id", ondelete="SET NULL")
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    retrieval_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    include_context: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    review_status: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    faq_matched: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 用户反馈：up 有帮助、down 无帮助；为空表示未反馈。点踩会自动进入待补资料。
    feedback: Mapped[str | None] = mapped_column(String(10))
    feedback_reason: Mapped[str | None] = mapped_column(String(500))
    feedback_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chat_provider: Mapped[str | None] = mapped_column(String(80))
    chat_model_name: Mapped[str | None] = mapped_column(String(200))
    retrieval_duration_ms: Mapped[int | None] = mapped_column(Integer)
    generation_duration_ms: Mapped[int | None] = mapped_column(Integer)
    total_duration_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)


class QASource(TimestampMixin, Base):
    """问答引用来源快照；文档删除后仍保留回答当时的来源文本。"""

    __tablename__ = "qa_sources"
    __table_args__ = (
        UniqueConstraint("qa_record_id", "rank", name="uq_qa_sources_record_rank"),
        Index("ix_qa_sources_record", "qa_record_id"),
        Index("ix_qa_sources_document", "document_id"),
        Index("ix_qa_sources_company_created", "company_id", "created_at"),
        {"mysql_engine": "InnoDB", "comment": "在线问答引用来源和文本快照"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT")
    )
    qa_record_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("qa_records.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    document_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="SET NULL")
    )
    chunk_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document_chunks.id", ondelete="SET NULL")
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(500))
    content_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    context_snapshot: Mapped[str | None] = mapped_column(Text)
    cosine_distance: Mapped[float | None] = mapped_column(Float)
    bm25_score: Mapped[float | None] = mapped_column(Float)
    rrf_score: Mapped[float | None] = mapped_column(Float)
    rerank_score: Mapped[float | None] = mapped_column(Float)
    asset_ids: Mapped[list[str]] = mapped_column(json_type, default=list, nullable=False)


class QAEvent(TimestampMixin, Base):
    """在线问答阶段日志；记录检索、生成和失败阶段的状态与耗时。"""

    __tablename__ = "qa_events"
    __table_args__ = (
        CheckConstraint("status IN ('started', 'success', 'failed')", name="ck_qa_events_status"),
        Index("ix_qa_events_record_created", "qa_record_id", "created_at"),
        Index("ix_qa_events_request_created", "request_id", "created_at"),
        Index("ix_qa_events_company_created", "company_id", "created_at"),
        {"mysql_engine": "InnoDB", "comment": "在线问答阶段日志"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT")
    )
    qa_record_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("qa_records.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    message: Mapped[str | None] = mapped_column(String(1000))
    details: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)


class AppProject(TimestampMixin, Base):
    """接入方项目登记；项目轨迹按项目隔离，并按各自保留天数定期清理。"""

    __tablename__ = "app_projects"
    __table_args__ = (
        UniqueConstraint("project_key", name="uq_app_projects_project_key"),
        {"mysql_engine": "InnoDB", "comment": "接入项目登记与轨迹保留策略"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    # 调用方通过 X-Project-Key 传入的稳定标识，创建后不建议修改。
    project_key: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    retention_days: Mapped[int] = mapped_column(
        Integer, default=90, server_default=text("90"), nullable=False
    )
    # 惰性清理标记：写入轨迹时每天最多触发一次过期数据删除。
    last_purged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ProjectQaTrace(TimestampMixin, Base):
    """外部项目的一次问答请求轨迹；检索阶段先落库，项目回答完成后回填答案。"""

    __tablename__ = "project_qa_traces"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'no_results', 'failed', 'clarification')",
            name="ck_project_qa_traces_status",
        ),
        CheckConstraint(
            "feedback IS NULL OR feedback IN ('up', 'down')",
            name="ck_project_qa_traces_feedback",
        ),
        CheckConstraint(
            "review_status IN ('none', 'pending', 'resolved')",
            name="ck_project_qa_traces_review_status",
        ),
        UniqueConstraint(
            "company_id",
            "project_id",
            "external_request_id",
            name="uq_project_qa_traces_request",
        ),
        Index("ix_project_qa_traces_company_created", "company_id", "created_at"),
        Index("ix_project_qa_traces_project_created", "project_id", "created_at"),
        Index("ix_project_qa_traces_kb_created", "knowledge_base_id", "created_at"),
        Index("ix_project_qa_traces_status_created", "status", "created_at"),
        Index("ix_project_qa_traces_session_created", "session_ref", "created_at"),
        Index("ix_project_qa_traces_feedback_created", "feedback", "created_at"),
        Index("ix_project_qa_traces_review_created", "review_status", "created_at"),
        {"mysql_engine": "InnoDB", "comment": "外部项目问答轨迹与检索摘要"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_projects.id", ondelete="RESTRICT"), nullable=False
    )
    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 调用方自己的请求 ID，用于检索记录与完成回填对齐；同一项目内唯一。
    external_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # 调用方会话标识，统一转成字符串保存，不建立外键。
    session_ref: Mapped[str | None] = mapped_column(String(128))
    # 仅保存调用方用户标识的哈希，避免在知识库侧留存明文账号。
    user_ref: Mapped[str | None] = mapped_column(String(64))
    query: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    # 回答路径：rag / direct / clarification / chat / health_data / report / refusal 等。
    answer_mode: Mapped[str | None] = mapped_column(String(40))
    intent: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    chat_provider: Mapped[str | None] = mapped_column(String(80))
    chat_model_name: Mapped[str | None] = mapped_column(String(200))
    retrieval_mode: Mapped[str | None] = mapped_column(String(20))
    top_k: Mapped[int | None] = mapped_column(Integer)
    include_context: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    product_name: Mapped[str | None] = mapped_column(String(200))
    model_name: Mapped[str | None] = mapped_column(String(200))
    # 每次检索尝试一行摘要；source 表通过 attempt_index 关联具体命中。
    retrieval_attempts: Mapped[list[dict[str, Any]]] = mapped_column(
        json_type, default=list, nullable=False
    )
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    top_rerank_score: Mapped[float | None] = mapped_column(Float)
    retrieval_duration_ms: Mapped[int | None] = mapped_column(Integer)
    generation_duration_ms: Mapped[int | None] = mapped_column(Integer)
    total_duration_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    # 项目侧回传的用户反馈；点踩会自动进入待补资料，管理端可标记已处理。
    feedback: Mapped[str | None] = mapped_column(String(10))
    feedback_reason: Mapped[str | None] = mapped_column(String(500))
    feedback_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    review_status: Mapped[str] = mapped_column(
        String(20), default="none", server_default=text("'none'"), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)


class ProjectQaSource(TimestampMixin, Base):
    """项目轨迹的召回片段快照；文档删除后仍保留回答当时的正文与分数。"""

    __tablename__ = "project_qa_sources"
    __table_args__ = (
        UniqueConstraint("trace_id", "attempt_index", "rank", name="uq_project_qa_sources_rank"),
        Index("ix_project_qa_sources_trace_attempt", "trace_id", "attempt_index"),
        Index("ix_project_qa_sources_company_created", "company_id", "created_at"),
        Index("ix_project_qa_sources_document", "document_id"),
        {"mysql_engine": "InnoDB", "comment": "项目轨迹召回片段与文本快照"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False
    )
    trace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("project_qa_traces.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 同一次项目请求内的第几次检索，从 0 开始。
    attempt_index: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    document_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="SET NULL")
    )
    chunk_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document_chunks.id", ondelete="SET NULL")
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(500))
    content_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    context_snapshot: Mapped[str | None] = mapped_column(Text)
    cosine_distance: Mapped[float | None] = mapped_column(Float)
    bm25_score: Mapped[float | None] = mapped_column(Float)
    rrf_score: Mapped[float | None] = mapped_column(Float)
    rerank_score: Mapped[float | None] = mapped_column(Float)
    asset_ids: Mapped[list[str]] = mapped_column(json_type, default=list, nullable=False)


class EvaluationSet(TimestampMixin, Base):
    """检索评测集；用例只依赖知识库当前版本，不调用聊天模型。"""

    __tablename__ = "evaluation_sets"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "name", name="uq_evaluation_sets_kb_name"),
        Index("ix_evaluation_sets_kb_created", "knowledge_base_id", "created_at"),
        {"mysql_engine": "InnoDB", "comment": "检索评测集"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))


class EvaluationCase(TimestampMixin, Base):
    """评测用例：记录期望命中的文档或正文关键词，支持启停而不删除历史。"""

    __tablename__ = "evaluation_cases"
    __table_args__ = (
        CheckConstraint(
            "retrieval_mode IN ('hybrid', 'bm25', 'vector')",
            name="ck_evaluation_cases_retrieval_mode",
        ),
        CheckConstraint("top_k BETWEEN 1 AND 20", name="ck_evaluation_cases_top_k"),
        Index("ix_evaluation_cases_set_created", "set_id", "created_at"),
        {"mysql_engine": "InnoDB", "comment": "检索评测用例"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    set_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("evaluation_sets.id", ondelete="CASCADE"),
        nullable=False,
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    # 期望命中的文档 ID 列表；文档被删除或替换后仍保留快照，未命中会如实体现在召回率。
    expected_document_ids: Mapped[list[str]] = mapped_column(
        json_type, default=list, nullable=False
    )
    # 期望在召回正文中出现的关键词；与文档 ID 任一匹配即视为相关。
    expected_keywords: Mapped[list[str]] = mapped_column(json_type, default=list, nullable=False)
    retrieval_mode: Mapped[str] = mapped_column(
        String(20), default="hybrid", server_default=text("'hybrid'"), nullable=False
    )
    top_k: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("1"), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(String(1000))


class EvaluationRun(TimestampMixin, Base):
    """一次评测运行；指标在运行结束时一次性写入，失败保留错误摘要。"""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'failed')",
            name="ck_evaluation_runs_status",
        ),
        Index("ix_evaluation_runs_set_created", "set_id", "created_at"),
        Index("ix_evaluation_runs_kb_created", "knowledge_base_id", "created_at"),
        Index("ix_evaluation_runs_status_created", "status", "created_at"),
        {"mysql_engine": "InnoDB", "comment": "检索评测运行记录"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    set_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("evaluation_sets.id", ondelete="CASCADE"),
        nullable=False,
    )
    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    company_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(
        String(20), default="running", server_default=text("'running'"), nullable=False
    )
    case_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(json_type, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class EvaluationCaseResult(TimestampMixin, Base):
    """单条用例的评测结果；保存命中片段摘要，便于逐条排查。"""

    __tablename__ = "evaluation_case_results"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id", name="uq_evaluation_case_results_run_case"),
        Index("ix_evaluation_case_results_run", "run_id"),
        Index("ix_evaluation_case_results_case", "case_id"),
        {"mysql_engine": "InnoDB", "comment": "检索评测单条用例结果"},
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    case_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("evaluation_cases.id", ondelete="SET NULL")
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_relevant_rank: Mapped[int | None] = mapped_column(Integer)
    recall: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reciprocal_rank: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    matches: Mapped[list[dict[str, Any]]] = mapped_column(json_type, default=list, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)


class OutboxEvent(TimestampMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'published', 'failed')",
            name="ck_outbox_events_status",
        ),
        Index("ix_outbox_events_status_created_at", "status", "created_at"),
        {"mysql_engine": "InnoDB", "comment": "事务 Outbox 事件，供应用内任务循环消费"},
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
