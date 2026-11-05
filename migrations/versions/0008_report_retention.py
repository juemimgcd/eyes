"""Preserve report identity when configured retention removes its content."""

import sqlalchemy as sa
from alembic import op

revision = "0008_report_retention"
down_revision = "0007_discovery"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "comparison_reports", sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM comparison_reports WHERE expired_at IS NOT NULL)")
    ):
        raise RuntimeError(
            "expired report content cannot be restored by downgrade; restore a backup"
        )
    op.drop_column("comparison_reports", "expired_at")
