"""检索评测集：评测集、用例、运行记录与逐条结果。"""

import sqlalchemy as sa
from alembic import op

revision = "0030_retrieval_evaluation"
down_revision = "0029_feedback_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_sets",
        sa.Column("id", sa.Uuid(), nullable=False, comment="评测集 ID"),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False, comment="所属知识库 ID"),
        sa.Column("name", sa.String(160), nullable=False, comment="评测集名称，同一知识库内唯一"),
        sa.Column("description", sa.String(1000), nullable=True, comment="评测集说明"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="最后更新时间",
        ),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("knowledge_base_id", "name", name="uq_evaluation_sets_kb_name"),
        comment="检索评测集",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_evaluation_sets_kb_created", "evaluation_sets", ["knowledge_base_id", "created_at"]
    )

    op.create_table(
        "evaluation_cases",
        sa.Column("id", sa.Uuid(), nullable=False, comment="用例 ID"),
        sa.Column("set_id", sa.Uuid(), nullable=False, comment="所属评测集 ID"),
        sa.Column("query", sa.Text(), nullable=False, comment="用户问题原文"),
        sa.Column(
            "expected_document_ids",
            sa.JSON(),
            nullable=False,
            comment="期望命中的文档 ID 列表",
        ),
        sa.Column(
            "expected_keywords",
            sa.JSON(),
            nullable=False,
            comment="期望在召回正文中出现的关键词列表",
        ),
        sa.Column(
            "retrieval_mode",
            sa.String(20),
            server_default=sa.text("'hybrid'"),
            nullable=False,
            comment="检索模式：hybrid、bm25 或 vector",
        ),
        sa.Column(
            "top_k",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
            comment="本次用例的召回条数",
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
            comment="是否参与评测；关闭后保留历史结果",
        ),
        sa.Column("notes", sa.String(1000), nullable=True, comment="用例备注"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="最后更新时间",
        ),
        sa.CheckConstraint(
            "retrieval_mode IN ('hybrid', 'bm25', 'vector')",
            name="ck_evaluation_cases_retrieval_mode",
        ),
        sa.CheckConstraint("top_k BETWEEN 1 AND 20", name="ck_evaluation_cases_top_k"),
        sa.ForeignKeyConstraint(["set_id"], ["evaluation_sets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="检索评测用例",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_evaluation_cases_set_created", "evaluation_cases", ["set_id", "created_at"])

    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.Uuid(), nullable=False, comment="运行 ID"),
        sa.Column("set_id", sa.Uuid(), nullable=False, comment="所属评测集 ID"),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False, comment="评测知识库 ID"),
        sa.Column("company_id", sa.Uuid(), nullable=True, comment="所属公司 ID"),
        sa.Column(
            "status",
            sa.String(20),
            server_default=sa.text("'running'"),
            nullable=False,
            comment="运行状态：running、success 或 failed",
        ),
        sa.Column("case_count", sa.Integer(), nullable=False, comment="本次参与评测的用例数"),
        sa.Column(
            "completed_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="已完成的用例数",
        ),
        sa.Column(
            "metrics",
            sa.JSON(),
            nullable=False,
            comment="汇总指标：命中率、召回率、MRR、无结果率和平均耗时",
        ),
        sa.Column("error_message", sa.Text(), nullable=True, comment="运行级失败摘要"),
        sa.Column(
            "started_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="开始时间",
        ),
        sa.Column("finished_at", sa.DateTime(), nullable=True, comment="完成时间"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="最后更新时间",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'failed')", name="ck_evaluation_runs_status"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["set_id"], ["evaluation_sets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="检索评测运行记录",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_evaluation_runs_set_created", "evaluation_runs", ["set_id", "created_at"])
    op.create_index(
        "ix_evaluation_runs_kb_created", "evaluation_runs", ["knowledge_base_id", "created_at"]
    )
    op.create_index(
        "ix_evaluation_runs_status_created", "evaluation_runs", ["status", "created_at"]
    )

    op.create_table(
        "evaluation_case_results",
        sa.Column("id", sa.Uuid(), nullable=False, comment="用例结果 ID"),
        sa.Column("run_id", sa.Uuid(), nullable=False, comment="所属运行 ID"),
        sa.Column("case_id", sa.Uuid(), nullable=True, comment="原用例 ID；用例删除后为空"),
        sa.Column("query", sa.Text(), nullable=False, comment="评测时的问题快照"),
        sa.Column("retrieval_mode", sa.String(20), nullable=False, comment="评测时的检索模式"),
        sa.Column("top_k", sa.Integer(), nullable=False, comment="评测时的召回条数"),
        sa.Column("hit", sa.Boolean(), nullable=False, comment="top_k 内是否命中期望资料"),
        sa.Column(
            "first_relevant_rank",
            sa.Integer(),
            nullable=True,
            comment="第一个相关结果的排名；未命中时为空",
        ),
        sa.Column("recall", sa.Float(), nullable=False, comment="本用例的召回率"),
        sa.Column("reciprocal_rank", sa.Float(), nullable=False, comment="本用例的倒数排名"),
        sa.Column("result_count", sa.Integer(), nullable=False, comment="实际召回条数"),
        sa.Column("duration_ms", sa.Integer(), nullable=True, comment="检索耗时，单位毫秒"),
        sa.Column(
            "matches",
            sa.JSON(),
            nullable=False,
            comment="召回结果摘要：排名、文档、章节与命中方式",
        ),
        sa.Column("error_message", sa.Text(), nullable=True, comment="单条用例失败摘要"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="最后更新时间",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["evaluation_cases.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["evaluation_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "case_id", name="uq_evaluation_case_results_run_case"),
        comment="检索评测单条用例结果",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_evaluation_case_results_run", "evaluation_case_results", ["run_id"])
    op.create_index("ix_evaluation_case_results_case", "evaluation_case_results", ["case_id"])


def downgrade() -> None:
    op.drop_index("ix_evaluation_case_results_case", table_name="evaluation_case_results")
    op.drop_index("ix_evaluation_case_results_run", table_name="evaluation_case_results")
    op.drop_table("evaluation_case_results")
    op.drop_index("ix_evaluation_runs_status_created", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_kb_created", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_set_created", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_index("ix_evaluation_cases_set_created", table_name="evaluation_cases")
    op.drop_table("evaluation_cases")
    op.drop_index("ix_evaluation_sets_kb_created", table_name="evaluation_sets")
    op.drop_table("evaluation_sets")
