"""Optional project/work metadata for organization agents."""

import sqlalchemy as sa
from alembic import op

revision = "20261002_agent_context"
down_revision = "20261001_agent_checkins"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("workspace_agents")}
    if "work_context" not in columns:
        op.add_column("workspace_agents", sa.Column("work_context", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("workspace_agents", "work_context")
