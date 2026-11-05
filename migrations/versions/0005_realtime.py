"""Transactional resource versions, coalescing notifications and receive cursors."""

import sqlalchemy as sa
from alembic import op

revision = "0005_realtime"
down_revision = "0004_experiment_batches"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "resource_revisions",
        sa.Column("project_id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(30), primary_key=True),
        sa.Column("resource_id", sa.Uuid(), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("project_id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(30), primary_key=True),
        sa.Column("resource_id", sa.Uuid(), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failures", sa.Integer(), nullable=False),
    )
    op.create_index("ix_notification_outbox_available_at", "notification_outbox", ["available_at"])
    op.create_table(
        "event_receipts",
        sa.Column("kind", sa.String(30), primary_key=True),
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("receive_version", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("project_id", "kind", "resource_id", "receive_version"),
    )
    for table, kind, resource in (
        ("events", "attempt", "attempt_id"),
        ("observed_events", "observed_run", "run_id"),
    ):
        op.execute(
            sa.text(f"""
            INSERT INTO event_receipts (kind, event_id, project_id, resource_id, receive_version)
            SELECT '{kind}', id, project_id, {resource},
                   row_number() OVER (PARTITION BY {resource} ORDER BY created_at, id)
            FROM {table}
        """)
        )
    op.execute(
        sa.text("""
        INSERT INTO resource_revisions (project_id, kind, resource_id, version, updated_at)
        SELECT project_id, kind, resource_id, max(receive_version), now()
        FROM event_receipts GROUP BY project_id, kind, resource_id
    """)
    )


def downgrade():
    op.drop_table("event_receipts")
    op.drop_table("notification_outbox")
    op.drop_table("resource_revisions")
