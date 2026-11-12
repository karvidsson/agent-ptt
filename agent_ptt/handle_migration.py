"""One-time migration: build ``handle_claims`` from existing data, fix duplicates.

Runs from ``init_db()`` while the registry is empty (so once per database):

1. Every session identity becomes a claim (owner ``session:<agent>:<id>``).
2. Project members seen in the last 24 h join their session's claim when the
   session's mention receiver is in the member's project channel (or the
   session has no receiver and the harness matches); the rest are grouped by
   harness + machine into ``host:`` owners.
3. Where a name has several owners, one keeps it: the preferred owner in
   ``KEEPS`` (approved decision: the Cursor agent on the Mac keeps "Theo"),
   else the most recently seen. Every other owner seen in the last 24 h gets
   the next free name: its identity, member rows and channel participants are
   renamed, and a notice is posted in the affected channels. Owners not seen
   for 24 h are left alone; their names come back under the TTL rule.

New rows and in-place renames only; nothing is dropped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent_ptt import handles
from agent_ptt.models import (
    ChannelDB,
    HandleClaimDB,
    MentionReceiverDB,
    Message,
    MessageDB,
    ParticipantKeyDB,
    ProjectDB,
    ProjectMemberDB,
    SessionIdentityDB,
)

logger = logging.getLogger(__name__)

# handle_ci -> (harness, machine) of the owner that keeps the name.
KEEPS = {"theo": ("cursor", "mac")}
RECENT = timedelta(hours=24)
NOTICE_HANDLE = "agent-ptt"
NOTICE_SENDER = "project-server"
_MIN = datetime.min.replace(tzinfo=UTC)


@dataclass
class Candidate:
    owner_id: str
    handle: str
    harness: str | None = None
    machine: str | None = None
    session_id: str | None = None
    agent: str | None = None
    identity: SessionIdentityDB | None = None
    recipient: str | None = None
    channels: set[str] = field(default_factory=set)  # mention receiver channels
    members: list[ProjectMemberDB] = field(default_factory=list)
    last_seen: datetime = _MIN

    def seen(self, when: datetime | None) -> None:
        when = handles.as_utc(when)
        if when and when > self.last_seen:
            self.last_seen = when

    @property
    def label(self) -> str:
        return f"{(self.harness or '?').lower()}/{self.machine or '?'}"


@dataclass
class Rename:
    old: str
    new: str
    owner: str
    kept_by: str
    channels: list[str]
    members: list[str]


def _post_notice(db: Session, channel_id: str, text: str) -> None:
    from agent_ptt import channel as ch

    msg = Message(
        channel_id=channel_id,
        sender_key=NOTICE_SENDER,
        handle=NOTICE_HANDLE,
        text=text,
        kind="notice",
    )
    db.add(
        MessageDB(
            message_id=msg.message_id,
            channel_id=channel_id,
            sender_key=msg.sender_key,
            handle=msg.handle,
            text=msg.text,
            kind=msg.kind,
            timestamp=msg.timestamp,
        )
    )
    live = ch.get_channel(channel_id)
    if live is not None:
        live.messages.append(msg)


def _candidates(db: Session, now: datetime) -> dict[str, list[Candidate]]:
    receivers: dict[str, set[str]] = {}
    receiver_keys: dict[str, str] = {}
    for row in db.scalars(select(MentionReceiverDB)):
        receivers.setdefault(row.recipient_id, set()).add(row.channel_id)
        receiver_keys[row.key_id] = row.recipient_id
    key_seen: dict[str, datetime] = {}
    for key in db.scalars(select(ParticipantKeyDB)):
        recipient = receiver_keys.get(key.key_id)
        when = handles.as_utc(key.created_at)
        if recipient and when and when > key_seen.get(recipient, _MIN):
            key_seen[recipient] = when

    by_name: dict[str, list[Candidate]] = {}
    for identity in db.scalars(select(SessionIdentityDB)):
        recipient = handles.recipient_id(identity.agent, identity.session_id)
        candidate = Candidate(
            owner_id=handles.session_owner(identity.agent, identity.session_id),
            handle=identity.handle,
            harness=identity.agent,
            session_id=identity.session_id,
            agent=identity.agent,
            identity=identity,
            recipient=recipient,
            channels=receivers.get(recipient, set()),
        )
        candidate.seen(identity.created_at)
        candidate.seen(key_seen.get(recipient))
        by_name.setdefault(handles.ci(identity.handle), []).append(candidate)

    channel_of = {p.project_id: p.channel_id for p in db.scalars(select(ProjectDB))}
    since = (now - RECENT).replace(tzinfo=None)  # SQLite stores naive UTC
    members = db.scalars(select(ProjectMemberDB).where(ProjectMemberDB.last_seen >= since))
    for member in sorted(members, key=lambda m: handles.as_utc(m.last_seen) or _MIN):
        name = handles.ci(member.handle)
        group = by_name.setdefault(name, [])
        sessions = [c for c in group if c.identity and handles.same(c.harness, member.harness)]
        channel = channel_of.get(member.project_id)
        linked = [c for c in sessions if channel and channel in c.channels]
        if not linked:
            linked = [c for c in sessions if not c.channels][:1]
        if linked:
            candidate = linked[0]
        else:
            owner_id = handles.host_owner(member.harness, member.machine)
            candidate = next((c for c in group if c.owner_id == owner_id), None)
            if candidate is None:
                candidate = Candidate(
                    owner_id=owner_id,
                    handle=member.handle,
                    harness=member.harness,
                    machine=member.machine,
                )
                group.append(candidate)
        candidate.members.append(member)
        candidate.machine = candidate.machine or member.machine
        candidate.seen(member.last_seen)
    return by_name


def _winner(name: str, group: list[Candidate]) -> Candidate:
    keep = KEEPS.get(name)
    if keep:
        harness, machine = keep
        preferred = [
            c
            for c in group
            if handles.same(c.harness, harness) and handles.same(c.machine, machine)
        ]
        if preferred:
            return max(preferred, key=lambda c: c.last_seen)
    return max(group, key=lambda c: c.last_seen)


def _claim(db: Session, handle: str, candidate: Candidate) -> HandleClaimDB:
    claim = handles.take(
        db,
        handle,
        handles.Owner(
            candidate.owner_id,
            harness=candidate.harness,
            machine=candidate.machine,
            session_id=candidate.session_id,
        ),
    )
    claim.last_seen = candidate.last_seen if candidate.last_seen > _MIN else handles.now_utc()
    claim.claimed_at = claim.last_seen
    return claim


def migrate(db: Session, now: datetime | None = None) -> list[Rename]:
    """Build claims and rename duplicates; a no-op once any claim exists."""
    if db.scalar(select(func.count()).select_from(HandleClaimDB)):
        return []
    now = now or handles.now_utc()
    by_name = _candidates(db, now)
    if not by_name:
        return []

    losers: list[tuple[str, Candidate, Candidate]] = []
    for name, group in by_name.items():
        winner = _winner(name, group)
        _claim(db, winner.handle, winner)
        for candidate in group:
            if candidate is not winner and candidate.last_seen >= now - RECENT:
                losers.append((name, winner, candidate))
    db.flush()

    renames: list[Rename] = []
    for name, winner, loser in losers:
        old = loser.handle
        new = handles.allocate(db, now)
        for stale in db.scalars(select(SessionIdentityDB)):
            if handles.ci(stale.handle) == handles.ci(new):
                db.delete(stale)
        db.flush()
        if loser.identity is not None:
            loser.identity.handle = new
        for member in loser.members:
            member.handle = new
        others = {c.recipient for c in by_name[name] if c.recipient and c is not loser}
        if loser.recipient:
            channels = handles.rename_participants(db, old, new, recipients={loser.recipient})
        else:
            member_channels = {
                p.channel_id
                for p in db.scalars(
                    select(ProjectDB).where(
                        ProjectDB.project_id.in_([m.project_id for m in loser.members])
                    )
                )
                if p.channel_id
            }
            channels = handles.rename_participants(
                db, old, new, channel_ids=member_channels, exclude_recipients=others
            )
        for member in loser.members:
            project = db.get(ProjectDB, member.project_id)
            if project is not None and project.channel_id:
                channels.add(project.channel_id)
        _claim(db, new, loser)
        text = (
            f"@{winner.handle} ({winner.label}) keeps {old}; "
            f"@{new} is now {loser.label} (was {old})"
        )
        channels = {cid for cid in channels if db.get(ChannelDB, cid) is not None}
        for channel_id in sorted(channels):
            _post_notice(db, channel_id, text)
        logger.warning("Handle registry migration: %s", text)
        renames.append(
            Rename(
                old=old,
                new=new,
                owner=loser.label,
                kept_by=winner.label,
                channels=sorted(channels),
                members=[m.member_id for m in loser.members],
            )
        )
    db.commit()
    return renames
