"""Persistent names and existing voice assignments for CLI sessions."""

from collections import Counter
from datetime import datetime
from secrets import choice
from threading import Lock

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent_ptt.models import ChannelDB, ParticipantKeyDB, SessionIdentityDB
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
_EPOCH = datetime.min


def _naive(value: datetime | None) -> datetime:
    """SQLite hands back naive UTC datetimes; fresh objects may be aware."""
    if value is None:
        return _EPOCH
    return value.replace(tzinfo=None)


def _busy_handles(db: Session) -> set[str]:
    """Handles of participants currently in a (still existing) channel.

    The DB is the source of truth: leaving, reaping and kicking detach the
    key row (``channel_id = NULL``), and a deleted channel's dangling rows
    no longer count. Plain-handle participants block their name too.
    """
    rows = db.execute(
        select(ParticipantKeyDB.handle)
        .join(ChannelDB, ChannelDB.channel_id == ParticipantKeyDB.channel_id)
        .distinct()
    )
    return {handle for (handle,) in rows}


def _tier(suffix: int) -> list[str]:
    return list(NAMES) if suffix == 1 else [f"{name} {suffix}" for name in NAMES]


def _allocate_handle(existing: list[SessionIdentityDB], db: Session) -> str:
    """Pick a name for a new session, reclaiming one from a dormant session.

    Tiers are tried in order (plain names, then " 2", " 3", ...). Within the
    first tier with a name that isn't busy, a name no session holds wins;
    otherwise the dormant session that was least recently in a channel
    loses its identity row, and with it the name.
    """
    busy = _busy_handles(db)
    held = {item.handle: item for item in existing}
    suffix = 1
    while True:
        free = [name for name in _tier(suffix) if name not in busy]
        if free:
            break
        suffix += 1
    unheld = [name for name in free if name not in held]
    if unheld:
        return choice(unheld)

    last_seen = dict(
        db.execute(
            select(ParticipantKeyDB.handle, func.max(ParticipantKeyDB.created_at))
            .where(ParticipantKeyDB.handle.in_(free))
            .group_by(ParticipantKeyDB.handle)
        ).all()
    )
    dormant = min(
        (held[name] for name in free),
        key=lambda item: _naive(last_seen.get(item.handle) or item.created_at),
    )
    existing.remove(dormant)
    db.delete(dormant)
    db.flush()  # Free the UNIQUE handle before the new row is inserted.
    return dormant.handle


def _pin(session_id: str, agent: str, handle: str, db: Session) -> SessionIdentityDB:
    """Give this session the chosen ``handle``, taking it from whoever held it.

    A pinned handle (``AGENT_PTT_HANDLE`` in the plugins) is how a named agent
    stays addressable across sessions and machines: the new session inherits
    the old session's name and voice, so it reuses the old channel participant.
    """
    identity = db.get(SessionIdentityDB, (session_id, agent))
    if identity is not None and identity.handle == handle:
        return identity
    holder = db.scalars(select(SessionIdentityDB).filter_by(handle=handle)).first()
    voice_id = holder.voice_id if holder is not None else None
    if voice_id is None and identity is not None:
        voice_id = identity.voice_id
    for row in (holder, identity):
        if row is not None:
            db.delete(row)
    db.flush()  # Free the UNIQUE handle and the primary key before re-inserting.
    if voice_id is None or not get_voice_profile(voice_id, db):
        voice_id = get_or_create_pinned_voice(handle, db).voice_id
    pinned = SessionIdentityDB(session_id=session_id, agent=agent, handle=handle, voice_id=voice_id)
    db.add(pinned)
    db.commit()
    return pinned


def get_session_identity(
    session_id: str, agent: str, db: Session, pinned_handle: str | None = None
) -> SessionIdentityDB:
    """Allocate once; reuse the pair on every join, including after a restart.

    A session keeps its name while its identity row exists. Names of
    sessions that are in no channel may be reclaimed by new sessions, so
    the pool of plain names doesn't run out over time. ``pinned_handle``
    makes the session take that name instead (see ``_pin``).
    """
    with _identity_lock:
        if pinned_handle:
            return _pin(session_id, agent, pinned_handle, db)
        identity = db.get(SessionIdentityDB, (session_id, agent))
        if identity and get_voice_profile(identity.voice_id, db):
            return identity

        existing = list(db.scalars(select(SessionIdentityDB)))
        if identity is None:
            identity = SessionIdentityDB(
                session_id=session_id, agent=agent, handle=_allocate_handle(existing, db)
            )
        else:
            existing.remove(identity)

        profiles = [p for p in list_voice_profiles(db) if has_backend(p.engine)]
        # Prefer the deliberately created library over old auto-generated handles.
        curated = [p for p in profiles if not p.voice_id.startswith("auto-")]
        pool = curated or profiles
        if pool:
            # Spread voices over the sessions people hear now (in a channel),
            # then over every remembered session.
            busy = _busy_handles(db)
            active = Counter(item.voice_id for item in existing if item.handle in busy)
            total = Counter(item.voice_id for item in existing)

            def usage(voice_id: str) -> tuple[int, int]:
                return active[voice_id], total[voice_id]

            lowest = min(usage(p.voice_id) for p in pool)
            identity.voice_id = choice([p for p in pool if usage(p.voice_id) == lowest]).voice_id
        else:
            # Fresh installations still work before a voice library is created.
            identity.voice_id = get_or_create_pinned_voice(identity.handle, db).voice_id
        db.add(identity)
        db.commit()
        return identity
