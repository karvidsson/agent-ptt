"""Adopt historical channel participation once, preserving future removals."""

import sqlalchemy as sa
from alembic import op

from agent_ptt.workspace_membership_backfill import backfill

revision = "20261002_legacy_memberships"
down_revision = "20261002_agent_channels"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("workspace_data_migrations"):
        op.create_table(
            "workspace_data_migrations", sa.Column("name", sa.String(80), primary_key=True)
        )
    backfill(op.get_bind())


def downgrade():
    # Preserve memberships and the marker: rollback must not resurrect removals.
    pass
