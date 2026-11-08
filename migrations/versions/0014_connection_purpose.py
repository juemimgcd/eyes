"""Separate observation-only connections from evaluation permission."""

import sqlalchemy as sa
from alembic import op

revision = "0014_connection_purpose"
down_revision = "0013_tunnel_connections"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_pairings",
        sa.Column("purpose", sa.String(20), nullable=False, server_default="evaluate"),
    )
    op.create_check_constraint(
        "ck_pairing_purpose", "agent_pairings", "purpose IN ('observe', 'evaluate')"
    )


def downgrade():
    # Older code cannot enforce observation-only permission; revoke those connections first.
    op.execute(
        "UPDATE credentials SET revoked = true WHERE id IN "
        "(SELECT credential_id FROM agent_pairings WHERE purpose = 'observe')"
    )
    op.execute("UPDATE agent_pairings SET revoked = true WHERE purpose = 'observe'")
    op.drop_constraint("ck_pairing_purpose", "agent_pairings", type_="check")
    op.drop_column("agent_pairings", "purpose")
