"""Persist one bounded connection-check slot per Runner."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_discovery"
down_revision = "0006_cache_epoch"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_discoveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("runner_id", sa.Uuid(), sa.ForeignKey("runners.id"), nullable=False, unique=True),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", JSONB, nullable=True),
    )


def downgrade():
    op.drop_table("agent_discoveries")
