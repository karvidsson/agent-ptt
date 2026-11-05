"""Persisted agent voices from the shipped catalog."""

from collections import Counter
from secrets import choice

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_ptt.voice_catalog import BUILTIN_VOICES
from agent_ptt.workspace_models import Agent, Organization


def choose_voice(org_id: str, db: Session) -> str:
    # Serialize allocation within an organization on PostgreSQL.
    db.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())
    counts = Counter(
        db.scalars(
            select(Agent.voice_id).where(
                Agent.org_id == org_id,
                Agent.active.is_(True),
            )
        )
    )
    lowest = min(counts[name] for name in BUILTIN_VOICES)
    return choice([name for name in BUILTIN_VOICES if counts[name] == lowest])


def assign_missing_voices(db: Session) -> None:
    for agent in db.scalars(select(Agent).where(Agent.voice_id.is_(None)).order_by(Agent.id)):
        agent.voice_id = choose_voice(agent.org_id, db)
        db.flush()
