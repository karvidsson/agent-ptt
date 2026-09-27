"""Voice profile persistence — CRUD over the voice_profiles table."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_ptt.models import VoiceProfile, VoiceProfileDB


def save_voice_profile(profile: VoiceProfile, db: Session) -> VoiceProfile:
    """Insert or update a voice profile (upsert by voice_id)."""
    db.merge(
        VoiceProfileDB(
            voice_id=profile.voice_id,
            display_name=profile.display_name,
            engine=profile.engine,
            settings=profile.settings,
            created_at=profile.created_at,
        )
    )
    db.commit()
    return profile


def get_voice_profile(voice_id: str, db: Session) -> VoiceProfile | None:
    """Look up a voice profile by ID."""
    row = db.get(VoiceProfileDB, voice_id)
    return VoiceProfile.model_validate(row) if row else None


def list_voice_profiles(db: Session, engine: str | None = None) -> list[VoiceProfile]:
    """List stored voice profiles, optionally filtered by engine."""
    stmt = select(VoiceProfileDB)
    if engine:
        stmt = stmt.where(VoiceProfileDB.engine == engine)
    return [VoiceProfile.model_validate(row) for row in db.scalars(stmt)]


def delete_voice_profile(voice_id: str, db: Session) -> bool:
    """Delete a voice profile. Returns True if it existed."""
    row = db.get(VoiceProfileDB, voice_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def migrate_legacy_voice_profiles(db: Session) -> None:
    """Replace removed engines while preserving profile IDs and existing pins."""
    from agent_ptt.voicedesign import design_voice

    rows = db.scalars(
        select(VoiceProfileDB).where(VoiceProfileDB.engine.in_(["edge-tts", "system", "omnivoice"]))
    )
    for row in rows:
        replacement = design_voice(row.voice_id)
        row.engine = replacement.engine
        row.settings = replacement.settings
    db.commit()
