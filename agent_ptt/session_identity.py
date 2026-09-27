"""Persistent names and existing voice assignments for CLI sessions."""

from collections import Counter
from secrets import choice
from threading import Lock

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_ptt.models import SessionIdentityDB
from agent_ptt.tts import has_backend
from agent_ptt.voicedesign import get_or_create_pinned_voice
from agent_ptt.voices import get_voice_profile, list_voice_profiles

NAMES = (
    "Atlas",
    "Juniper",
    "Nova",
    "Milo",
    "Cleo",
    "Otis",
    "Piper",
    "Felix",
    "Iris",
    "Theo",
    "Luna",
    "Jasper",
    "Wren",
    "Arlo",
    "Hazel",
    "Finn",
    "Olive",
    "Oscar",
    "Ruby",
    "Hugo",
    "Sage",
    "Remy",
    "Ivy",
    "Leo",
    "Ember",
    "Rowan",
    "Mabel",
    "Nico",
    "Freya",
    "Ezra",
    "Pearl",
    "Luca",
    "Willow",
    "Orion",
    "Ada",
    "Silas",
    "Poppy",
    "Otto",
    "Zelda",
    "Rory",
)
_identity_lock = Lock()  # The channel server runs in one process.


def get_session_identity(session_id: str, agent: str, db: Session) -> SessionIdentityDB:
    """Allocate once; reuse the pair on every join, including after a restart."""
    with _identity_lock:
        identity = db.get(SessionIdentityDB, (session_id, agent))
        if identity and get_voice_profile(identity.voice_id, db):
            return identity

        existing = list(db.scalars(select(SessionIdentityDB)))
        if identity is None:
            used = {item.handle for item in existing}
            available = [name for name in NAMES if name not in used]
            suffix = 2
            while not available:
                available = [f"{name} {suffix}" for name in NAMES if f"{name} {suffix}" not in used]
                suffix += 1
            identity = SessionIdentityDB(
                session_id=session_id, agent=agent, handle=choice(available)
            )

        profiles = [p for p in list_voice_profiles(db) if has_backend(p.engine)]
        # Prefer the deliberately created library over old auto-generated handles.
        curated = [p for p in profiles if not p.voice_id.startswith("auto-")]
        pool = curated or profiles
        if pool:
            usage = Counter(item.voice_id for item in existing)
            lowest = min(usage[p.voice_id] for p in pool)
            identity.voice_id = choice([p for p in pool if usage[p.voice_id] == lowest]).voice_id
        else:
            # Fresh installations still work before a voice library is created.
            identity.voice_id = get_or_create_pinned_voice(identity.handle, db).voice_id
        db.add(identity)
        db.commit()
        return identity
