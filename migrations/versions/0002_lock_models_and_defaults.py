"""为 MySQL 增加默认模型约束和不可变配置触发器。"""

from collections.abc import Sequence
from logging import getLogger

import sqlalchemy as sa
from alembic import op

revision: str = "0002_model_defaults"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = getLogger("alembic.runtime.migration")


def _ensure_innodb_tables() -> None:
    """修复首次迁移留下的 MyISAM 表；转换会保留表内记录。"""
    connection = op.get_bind()
    if connection.dialect.name != "mysql":
        return
    table_names = (
        "alembic_version",
        "projects",
        "model_profiles",
        "vector_store_profiles",
        "api_credentials",
        "knowledge_bases",
        "documents",
        "ingestion_jobs",
        "document_chunks",
        "outbox_events",
    )
    for table_name in table_names:
        engine = connection.execute(
            sa.text(
                "SELECT ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name"
            ),
            {"table_name": table_name},
        ).scalar_one_or_none()
        if engine and engine.lower() != "innodb":
            op.execute(sa.text(f"ALTER TABLE `{table_name}` ENGINE=InnoDB"))


def _ensure_initial_foreign_keys() -> None:
    """MyISAM 会忽略外键，转换后补齐初始迁移中未实际落库的约束。"""
    foreign_keys = (
        ("api_credentials", "project_id", "projects", "id", "CASCADE", "fk_api_cred_project"),
        ("knowledge_bases", "project_id", "projects", "id", "CASCADE", "fk_kb_project"),
        (
            "knowledge_bases",
            "embedding_model_id",
            "model_profiles",
            "id",
            "RESTRICT",
            "fk_kb_embedding_model",
        ),
        (
            "knowledge_bases",
            "rerank_model_id",
            "model_profiles",
            "id",
            "RESTRICT",
            "fk_kb_rerank_model",
        ),
        (
            "knowledge_bases",
            "vector_store_profile_id",
            "vector_store_profiles",
            "id",
            "RESTRICT",
            "fk_kb_vector_store",
        ),
        ("documents", "knowledge_base_id", "knowledge_bases", "id", "CASCADE", "fk_docs_kb"),
        ("ingestion_jobs", "document_id", "documents", "id", "CASCADE", "fk_jobs_document"),
        (
            "document_chunks",
            "document_id",
            "documents",
            "id",
            "CASCADE",
            "fk_chunks_document",
        ),
    )
    inspector = sa.inspect(op.get_bind())
    for source, column, target, target_column, ondelete, name in foreign_keys:
        exists = any(
            foreign_key["constrained_columns"] == [column]
            and foreign_key["referred_table"] == target
            for foreign_key in inspector.get_foreign_keys(source)
        )
        if not exists:
            op.create_foreign_key(
                name,
                source,
                target,
                [column],
                [target_column],
                ondelete=ondelete,
            )
            inspector = sa.inspect(op.get_bind())


def _has_column(table_name: str, column_name: str) -> bool:
    """兼容 SQLAlchemy 版本差异，检查迁移重试所需的列是否已存在。"""
    return any(
        column["name"] == column_name
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    )


def _create_trigger(statement: str, name: str) -> None:
    """账号缺少 MySQL binlog 触发器权限时依靠应用层规则继续迁移。"""
    try:
        op.execute(statement)
    except sa.exc.OperationalError as error:
        original = error.orig
        if not getattr(original, "args", ()) or original.args[0] != 1419:
            raise
        logger.warning(
            "MySQL 账号无权创建触发器 %s（错误 1419），已跳过；模型绑定仍由应用层锁定。",
            name,
        )


