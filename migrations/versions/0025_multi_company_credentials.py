"""公司凭据改为多枚：新增 id 与项目名称，旧 Token 原样保留为「默认凭据」。"""

from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import op

revision = "0025_multi_company_credentials"
down_revision = "0024_qa_faq_matched"
branch_labels = None
depends_on = None

_TABLE = "company_credentials"
_DEFAULT_NAME = "默认凭据"

_COLUMNS = (
    sa.Column("company_id", sa.Uuid(), nullable=False),
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("token_hash", sa.String(64), nullable=False),
    sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
)


def _create_multi_table(name: str) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.Uuid(), nullable=False),
        *_COLUMNS,
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_company_credentials_token_hash"),
        sa.UniqueConstraint(
            "company_id", "name", name="uq_company_credentials_company_name"
        ),
        mysql_engine="InnoDB",
        comment="公司访问凭据，仅保存哈希；每公司可多枚",
    )


def _create_single_table(name: str) -> None:
    op.create_table(
        name,
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("company_id"),
        sa.UniqueConstraint("token_hash", name="uq_company_credentials_token_hash"),
        mysql_engine="InnoDB",
        comment="公司访问凭据，仅保存哈希",
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        # MySQL 的 CHAR(32) 需要去掉 UUID() 的连字符，才能与 ORM 的 Uuid 存储一致。
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN id CHAR(32) NULL")
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN name VARCHAR(120) NULL")
        op.execute(
            f"UPDATE {_TABLE} SET id = REPLACE(UUID(), '-', ''), name = '{_DEFAULT_NAME}'"
        )
        op.execute(
            f"ALTER TABLE {_TABLE} MODIFY COLUMN id CHAR(32) NOT NULL, "
            "MODIFY COLUMN name VARCHAR(120) NOT NULL"
        )
        # 先补 company_id 的普通索引，确保仍有外键可用后再替换主键。
        op.execute(
            f"CREATE INDEX ix_company_credentials_company_id ON {_TABLE} (company_id)"
        )
        op.execute(
            f"ALTER TABLE {_TABLE} ADD UNIQUE KEY "
            "uq_company_credentials_company_name (company_id, name)"
        )
        op.execute(f"ALTER TABLE {_TABLE} DROP PRIMARY KEY")
        op.execute(f"ALTER TABLE {_TABLE} ADD PRIMARY KEY (id)")
        op.execute(f"ALTER TABLE {_TABLE} COMMENT '公司访问凭据，仅保存哈希；每公司可多枚'")
        return

    # SQLite 等方言不支持原地修改主键：新增列回填后整表重建。
    op.add_column(_TABLE, sa.Column("id", sa.Uuid(), nullable=True))
    op.add_column(_TABLE, sa.Column("name", sa.String(120), nullable=True))
    rows = bind.execute(sa.text(f"SELECT company_id FROM {_TABLE}")).all()
    for (company_id,) in rows:
        bind.execute(
            sa.text(
                f"UPDATE {_TABLE} SET id = :id, name = :name WHERE company_id = :company_id"
            ),
            {"id": uuid4().hex, "name": _DEFAULT_NAME, "company_id": company_id},
        )
    _create_multi_table(f"{_TABLE}_new")
    op.execute(
        sa.text(
            f"INSERT INTO {_TABLE}_new "
            "(id, company_id, name, token_hash, is_enabled, created_at, updated_at) "
            f"SELECT id, company_id, name, token_hash, is_enabled, created_at, updated_at "
            f"FROM {_TABLE}"
        )
    )
    op.drop_table(_TABLE)
    op.rename_table(f"{_TABLE}_new", _TABLE)
    op.create_index("ix_company_credentials_company_id", _TABLE, ["company_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        # 旧结构每公司只能保留一枚凭据：优先保留启用者，其次最新签发者。
        op.execute(
            f"""
            DELETE doomed FROM {_TABLE} AS doomed
            JOIN {_TABLE} AS keeper
              ON keeper.company_id = doomed.company_id
             AND (
                keeper.is_enabled > doomed.is_enabled
                OR (keeper.is_enabled = doomed.is_enabled
                    AND keeper.created_at > doomed.created_at)
                OR (keeper.is_enabled = doomed.is_enabled
                    AND keeper.created_at = doomed.created_at
                    AND keeper.id > doomed.id)
             )
            """
        )
        op.execute(f"ALTER TABLE {_TABLE} DROP PRIMARY KEY")
        op.execute(f"ALTER TABLE {_TABLE} ADD PRIMARY KEY (company_id)")
        op.execute(f"ALTER TABLE {_TABLE} DROP INDEX ix_company_credentials_company_id")
        op.execute(
            f"ALTER TABLE {_TABLE} DROP INDEX uq_company_credentials_company_name"
        )
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN name, DROP COLUMN id")
        op.execute(f"ALTER TABLE {_TABLE} COMMENT '公司访问凭据，仅保存哈希'")
        return

    rows = bind.execute(
        sa.text(
            f"SELECT id, company_id, token_hash, is_enabled, created_at, updated_at "
            f"FROM {_TABLE} ORDER BY company_id, is_enabled DESC, created_at DESC, id DESC"
        )
    ).mappings()
    keepers: dict[str, dict[str, object]] = {}
    for row in rows:
        keeper = dict(row)
        # 回填时可能读到 UUID 对象，统一转成 CHAR(32) 兼容的十六进制字符串。
        company_id = keeper["company_id"]
        if isinstance(company_id, UUID):
            company_id = company_id.hex
        keepers.setdefault(str(company_id), keeper)

    _create_single_table(f"{_TABLE}_legacy")
    for company_id, row in keepers.items():
        bind.execute(
            sa.text(
                f"INSERT INTO {_TABLE}_legacy "
                "(company_id, token_hash, is_enabled, created_at, updated_at) "
                "VALUES (:company_id, :token_hash, :is_enabled, :created_at, :updated_at)"
            ),
            {
                "company_id": company_id,
                "token_hash": row["token_hash"],
                "is_enabled": int(bool(row["is_enabled"])),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
        )
    op.drop_table(_TABLE)
    op.rename_table(f"{_TABLE}_legacy", _TABLE)
