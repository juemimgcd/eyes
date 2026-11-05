"""Track durable event consumption separately from broker acceptance."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_event_submissions"
down_revision = "0008_report_retention"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "event_submissions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("credential_id", sa.Uuid(), sa.ForeignKey("credentials.id"), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error", postgresql.JSONB()),
        sa.CheckConstraint(
            "status IN ('pending', 'persisted', 'rejected')", name="ck_event_submission_status"
        ),
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM event_submissions WHERE status = 'pending')")
    ):
        raise RuntimeError("drain event submissions before downgrading")
    op.drop_table("event_submissions")
