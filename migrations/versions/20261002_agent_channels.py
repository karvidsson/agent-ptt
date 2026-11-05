"""Persistent membership of multiple channels per agent."""

import sqlalchemy as sa
from alembic import op

revision = "20261002_agent_channels"
down_revision = "20261002_channel_context"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("workspace_agent_channels"):
        return
    op.create_table(
        "workspace_agent_channels",
        sa.Column("org_id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), primary_key=True),
        sa.Column("channel_id", sa.String(), primary_key=True),
        sa.Column("joined_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_id"],
            ["workspace_agents.org_id", "workspace_agents.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "channel_id"],
            ["workspace_rooms.org_id", "workspace_rooms.id"],
            ondelete="CASCADE",
        ),
    )


def downgrade():
    op.drop_table("workspace_agent_channels")
