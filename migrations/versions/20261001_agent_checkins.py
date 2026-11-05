"""Authenticated agent check-ins, independent of reported task/session state."""

import sqlalchemy as sa
from alembic import op

revision = "20261001_agent_checkins"
down_revision = "20260928_join_links"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("workspace_agent_checkins"):
        expected = {"org_id", "agent_id", "channel_id", "last_seen", "mode"}
        if not expected <= {c["name"] for c in inspector.get_columns("workspace_agent_checkins")}:
            raise RuntimeError("Existing agent check-in table has an unsupported schema")
        return
    op.create_table(
        "workspace_agent_checkins",
        sa.Column("org_id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), primary_key=True),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_id"],
            ["workspace_agents.org_id", "workspace_agents.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "channel_id"], ["workspace_rooms.org_id", "workspace_rooms.id"]
        ),
    )


def downgrade():
    op.drop_table("workspace_agent_checkins")
