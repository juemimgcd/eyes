"""Personal accounts and expiring browser sessions; retire shared user tokens."""

import sqlalchemy as sa
from alembic import op

revision = "0012_personal_accounts"
down_revision = "0011_agent_pairings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("username", sa.String(40), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False, unique=True
        ),
    )
    op.create_table(
        "auth_rate_limits",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )
    op.create_index("ix_auth_rate_limits_started_at", "auth_rate_limits", ["started_at"])
    op.add_column("credentials", sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id")))
    op.add_column("credentials", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.create_index("ix_credentials_account_id", "credentials", ["account_id"])
    # Legacy shared data is deliberately not assigned to a newly registered user.
    op.execute("UPDATE credentials SET revoked = true WHERE role IN ('read', 'manage')")


def downgrade():
    # Never resurrect credentials revoked during the upgrade.
    op.drop_index("ix_credentials_account_id", table_name="credentials")
    op.drop_column("credentials", "expires_at")
    op.drop_column("credentials", "account_id")
    op.drop_table("auth_rate_limits")
    op.drop_table("accounts")