def upgrade() -> None:
    # 先修复 0001 已落库的表引擎和外键，再创建依赖 InnoDB 的生成列索引。
    _ensure_innodb_tables()
    _ensure_initial_foreign_keys()

    if not _has_column("model_profiles", "is_default"):
        op.add_column(
            "model_profiles",
            sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        )
    # NULL 值可重复，默认模型按 kind 生成唯一键并由 MySQL 原子约束。
    if not _has_column("model_profiles", "default_kind"):
        op.add_column(
            "model_profiles",
            sa.Column(
                "default_kind",
                sa.String(length=20),
                sa.Computed("CASE WHEN is_default THEN kind ELSE NULL END"),
                nullable=True,
            ),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("model_profiles")}
    if "uq_model_profiles_kind_default" not in indexes:
        op.create_index(
            "uq_model_profiles_kind_default", "model_profiles", ["default_kind"], unique=True
        )

    # 已存在的知识库也按“创建即锁定”规则处理。
    op.execute(
        "UPDATE knowledge_bases "
        "SET models_locked_at = COALESCE(models_locked_at, CURRENT_TIMESTAMP)"
    )
    op.alter_column(
        "knowledge_bases",
        "models_locked_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )

    # MySQL 触发器直接拒绝已锁定知识库的模型或向量库绑定变更。
    op.execute("DROP TRIGGER IF EXISTS trg_knowledge_bases_model_binding_immutable")
    _create_trigger(
        """
        CREATE TRIGGER trg_knowledge_bases_model_binding_immutable
        BEFORE UPDATE ON knowledge_bases
        FOR EACH ROW
        BEGIN
            IF OLD.models_locked_at IS NOT NULL AND (
                NEW.models_locked_at IS NULL
                OR NOT (NEW.embedding_model_id <=> OLD.embedding_model_id)
                OR NOT (NEW.rerank_model_id <=> OLD.rerank_model_id)
                OR NOT (NEW.vector_store_profile_id <=> OLD.vector_store_profile_id)
            ) THEN
                SIGNAL SQLSTATE '45000'
                    SET MESSAGE_TEXT = 'knowledge base model configuration is immutable';
            END IF;
        END
        """,
        "trg_knowledge_bases_model_binding_immutable",
    )

    # 为升级前创建的入库事件补上 Worker 所需任务 ID；MySQL UUID 列以 32 位十六进制保存。
    op.execute(
        """
        UPDATE outbox_events AS event
        JOIN ingestion_jobs AS job ON event.aggregate_id = job.document_id
        SET event.payload = JSON_SET(
            event.payload,
            '$.job_id',
            CONCAT(
                SUBSTRING(job.id, 1, 8), '-', SUBSTRING(job.id, 9, 4), '-',
                SUBSTRING(job.id, 13, 4), '-', SUBSTRING(job.id, 17, 4), '-',
                SUBSTRING(job.id, 21, 12)
            )
        )
        WHERE event.event_type = 'document.ingestion_requested'
          AND JSON_CONTAINS_PATH(event.payload, 'one', '$.job_id') = 0
        """
    )

    # 已绑定模型的供应商、模型名、维度和设置不能原地改变，密钥引用仍允许轮换。
    op.execute("DROP TRIGGER IF EXISTS trg_bound_model_profile_immutable")
    _create_trigger(
        """
        CREATE TRIGGER trg_bound_model_profile_immutable
        BEFORE UPDATE ON model_profiles
        FOR EACH ROW
        BEGIN
            DECLARE profile_is_bound INT DEFAULT 0;
            IF NOT (NEW.kind <=> OLD.kind)
                OR NOT (NEW.provider <=> OLD.provider)
                OR NOT (NEW.model_name <=> OLD.model_name)
                OR NOT (NEW.dimensions <=> OLD.dimensions)
                OR NOT (NEW.settings <=> OLD.settings)
            THEN
                SELECT EXISTS (
                    SELECT 1 FROM knowledge_bases
                    WHERE embedding_model_id = OLD.id OR rerank_model_id = OLD.id
                ) INTO profile_is_bound;
                IF profile_is_bound = 1 THEN
                    SIGNAL SQLSTATE '45000'
                        SET MESSAGE_TEXT = 'bound model identity is immutable';
                END IF;
            END IF;
        END
        """,
        "trg_bound_model_profile_immutable",
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_bound_model_profile_immutable")
    op.execute("DROP TRIGGER IF EXISTS trg_knowledge_bases_model_binding_immutable")
    op.alter_column(
        "knowledge_bases",
        "models_locked_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
        server_default=None,
    )
    op.drop_index("uq_model_profiles_kind_default", table_name="model_profiles")
    op.drop_column("model_profiles", "default_kind")
    op.drop_column("model_profiles", "is_default")
