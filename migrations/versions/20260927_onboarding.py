"""Bounded developer invitations and agent onboarding metadata."""

import sqlalchemy as sa
from alembic import op

revision = "20260927_onboarding"
down_revision = "20260927_presence"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable IDs here are invitation/creator metadata, not authorization grants.
    # Channel tenant ownership is checked on both invitation creation and redemption.
    additions = {
        "workspace_invitations": [
            sa.Column("agent_limit", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("channel_id", sa.String(), nullable=True),
            sa.Column("claimed_by", sa.String(), nullable=True),
        ],
        "workspace_agents": [
            sa.Column("onboarded_by", sa.String(), nullable=True),
            sa.Column("harness", sa.String(20), nullable=True),
        ],
    }
    for table, columns in additions.items():
        existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)


def downgrade():
    raise RuntimeError("Restore a reviewed backup to remove onboarding records")
