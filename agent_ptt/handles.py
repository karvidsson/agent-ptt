"""The server-wide handle registry: one live owner per name (``handle_claims``).

Channel participants, project members and session identities all take their
name from here, so "@Theo" means one agent across every channel and project.

Owners
    ``session:<agent>:<session_id>``  a harness session (the plugins, MCP, the CLI)
    ``host:<harness>@<machine>``      a project member that joined without a session
    ``raw:<handle>``                  a channel join with neither (web UI, old clients)

A claim stays live while its owner shows signs of life (``touch``): joins,
say, WebSocket and inbox activity, ``PATCH members/me``. Automatic names are
freed after ``AGENT_PTT_HANDLE_TTL_HOURS`` (24) without one, pinned names
(``AGENT_PTT_HANDLE``) after ``AGENT_PTT_PINNED_HANDLE_TTL_DAYS`` (30).

Touches only update an in-memory map; ``flush`` writes them to the DB (the
server runs it once a minute and before registry decisions), so a busy
participant costs at most one write per claim per minute.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import choice
from threading import Lock
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_ptt.models import ChannelDB, HandleClaimDB, ParticipantKeyDB

logger = logging.getLogger(__name__)

lock = Lock()  # The channel server runs in one process.

DEFAULT_TTL_HOURS = 24
DEFAULT_PINNED_TTL_DAYS = 30
# A predecessor this long without a sign of life hands its name to a restart.
STICKY_SECONDS = 600
FLUSH_SECONDS = 60

_seen: dict[str, datetime] = {}  # handle_ci -> last sign of life not yet flushed


def reset_runtime_state() -> None:
    """Forget unflushed touches (tests)."""
    _seen.clear()


def ci(handle: str) -> str:
    return handle.strip().casefold()


def session_owner(agent: str, session_id: str) -> str:
    return f"session:{agent}:{session_id}"


def host_owner(harness: str | None, machine: str | None) -> str:
    return f"host:{(harness or '').strip().casefold()}@{(machine or '').strip().casefold()}"


def raw_owner(handle: str) -> str:
    return f"raw:{ci(handle)}"


def now_utc() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _env_number(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        logger.warning("Ignoring %s=%r (not a positive integer); using %d", name, raw, default)
        return default
    return value


def auto_ttl() -> timedelta:
    return timedelta(hours=_env_number("AGENT_PTT_HANDLE_TTL_HOURS", DEFAULT_TTL_HOURS))


def pinned_ttl() -> timedelta:
    return timedelta(days=_env_number("AGENT_PTT_PINNED_HANDLE_TTL_DAYS", DEFAULT_PINNED_TTL_DAYS))


def ttl(claim: HandleClaimDB) -> timedelta:
    return pinned_ttl() if claim.pinned else auto_ttl()


def last_seen(claim: HandleClaimDB) -> datetime:
    stored = as_utc(claim.last_seen) or datetime.min.replace(tzinfo=UTC)
    pending = _seen.get(claim.handle_ci)
    return max(stored, pending) if pending else stored


def expires_at(claim: HandleClaimDB) -> datetime:
    return last_seen(claim) + ttl(claim)


def is_live(claim: HandleClaimDB | None, now: datetime | None = None) -> bool:
    return claim is not None and expires_at(claim) > (now or now_utc())


def same(a: str | None, b: str | None) -> bool:
    """Case-insensitive match of two known values (unknown never matches)."""
    return bool(a and b) and a.strip().casefold() == b.strip().casefold()


# ---------------------------------------------------------------------------
# Signs of life
# ---------------------------------------------------------------------------


def touch(handle: str | None) -> None:
    """Record a sign of life for whoever owns ``handle``; memory only, see ``flush``."""
    if handle:
        _seen[ci(handle)] = now_utc()


def flush(db: Session) -> int:
    """Write pending touches to ``handle_claims``; returns the rows changed."""
    if not _seen:
        return 0
    pending = dict(_seen)
    changed = 0
    for claim in db.scalars(select(HandleClaimDB).where(HandleClaimDB.handle_ci.in_(pending))):
        when = pending[claim.handle_ci]
        if when > (as_utc(claim.last_seen) or datetime.min.replace(tzinfo=UTC)):
            claim.last_seen = when
            changed += 1
    db.commit()
    for key, when in pending.items():
        if _seen.get(key) == when:
            _seen.pop(key, None)
    return changed


def flush_now() -> None:
    from agent_ptt.db import SessionLocal

    with lock, SessionLocal() as db:
        flush(db)


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


def public_harness(harness: str | None) -> str | None:
    """Harness names as project members show them: ``Claude`` -> ``claude``."""
    return harness.strip().lower() or None if harness else None


class HandleTaken(Exception):
    """A name another owner holds; the API answers 409 ``handle_taken``."""

    def __init__(self, claim: HandleClaimDB) -> None:
        self.handle = claim.handle
        self.harness = public_harness(claim.harness)
        self.machine = claim.machine
        self.last_seen = last_seen(claim)
        super().__init__(self.detail)

    @property
    def detail(self) -> str:
        where = f" on {self.machine}" if self.machine else ""
        return f"{self.handle} is held by {self.harness or 'another agent'}{where}"

    def body(self) -> dict[str, Any]:
        return {
            "error": "handle_taken",
            "handle": self.handle,
            "held_by": {
                "harness": self.harness,
                "machine": self.machine,
                "last_seen": self.last_seen.isoformat(),
            },
            "detail": self.detail,
        }


def get_claim(db: Session, handle: str) -> HandleClaimDB | None:
    return db.get(HandleClaimDB, ci(handle))


def live_claim(db: Session, handle: str, now: datetime | None = None) -> HandleClaimDB | None:
    claim = get_claim(db, handle)
    return claim if is_live(claim, now) else None


@dataclass
class Owner:
    owner_id: str
    harness: str | None = None
    machine: str | None = None
    folder: str | None = None
    session_id: str | None = None


def take(
    db: Session, handle: str, owner: Owner, pinned: bool = False, now: datetime | None = None
) -> HandleClaimDB:
    """Give ``handle`` to ``owner`` (new claim or transfer); the caller commits."""
    now = now or now_utc()
    key = ci(handle)
    claim = db.get(HandleClaimDB, key)
    if claim is None:
        claim = HandleClaimDB(handle_ci=key)
        db.add(claim)
    if claim.owner_id != owner.owner_id:
        claim.claimed_at = now
    claim.handle = handle.strip()
    claim.owner_id = owner.owner_id
    claim.harness = (owner.harness or None) and owner.harness[:64]
    claim.machine = (owner.machine or None) and owner.machine[:100]
    claim.folder = (owner.folder or None) and owner.folder[:500]
    claim.session_id = owner.session_id
    claim.pinned = pinned
    claim.last_seen = now
    _seen.pop(key, None)
    return claim


def refresh(claim: HandleClaimDB, owner: Owner, now: datetime | None = None) -> None:
    """The owner showed up again: fresh ``last_seen``, newer machine/folder."""
    claim.last_seen = now or now_utc()
    _seen.pop(claim.handle_ci, None)
    if owner.machine:
        claim.machine = owner.machine[:100]
    if owner.folder:
        claim.folder = owner.folder[:500]


def release(db: Session, handle: str) -> HandleClaimDB | None:
    """Delete the claim (``DELETE /handles``); the caller commits."""
    claim = get_claim(db, handle)
    if claim is not None:
        db.delete(claim)
        _seen.pop(claim.handle_ci, None)
    return claim


def claims_of(db: Session, owner_id: str) -> list[HandleClaimDB]:
    return list(db.scalars(select(HandleClaimDB).filter_by(owner_id=owner_id)))


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------


def busy_participant_handles(db: Session) -> set[str]:
    """Casefolded handles of participants in a (still existing) channel."""
    rows = db.execute(
        select(ParticipantKeyDB.handle)
        .join(ChannelDB, ChannelDB.channel_id == ParticipantKeyDB.channel_id)
        .distinct()
    )
    return {ci(handle) for (handle,) in rows}


def tier(suffix: int) -> list[str]:
    from agent_ptt.session_identity import NAMES

    # No space ("Pearl2", not "Pearl 2"): a plain @Pearl2 mention needs no quotes.
    return list(NAMES) if suffix == 1 else [f"{name}{suffix}" for name in NAMES]


def allocate(db: Session, now: datetime | None = None, exclude: set[str] = frozenset()) -> str:
    """A free name: plain names first, then ``Name2``, ``Name3``, ….

    Free means no live claim and no participant using it in a channel.
    Within the first tier with a free name, one never claimed wins (random);
    otherwise the name whose claim expired longest ago.
    """
    now = now or now_utc()
    claims = {claim.handle_ci: claim for claim in db.scalars(select(HandleClaimDB))}
    busy = busy_participant_handles(db) | {ci(name) for name in exclude}
    suffix = 1
    while True:
        free = [
            name
            for name in tier(suffix)
            if ci(name) not in busy and not is_live(claims.get(ci(name)), now)
        ]
        if free:
            break
        suffix += 1
    never = [name for name in free if ci(name) not in claims]
    if never:
        return choice(never)
    return min(free, key=lambda name: expires_at(claims[ci(name)]))


def claim_info(claim: HandleClaimDB | None) -> dict[str, Any] | None:
    """The ``handle_claim`` field of a join response."""
    if claim is None:
        return None
    return {
        "handle": claim.handle,
        "pinned": bool(claim.pinned),
        "expires_after_hours": int(ttl(claim).total_seconds() // 3600),
    }


# ---------------------------------------------------------------------------
# Raw joins (no session): the name must be free or already this owner's
# ---------------------------------------------------------------------------


_UNKNOWN_HARNESS = {"", "cli"}


def sessionless_match(claim: HandleClaimDB, harness: str | None, machine: str | None) -> bool:
    """Whether a session-less client may share ``claim`` with its session-less holder.

    Clients without a session (the web UI, the plain CLI, an old project
    join) can't be told apart, so they only conflict when harness or machine
    are known on both sides and differ. A session's claim never matches.
    """
    if claim.session_id:
        return False
    if claim.machine and machine and not same(claim.machine, machine):
        return False
    known = (claim.harness or "").strip().casefold(), (harness or "").strip().casefold()
    return known[0] in _UNKNOWN_HARNESS or known[1] in _UNKNOWN_HARNESS or known[0] == known[1]


def claim_raw(db: Session, handle: str, owner: Owner, compatible=None) -> HandleClaimDB:
    """Claim ``handle`` for a session-less owner, or raise ``HandleTaken``.

    ``compatible(claim)`` may accept a live claim of another owner that is
    plausibly the same agent (see projects.join_project). The caller commits.
    """
    with lock:
        flush(db)
        now = now_utc()
        claim = get_claim(db, handle)
        if claim is not None and is_live(claim, now) and claim.owner_id != owner.owner_id:
            if compatible is None or not compatible(claim):
                raise HandleTaken(claim)
            refresh(claim, Owner(claim.owner_id), now)
            return claim
        if claim is not None and claim.owner_id == owner.owner_id:
            refresh(claim, owner, now)
            return claim
        return take(db, handle, owner, now=now)


# ---------------------------------------------------------------------------
# GET /handles
# ---------------------------------------------------------------------------


def list_claims(db: Session) -> list[dict[str, Any]]:
    from agent_ptt import channel as ch
    from agent_ptt.models import ProjectDB, ProjectMemberDB

    flush(db)
    now = now_utc()
    channels: dict[str, set[str]] = {}
    for live in ch.list_channels():
        for participant in live.participants.values():
            channels.setdefault(ci(participant.handle), set()).add(live.name)
    projects: dict[str, set[str]] = {}
    names = {p.project_id: p.name for p in db.scalars(select(ProjectDB))}
    for member in db.scalars(select(ProjectMemberDB)):
        if member.project_id in names:
            projects.setdefault(ci(member.handle), set()).add(names[member.project_id])
    out = []
    claims = sorted(db.scalars(select(HandleClaimDB)), key=lambda c: c.handle_ci)
    for claim in claims:
        if not is_live(claim, now):
            continue
        out.append(
            {
                "handle": claim.handle,
                "harness": public_harness(claim.harness),
                "machine": claim.machine,
                "pinned": bool(claim.pinned),
                "last_seen": last_seen(claim),
                "expires_at": expires_at(claim),
                "channels": sorted(channels.get(claim.handle_ci, ())),
                "projects": sorted(projects.get(claim.handle_ci, ())),
            }
        )
    return out


def release_handle(db: Session, handle: str) -> HandleClaimDB | None:
    """``DELETE /handles/{handle}``: drop the claim and its session's identity.

    The former holder gets a new automatic name at its next join; anyone may
    claim the released name at once.
    """
    from agent_ptt.models import SessionIdentityDB

    with lock:
        claim = release(db, handle)
        if claim is None:
            return None
        for identity in db.scalars(select(SessionIdentityDB)):
            if ci(identity.handle) == claim.handle_ci:
                db.delete(identity)
        db.commit()
        return claim


# ---------------------------------------------------------------------------
# Renames (adoption, migration)
# ---------------------------------------------------------------------------


def recipient_id(agent: str, session_id: str) -> str:
    """The mention recipient ID of a session (as mentions.register_receiver)."""
    import json
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([agent, session_id])))


def rename_participants(
    db: Session,
    old: str,
    new: str,
    recipients: set[str] | None = None,
    channel_ids: set[str] | None = None,
    exclude_recipients: set[str] | frozenset[str] = frozenset(),
) -> set[str]:
    """Rename the participants called ``old`` (in DB and memory); returns channel IDs.

    ``recipients`` keeps it to the keys registered for those mention
    recipients, ``channel_ids`` to those channels; keys registered for
    ``exclude_recipients`` are never touched. The caller commits.
    """
    from agent_ptt import channel as ch
    from agent_ptt.models import MentionReceiverDB

    receivers = {row.key_id: row.recipient_id for row in db.scalars(select(MentionReceiverDB))}

    def wanted(key_id: str, channel_id: str | None) -> bool:
        recipient = receivers.get(key_id)
        if recipient in exclude_recipients:
            return False
        if recipients is not None and recipient not in recipients:
            return False
        return channel_ids is None or channel_id in channel_ids

    touched: set[str] = set()
    for row in db.scalars(select(ParticipantKeyDB).filter_by(handle=old)):
        if row.channel_id and wanted(row.key_id, row.channel_id):
            row.handle = new
            touched.add(row.channel_id)
    for live in ch.list_channels(include_archived=True):
        for participant in live.participants.values():
            if participant.handle == old and wanted(participant.key_id, live.channel_id):
                participant.handle = new
                touched.add(live.channel_id)
    return touched
