"""Persistent names and voices for agent sessions, on the handle registry.

A session (``agent`` + ``session_id``) gets its name from ``handles.py``:
its own claim, else its predecessor's (restart stickiness), else a free
name. ``AGENT_PTT_HANDLE`` pins (``pinned_handle``) are owned by harness
plus machine. Voices are chosen here, spread over the sessions people hear.
"""

from collections import Counter
from datetime import timedelta
from secrets import choice

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_ptt import handles
from agent_ptt.handles import HandleTaken, Owner
from agent_ptt.models import HandleClaimDB, SessionIdentityDB
from agent_ptt.tts import has_backend
from agent_ptt.voicedesign import get_or_create_pinned_voice
from agent_ptt.voices import get_voice_profile, list_voice_profiles

__all__ = ["NAMES", "HandleTaken", "get_session_identity"]

NAMES = (
    # The first 40 keep their order (older names stay where they were).
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
    "Bruno",
    "Dante",
    "Elsa",
    "Gus",
    "Hank",
    "Ines",
    "Jonah",
    "Kai",
    "Mira",
    "Pia",
    "Opal",
    "Quinn",
    "Rosa",
    "Aldo",
    "Tess",
    "Uma",
    "Vera",
    "Wade",
    "Gil",
    "Zane",
    "Bea",
    "Cyrus",
    "Nadia",
    "Edgar",
    "Flora",
    "Greta",
    "Hector",
    "Ingrid",
    "Jude",
    "Bram",
    "Lars",
    "Hilda",
    "Odin",
    "Pablo",
    "Rhea",
    "Sven",
    "Tara",
    "Vince",
    "Wanda",
    "Xavier",
    "Yuri",
    "Basil",
    "Cora",
    "Dex",
    "Enzo",
    "Gwen",
    "Ike",
    "Joel",
    "Kurt",
    "Lily",
    "Moss",
    "Noel",
    "Reza",
    "Stella",
    "Toby",
    "Vito",
    "Alma",
    "Boris",
    "Cass",
    "Igor",
)


def _identities_named(db: Session, handle: str) -> list[SessionIdentityDB]:
    wanted = handles.ci(handle)
    rows = db.scalars(select(SessionIdentityDB))
    return [row for row in rows if handles.ci(row.handle) == wanted]


def _choose_voice(identity: SessionIdentityDB, db: Session) -> None:
    others = [
        item
        for item in db.scalars(select(SessionIdentityDB))
        if (item.session_id, item.agent) != (identity.session_id, identity.agent)
    ]
    # A voice chosen in the UI (``user-<handle>``) belongs to its handle only.
    profiles = [
        p
        for p in list_voice_profiles(db)
        if has_backend(p.engine) and not p.voice_id.startswith("user-")
    ]
    # Prefer the deliberately created library over old auto-generated handles.
    curated = [p for p in profiles if not p.voice_id.startswith("auto-")]
    pool = curated or profiles
    if pool:
        # Spread voices over the sessions people hear now (in a channel),
        # then over every remembered session.
        busy = handles.busy_participant_handles(db)
        active = Counter(item.voice_id for item in others if handles.ci(item.handle) in busy)
        total = Counter(item.voice_id for item in others)

        def usage(voice_id: str) -> tuple[int, int]:
            return active[voice_id], total[voice_id]

        lowest = min(usage(p.voice_id) for p in pool)
        identity.voice_id = choice([p for p in pool if usage(p.voice_id) == lowest]).voice_id
    else:
        # Fresh installations still work before a voice library is created.
        identity.voice_id = get_or_create_pinned_voice(identity.handle, db).voice_id


def _become(
    db: Session,
    session_id: str,
    agent: str,
    handle: str,
    current: SessionIdentityDB | None,
    inherit_voice: bool = True,
) -> SessionIdentityDB:
    """Make this session's identity carry ``handle``; with ``inherit_voice`` it
    keeps the voice of whichever session held the name (a restart sounds the same).
    """
    holders = [
        row
        for row in _identities_named(db, handle)
        if (row.session_id, row.agent) != (session_id, agent)
    ]
    voice_id = next(
        (row.voice_id for row in holders if inherit_voice and get_voice_profile(row.voice_id, db)),
        None,
    )
    if voice_id is None and current is not None and get_voice_profile(current.voice_id, db):
        voice_id = current.voice_id
    for row in holders:
        db.delete(row)
    if current is not None:
        db.delete(current)
    db.flush()  # Free the UNIQUE handle and the primary key before re-inserting.
    identity = SessionIdentityDB(session_id=session_id, agent=agent, handle=handle)
    if voice_id is not None:
        identity.voice_id = voice_id
    else:
        _choose_voice(identity, db)
    db.add(identity)
    return identity


def _drop_other_claims(db: Session, owner_id: str, keep: str) -> None:
    for claim in handles.claims_of(db, owner_id):
        if claim.handle_ci != handles.ci(keep):
            db.delete(claim)


