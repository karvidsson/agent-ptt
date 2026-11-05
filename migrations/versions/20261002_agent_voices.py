"""Persist agent voice choices."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from agent_ptt.workspace_voices import assign_missing_voices

revision = "20261002_agent_voices"
down_revision = "20261002_legacy_memberships"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    columns = {c["name"] for c in sa.inspect(connection).get_columns("workspace_agents")}
    if "voice_id" not in columns:
        op.add_column("workspace_agents", sa.Column("voice_id", sa.String(40), nullable=True))
    with Session(bind=connection) as db:
        assign_missing_voices(db)
        db.commit()


def downgrade():
    op.drop_column("workspace_agents", "voice_id")
