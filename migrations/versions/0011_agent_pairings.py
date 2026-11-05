"""Single-use connector pairing and scoped credential association."""

import sqlalchemy as sa
from alembic import op

revision = "0011_agent_pairings"
down_revision = "0010_result_submissions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_pairings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("code_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credential_id", sa.Uuid(), sa.ForeignKey("credentials.id")),
        sa.Column("target_version_id", sa.Uuid(), sa.ForeignKey("target_versions.id")),
        sa.Column("redemption_digest", sa.String(64)),
        sa.Column("revoked", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_agent_pairings_project_id", "agent_pairings", ["project_id"])


def downgrade():
    op.drop_table("agent_pairings")
