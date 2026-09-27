"""Deterministic voice design — hash a handle into a stable catalog voice.

When a participant joins without picking a voice, we design one from
their handle and pin it in the database, so the same handle always
sounds the same across sessions. Uses SHA-256 for repeatable assignments.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from agent_ptt.models import PinnedVoiceDB, VoiceProfile
from agent_ptt.tts import POCKET_VOICES
from agent_ptt.voices import get_voice_profile, save_voice_profile


def design_voice(handle: str, engine: str = "pocket-tts") -> VoiceProfile:
    """Assign a stable Pocket catalog voice from a case-insensitive handle."""
    if engine != "pocket-tts":
        raise ValueError(f"Unknown voice design engine: {engine}")
    h = int(hashlib.sha256(handle.lower().encode()).hexdigest(), 16)
    return VoiceProfile(
        voice_id=f"auto-{handle.lower()}",
        display_name=f"{handle}'s Voice",
        engine="pocket-tts",
        settings={"voice": POCKET_VOICES[h % len(POCKET_VOICES)]},
    )


def _pin_voice(handle: str, profile: VoiceProfile, source: str, db: Session) -> None:
    save_voice_profile(profile, db)
    db.merge(
        PinnedVoiceDB(
            handle=handle.lower(),
            voice_id=profile.voice_id,
            source=source,
            created_at=datetime.now(UTC),
        )
    )
    db.commit()


def get_or_create_pinned_voice(
    handle: str,
    db: Session,
    engine: str = "pocket-tts",
) -> VoiceProfile:
    """Return the voice pinned to a handle, designing and pinning one if absent."""
    pin = db.get(PinnedVoiceDB, handle.lower())
    if pin is not None:
        profile = get_voice_profile(pin.voice_id, db)
        if profile is not None:
            return profile

    profile, source = design_voice(handle, engine), "hash"
    _pin_voice(handle, profile, source, db)
    return profile


def redesign_pinned_voice(
    handle: str,
    db: Session,
    engine: str = "pocket-tts",
) -> VoiceProfile:
    """Design a fresh voice for a handle, replacing any existing pin."""
    profile, source = design_voice(handle, engine), "hash"
    _pin_voice(handle, profile, source, db)
    return profile


def list_pinned_voices(db: Session) -> list[dict]:
    """All pinned voices with their profile settings, newest first."""
    from sqlalchemy import select

    pins = db.scalars(select(PinnedVoiceDB)).all()
    result = []
    for pin in sorted(pins, key=lambda p: p.created_at or datetime.min, reverse=True):
        profile = get_voice_profile(pin.voice_id, db)
        result.append(
            {
                "handle": pin.handle,
                "voice_id": pin.voice_id,
                "source": pin.source,
                "engine": profile.engine if profile else None,
                "settings": profile.settings if profile else {},
            }
        )
    return result
