"""Track durable final-result consumption separately from broker acceptance."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_result_submissions"
down_revision = "0009_event_submissions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "result_submissions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("credential_id", sa.Uuid(), sa.ForeignKey("credentials.id"), nullable=False),
        sa.Column("work_id", sa.Uuid(), sa.ForeignKey("work_items.id"), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error", postgresql.JSONB()),
        sa.CheckConstraint(
            "status IN ('pending', 'persisted', 'rejected')", name="ck_result_submission_status"
        ),
    )

    op.create_index("ix_result_submissions_work_id", "result_submissions", ["work_id"])


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM result_submissions WHERE status = 'pending')")
    ):
        raise RuntimeError("drain result submissions before downgrading")
    op.drop_table("result_submissions")
