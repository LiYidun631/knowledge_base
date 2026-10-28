"""在线问答与项目轨迹增加用户反馈和复核状态。"""

import sqlalchemy as sa
from alembic import op

revision = "0029_feedback_loop"
down_revision = "0028_column_comments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 问答记录：用户点踩后自动进入待补资料，reason 只保存用户主动填写的简述。
    op.add_column(
        "qa_records",
        sa.Column(
            "feedback",
            sa.String(10),
            nullable=True,
            comment="用户反馈：up 有帮助、down 无帮助",
        ),
    )
    op.add_column(
        "qa_records",
        sa.Column("feedback_reason", sa.String(500), nullable=True, comment="用户填写的反馈原因"),
    )
    op.add_column(
        "qa_records",
        sa.Column("feedback_at", sa.DateTime(), nullable=True, comment="最近一次反馈时间"),
    )
    op.create_check_constraint(
        "ck_qa_records_feedback",
        "qa_records",
        "feedback IS NULL OR feedback IN ('up', 'down')",
    )
    op.create_index("ix_qa_records_feedback_created", "qa_records", ["feedback", "created_at"])

    # 项目轨迹：外部项目可回传终端用户反馈，同样进入待补资料闭环。
    op.add_column(
        "project_qa_traces",
        sa.Column(
            "feedback",
            sa.String(10),
            nullable=True,
            comment="用户反馈：up 有帮助、down 无帮助",
        ),
    )
    op.add_column(
        "project_qa_traces",
        sa.Column("feedback_reason", sa.String(500), nullable=True, comment="用户填写的反馈原因"),
    )
    op.add_column(
        "project_qa_traces",
        sa.Column("feedback_at", sa.DateTime(), nullable=True, comment="最近一次反馈时间"),
    )
    op.add_column(
        "project_qa_traces",
        sa.Column(
            "review_status",
            sa.String(20),
            server_default=sa.text("'none'"),
            nullable=False,
            comment="复核状态：none 未复核、pending 待补资料、resolved 已补充",
        ),
    )
    op.create_check_constraint(
        "ck_project_qa_traces_feedback",
        "project_qa_traces",
        "feedback IS NULL OR feedback IN ('up', 'down')",
    )
    op.create_check_constraint(
        "ck_project_qa_traces_review_status",
        "project_qa_traces",
        "review_status IN ('none', 'pending', 'resolved')",
    )
    op.create_index(
        "ix_project_qa_traces_feedback_created",
        "project_qa_traces",
        ["feedback", "created_at"],
    )
    op.create_index(
        "ix_project_qa_traces_review_created",
        "project_qa_traces",
        ["review_status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_qa_traces_review_created", table_name="project_qa_traces")
    op.drop_index("ix_project_qa_traces_feedback_created", table_name="project_qa_traces")
    op.drop_constraint("ck_project_qa_traces_review_status", "project_qa_traces", type_="check")
    op.drop_constraint("ck_project_qa_traces_feedback", "project_qa_traces", type_="check")
    op.drop_column("project_qa_traces", "review_status")
    op.drop_column("project_qa_traces", "feedback_at")
    op.drop_column("project_qa_traces", "feedback_reason")
    op.drop_column("project_qa_traces", "feedback")

    op.drop_index("ix_qa_records_feedback_created", table_name="qa_records")
    op.drop_constraint("ck_qa_records_feedback", "qa_records", type_="check")
    op.drop_column("qa_records", "feedback_at")
    op.drop_column("qa_records", "feedback_reason")
    op.drop_column("qa_records", "feedback")
