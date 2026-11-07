"""Restricted tunnel credentials and connection liveness."""

import sqlalchemy as sa
from alembic import op

revision = "0013_tunnel_connections"
down_revision = "0012_personal_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_credential_role", "credentials", type_="check")
    op.create_check_constraint(
        "ck_credential_role", "credentials", "role IN ('read', 'manage', 'runner', 'tunnel')"
    )
    op.add_column("agent_pairings", sa.Column("last_seen_at", sa.DateTime(timezone=True)))
    op.add_column(
        "agent_pairings",
        sa.Column(
            "observation_source_id",
            sa.Uuid(),
            sa.ForeignKey("observation_sources.id", name="fk_pairing_observation"),
        ),
    )


def downgrade():
    # Existing tunnel credentials cannot become Runner credentials during rollback.
    op.execute(
        "DELETE FROM agent_pairings WHERE credential_id IN "
        "(SELECT id FROM credentials WHERE role = 'tunnel')"
    )
    op.execute("DELETE FROM credentials WHERE role = 'tunnel'")
    op.drop_column("agent_pairings", "observation_source_id")
    op.drop_column("agent_pairings", "last_seen_at")
    op.drop_constraint("ck_credential_role", "credentials", type_="check")
    op.create_check_constraint(
        "ck_credential_role", "credentials", "role IN ('read', 'manage', 'runner')"
    )
