"""Separate restored database timelines in the query cache."""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0006_cache_epoch"
down_revision = "0005_realtime"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "cache_epoch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("epoch", sa.Uuid(), nullable=False),
    )
    op.bulk_insert(table, [{"id": 1, "epoch": uuid4()}])


def downgrade():
    op.drop_table("cache_epoch")
