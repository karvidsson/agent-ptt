"""Reusable organization links and retry-safe agent enrollment."""

import sqlalchemy as sa
from alembic import op

revision = "20260928_join_links"
down_revision = "20260927_onboarding"
branch_labels = None
depends_on = None


def upgrade():
    tables = {
        "workspace_join_links": [
            sa.Column(
                "org_id", sa.String(), sa.ForeignKey("workspace_organizations.id"), primary_key=True
            ),
            sa.Column("digest", sa.String(64), nullable=False, unique=True),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column(
                "created_by", sa.String(), sa.ForeignKey("workspace_users.id"), nullable=False
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        ],
        "workspace_agent_enrollments": [
            sa.Column("credential_digest", sa.String(64), primary_key=True),
            sa.Column(
                "agent_id",
                sa.String(),
                sa.ForeignKey("workspace_agents.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("link_digest", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        ],
    }
    for name, columns in tables.items():
        inspector = sa.inspect(op.get_bind())
        if not inspector.has_table(name):
            op.create_table(name, *columns)
        elif not {c.name for c in columns} <= {c["name"] for c in inspector.get_columns(name)}:
            raise RuntimeError(f"Existing {name} has an unsupported schema")


def downgrade():
    raise RuntimeError("Restore a reviewed backup to remove organization enrollment records")
