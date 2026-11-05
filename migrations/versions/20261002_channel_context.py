"""Project and purpose metadata for channels."""

import sqlalchemy as sa
from alembic import op

revision = "20261002_channel_context"
down_revision = "20261002_agent_context"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("workspace_rooms")}
    for name, size in (("project", 160), ("description", 500)):
        if name not in columns:
            op.add_column("workspace_rooms", sa.Column(name, sa.String(size), nullable=True))


def downgrade():
    op.drop_column("workspace_rooms", "description")
    op.drop_column("workspace_rooms", "project")