def _predecessor(db: Session, owner: Owner, agent: str, now) -> HandleClaimDB | None:
    """Restart stickiness: the newest live claim of a quiet session of the same
    harness, machine and folder (quiet: no sign of life for 10 minutes)."""
    if not (owner.machine and owner.folder):
        return None
    quiet_since = now - timedelta(seconds=handles.STICKY_SECONDS)
    candidates = [
        claim
        for claim in db.scalars(select(HandleClaimDB).where(HandleClaimDB.session_id.is_not(None)))
        if claim.owner_id != owner.owner_id
        and handles.same(claim.harness, agent)
        and handles.same(claim.machine, owner.machine)
        and claim.folder == owner.folder
        and handles.is_live(claim, now)
        and handles.last_seen(claim) < quiet_since
    ]
    return max(candidates, key=handles.last_seen, default=None)


def _pin(
    db: Session,
    session_id: str,
    agent: str,
    handle: str,
    owner: Owner,
    identity: SessionIdentityDB | None,
    now,
) -> SessionIdentityDB:
    """Take the pinned ``handle``: free, ours, or held by the same harness on the
    same machine (a pinned holder, or one quiet for 10 minutes). Otherwise 409."""
    claim = handles.get_claim(db, handle)
    if claim is not None and claim.owner_id != owner.owner_id and handles.is_live(claim, now):
        quiet = handles.last_seen(claim) < now - timedelta(seconds=handles.STICKY_SECONDS)
        # Older plugins send no machine: two unknown machines count as the same.
        same_machine = handles.same(claim.machine, owner.machine) or not (
            claim.machine or owner.machine
        )
        mine = handles.same(claim.harness, agent) and same_machine
        if not (mine and (claim.pinned or quiet)):
            raise HandleTaken(claim)
    if identity is None or identity.handle != handle:
        identity = _become(db, session_id, agent, handle, identity)
    handles.take(db, handle, owner, pinned=True, now=now)
    _drop_other_claims(db, owner.owner_id, handle)
    return identity


def get_session_identity(
    session_id: str,
    agent: str,
    db: Session,
    pinned_handle: str | None = None,
    machine: str | None = None,
    folder: str | None = None,
    preferred_handle: str | None = None,
) -> SessionIdentityDB:
    """This session's name and voice; allocated once, kept while it shows life.

    Order: ``pinned_handle`` (raises ``HandleTaken`` if another harness or
    machine holds it), the session's own claim, its quiet predecessor's
    claim (same harness, ``machine`` and ``folder``), ``preferred_handle``
    if free (project join), then a free name from the pool.
    """
    with handles.lock:
        handles.flush(db)
        now = handles.now_utc()
        owner = Owner(
            handles.session_owner(agent, session_id),
            harness=agent,
            machine=machine,
            folder=folder,
            session_id=session_id,
        )
        identity = db.get(SessionIdentityDB, (session_id, agent))
        if pinned_handle:
            identity = _pin(db, session_id, agent, pinned_handle.strip(), owner, identity, now)
            db.commit()
            return identity

        if identity is not None:
            claim = handles.get_claim(db, identity.handle)
            if claim is not None and claim.owner_id == owner.owner_id:
                handles.refresh(claim, owner, now)
            elif claim is None or not handles.is_live(claim, now):
                handles.take(db, identity.handle, owner, now=now)
            else:  # Someone else holds it now (released and retaken): start over.
                db.delete(identity)
                db.flush()
                identity = None
            if identity is not None:
                if not get_voice_profile(identity.voice_id, db):
                    _choose_voice(identity, db)
                db.commit()
                return identity

        inherited = _predecessor(db, owner, agent, now)
        if inherited is not None:
            handle = inherited.handle
            pinned = bool(inherited.pinned)
        else:
            pinned = False
            handle = None
            if preferred_handle and preferred_handle.strip():
                wanted = handles.get_claim(db, preferred_handle)
                if not handles.is_live(wanted, now):
                    handle = preferred_handle.strip()
            handle = handle or handles.allocate(db, now)
        identity = _become(db, session_id, agent, handle, None, inherit_voice=inherited is not None)
        handles.take(db, handle, owner, pinned=pinned, now=now)
        db.commit()
        return identity


def adopt_handle(session_id: str, agent: str, handle: str, db: Session) -> SessionIdentityDB | None:
    """A session rejoining a project member takes that member's name.

    For names claimed without a session (an older plugin's project join, or
    the migration): the member_key proves it is the same agent, so the
    session drops its automatic name for the member's. Returns None (no
    change) when the session's name is pinned or another session holds it.
    """
    with handles.lock:
        handles.flush(db)
        now = handles.now_utc()
        owner_id = handles.session_owner(agent, session_id)
        identity = db.get(SessionIdentityDB, (session_id, agent))
        if identity is None:
            return None
        mine = handles.get_claim(db, identity.handle)
        if mine is not None and mine.owner_id != owner_id:
            mine = None
        if mine is not None and mine.pinned:
            return None
        target = handles.get_claim(db, handle)
        if target is not None and handles.is_live(target, now) and target.session_id:
            return None
        old = identity.handle
        identity = _become(db, session_id, agent, handle.strip(), identity, inherit_voice=False)
        handles.take(
            db,
            handle,
            Owner(
                owner_id,
                harness=agent,
                machine=mine.machine if mine else None,
                folder=mine.folder if mine else None,
                session_id=session_id,
            ),
            now=now,
        )
        if mine is not None:
            db.delete(mine)
        handles.rename_participants(
            db, old, identity.handle, recipients={handles.recipient_id(agent, session_id)}
        )
        db.commit()
        return identity
