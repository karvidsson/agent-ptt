"""Channel manager — create, join, send, leave.

Channels live in memory while the server runs, and are mirrored to the
database so a restart doesn't take the room down with it: on startup the
server restores every channel, its participants and its transcript, and
the keys agents are already holding keep working.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from agent_ptt.models import (
    ArchivedChannelDB,
    Channel,
    ChannelDB,
    MentionReceiverDB,
    Message,
    MessageContext,
    MessageDB,
    MessageDeliveryDB,
    MessageKind,
    ParticipantKey,
    ParticipantKeyDB,
    RevokedParticipantDB,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory channel registry
# ---------------------------------------------------------------------------

_channels: dict[str, Channel] = {}
_message_queues: dict[str, asyncio.Queue[Message]] = {}


# ---------------------------------------------------------------------------
# Channel lifecycle
# ---------------------------------------------------------------------------


def create_channel(name: str, db: Session | None = None) -> Channel:
    """Create a new named channel, persisting it if a session is provided."""
    channel = Channel(name=name)
    _channels[channel.channel_id] = channel
    _message_queues[channel.channel_id] = asyncio.Queue()

    if db is not None:
        db.merge(
            ChannelDB(
                channel_id=channel.channel_id,
                name=channel.name,
                created_at=channel.created_at,
            )
        )
        db.commit()

    return channel


def list_channels(include_archived: bool = False) -> list[Channel]:
    """List all active channels."""
    return [c for c in _channels.values() if include_archived or not c.archived]


def get_channel(channel_id: str) -> Channel | None:
    """Get a channel by ID."""
    return _channels.get(channel_id)


def delete_channel(channel_id: str, db: Session | None = None) -> bool:
    """Delete a channel. Returns True if it existed."""
    removed = _channels.pop(channel_id, None)
    _message_queues.pop(channel_id, None)

    if db is not None:
        db.query(ArchivedChannelDB).filter_by(channel_id=channel_id).delete()
        db.query(MessageDeliveryDB).filter_by(channel_id=channel_id).delete()
        db.query(MentionReceiverDB).filter_by(channel_id=channel_id).delete()
        row = db.get(ChannelDB, channel_id)
        if row is not None:
            db.delete(row)
        db.commit()

    return removed is not None


def restore_channels(db: Session) -> list[Channel]:
    """Rebuild the in-memory registry from the database.

    Called once on startup. Participants are restored with the keys they
    were issued, so an agent that joined before the restart can carry on
    posting without noticing it happened. They come back ``offline`` until
    they show a sign of life (see ``mark_seen``); one that doesn't within
    the idle grace period is reaped.
    """
    restored: list[Channel] = []
    now = datetime.now(UTC)

    for row in db.query(ChannelDB).order_by(ChannelDB.created_at).all():
        if row.channel_id in _channels:
            continue

        channel = Channel(
            channel_id=row.channel_id,
            name=row.name,
            archived=db.get(ArchivedChannelDB, row.channel_id) is not None,
            created_at=_as_utc(row.created_at),
        )

        for key_row in (
            db.query(ParticipantKeyDB).filter(ParticipantKeyDB.channel_id == row.channel_id).all()
        ):
            channel.participants[key_row.key_id] = ParticipantKey(
                key_id=key_row.key_id,
                handle=key_row.handle,
                voice_id=key_row.voice_id,
                channel_id=key_row.channel_id,
                created_at=_as_utc(key_row.created_at),
                state="offline",
                since=now,
                last_seen=now,
            )

        for msg_row in (
            db.query(MessageDB)
            .filter(MessageDB.channel_id == row.channel_id)
            .order_by(MessageDB.timestamp)
            .all()
        ):
            channel.messages.append(
                Message(
                    message_id=msg_row.message_id,
                    channel_id=msg_row.channel_id,
                    sender_key=msg_row.sender_key,
                    handle=msg_row.handle,
                    text=msg_row.text,
                    kind=msg_row.kind or "message",
                    timestamp=_as_utc(msg_row.timestamp),
                    context=msg_row.context,
                )
            )

        for participant in channel.participants.values():
            last = last_message_from(channel, participant.key_id)
            participant.last_active = last.timestamp if last else participant.created_at

        for receiver in db.query(MentionReceiverDB).filter_by(channel_id=row.channel_id):
            participant = channel.participants.get(receiver.key_id)
            if participant:
                participant.mention_id = receiver.recipient_id
        from agent_ptt.mentions import attach_receipts

        attach_receipts(channel.messages, channel.channel_id, db)
        _channels[channel.channel_id] = channel
        if not channel.archived:
            _message_queues[channel.channel_id] = asyncio.Queue()
            restored.append(channel)

    return restored


def _as_utc(value: datetime | None) -> datetime:
    """SQLite hands back naive datetimes; they were always UTC."""
    if value is None:
        return datetime.now(UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Participant lifecycle
# ---------------------------------------------------------------------------


def join_channel(
    channel_id: str,
    handle: str,
    voice_id: str | None = None,
    db: Session | None = None,
) -> ParticipantKey | None:
    """Join a channel and receive a participation key.

    The key is also persisted to the database if a session is provided.
    """
    channel = _channels.get(channel_id)
    if channel is None or channel.archived:
        return None

    key = ParticipantKey(
        handle=handle,
        voice_id=voice_id,
        channel_id=channel_id,
    )
    channel.participants[key.key_id] = key

    # Persist to DB
    if db is not None:
        db_key = ParticipantKeyDB(
            key_id=key.key_id,
            handle=key.handle,
            voice_id=key.voice_id,
            channel_id=key.channel_id,
            created_at=key.created_at,
        )
        db.merge(db_key)
        db.commit()

    return key


def mark_seen(participant: ParticipantKey) -> None:
    """Record a sign of life; an ``offline`` participant comes back ``active``.

    Polling and heartbeats call only this; saying or doing something also
    moves ``last_active`` (see ``_touch``).
    """
    now = datetime.now(UTC)
    participant.last_seen = now
    if participant.state == "offline" or (participant.state == "away" and participant.idle_away):
        participant.state = "active"
        participant.away_reason = None
        participant.idle_away = False
        participant.since = now


DEFAULT_IDLE_REAP_SECONDS = 600


def idle_reap_seconds() -> int:
    """Grace period before an idle participant is removed; 0 turns reaping off.

    Read from ``AGENT_PTT_IDLE_REAP_SECONDS`` (default 600). A value that
    isn't a non-negative integer falls back to the default with a warning.
    """
    raw = os.environ.get("AGENT_PTT_IDLE_REAP_SECONDS", "").strip()
    if not raw:
        return DEFAULT_IDLE_REAP_SECONDS
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        logger.warning(
            "Ignoring AGENT_PTT_IDLE_REAP_SECONDS=%r (not a non-negative integer); using %d",
            raw,
            DEFAULT_IDLE_REAP_SECONDS,
        )
        return DEFAULT_IDLE_REAP_SECONDS
    return value


def idle_reap_message(grace: int) -> str:
    """One-line description of the idle reaper, logged at server startup."""
    if grace <= 0:
        return "Idle participant reaping: off (AGENT_PTT_IDLE_REAP_SECONDS=0)"
    return (
        f"Idle participant reaping: after {grace}s without a sign of life "
        "(AGENT_PTT_IDLE_REAP_SECONDS, 0 = off)"
    )


DEFAULT_AWAY_REAP_SECONDS = 604800


def away_reap_seconds() -> int:
    """How long a hooked participant may stay away before it is removed; 0 = never.

    Read from ``AGENT_PTT_AWAY_REAP_SECONDS`` (default 604800, 7 days); counted
    on top of the idle grace. A bad value falls back to the default.
    """
    raw = os.environ.get("AGENT_PTT_AWAY_REAP_SECONDS", "").strip()
    if not raw:
        return DEFAULT_AWAY_REAP_SECONDS
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        logger.warning(
            "Ignoring AGENT_PTT_AWAY_REAP_SECONDS=%r (not a non-negative integer); using %d",
            raw,
            DEFAULT_AWAY_REAP_SECONDS,
        )
        return DEFAULT_AWAY_REAP_SECONDS
    return value


def idle_participants(
    now: datetime, grace: float, live_keys: Collection[str], away_grace: float | None = None
) -> list[ParticipantKey]:
    """Participants to reap: no live agent WebSocket and unseen for over ``grace`` s.

    A hooked participant (one with a mention receiver) is not reaped at the
    grace (see ``idle_receivers``): only once unseen for ``grace + away_grace``
    s, and never when ``away_grace`` is 0. ``away_grace`` None reads
    ``AGENT_PTT_AWAY_REAP_SECONDS``. Archived channels are skipped (their
    participants are already revoked).
    """
    if away_grace is None:
        away_grace = away_reap_seconds()
    stale: list[ParticipantKey] = []
    for channel in _channels.values():
        if channel.archived:
            continue
        for participant in channel.participants.values():
            if participant.key_id in live_keys:
                continue
            unseen = (now - participant.last_seen).total_seconds()
            if participant.mention_id:
                if away_grace > 0 and unseen > grace + away_grace:
                    stale.append(participant)
            elif unseen > grace:
                stale.append(participant)
    return stale


def idle_receivers(now: datetime, grace: float, live_keys: Collection[str]) -> list[ParticipantKey]:
    """Hooked participants unseen past ``grace`` that are not yet ``away``.

    The reaper marks them away (``idle_away``) instead of removing them: they
    keep their seat and keep receiving mentions in their channel inbox.
    """
    found: list[ParticipantKey] = []
    for channel in _channels.values():
        if channel.archived:
            continue
        for participant in channel.participants.values():
            if (
                participant.mention_id
                and participant.key_id not in live_keys
                and participant.state != "away"
                and (now - participant.last_seen).total_seconds() > grace
            ):
                found.append(participant)
    return found


def mark_idle_away(participant: ParticipantKey, now: datetime | None = None) -> None:
    participant.state = "away"
    participant.away_reason = "idle"
    participant.idle_away = True
    participant.since = now or datetime.now(UTC)


def leave_channel(channel_id: str, key_id: str, db: Session) -> bool:
    """Remove a participant from ``channel_id`` and persist it.

    Returns False if the key is not a participant of that channel. Unlike
    ``revoke_participant`` this records no ban: the key row is detached
    (``channel_id = None``) so ``restore_channels()`` no longer brings the
    participant back, and its mention receiver is dropped so mentions stop
    queuing for it, but an explicit rejoin works as usual.
    """
    channel = _channels.get(channel_id)
    if channel is None or key_id not in channel.participants:
        return False
    row = db.get(ParticipantKeyDB, key_id)
    if row:
        row.channel_id = None
    db.query(MentionReceiverDB).filter_by(key_id=key_id).delete()
    db.commit()
    del channel.participants[key_id]
    return True


def revoke_participant(channel_id: str, key_id: str, db: Session) -> bool:
    channel = get_channel(channel_id)
    if channel is None or key_id not in channel.participants:
        return False
    db.merge(RevokedParticipantDB(key_id=key_id, channel_id=channel_id))
    row = db.get(ParticipantKeyDB, key_id)
    if row:
        row.channel_id = None
    db.query(MentionReceiverDB).filter_by(key_id=key_id).delete()
    db.commit()
    del channel.participants[key_id]
    return True


def set_archived(channel_id: str, archived: bool, db: Session) -> Channel | None:
    channel = get_channel(channel_id)
    if channel is None:
        return None
    if archived:
        for key_id in list(channel.participants):
            revoke_participant(channel_id, key_id, db)
        db.merge(ArchivedChannelDB(channel_id=channel_id))
        _message_queues.pop(channel_id, None)
    else:
        row = db.get(ArchivedChannelDB, channel_id)
        if row:
            db.delete(row)
        _message_queues.setdefault(channel_id, asyncio.Queue())
    db.commit()
    channel.archived = archived
    return channel


def get_participant(key_id: str) -> ParticipantKey | None:
    """Look up a participant across all channels."""
    for channel in _channels.values():
        if key_id in channel.participants:
            return channel.participants[key_id]
    return None


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------


async def send_message(
    key_id: str,
    text: str,
    db: Session | None = None,
    recipient_ids: list[str] | None = None,
    kind: MessageKind = "message",
    context: MessageContext | None = None,
) -> Message | None:
    """Post a text message from a participant.

    Returns the Message if successful, None if the participant isn't found.
    The message is added to channel history, persisted to DB, and queued
    for TTS synthesis. ``kind`` marks ``/me`` actions and ``/notice``
    notices; the TTS worker decides how (and whether) to speak them.
    ``context`` is the optional detail behind the line; it is stored and
    served on request but never spoken or broadcast in full.
    """
    participant = get_participant(key_id)
    if participant is None or participant.channel_id is None:
        return None

    channel = _channels.get(participant.channel_id)
    if channel is None or channel.archived:
        return None

    from agent_ptt import projects as pj
    from agent_ptt.mentions import notify, persist_deliveries, resolve_recipients

    # In a project's default channel, mentions of roles, the project and
    # absent members are queued in the project inbox (see docs/mentions.md).
    project = pj.project_for_channel(db, channel.channel_id) if db is not None else None
    members = pj.project_members(db, project.project_id) if project is not None else None
    recipients, durable = resolve_recipients(channel, key_id, text, recipient_ids, members)
    if (recipients or durable) and len(text) > 8000:
        raise ValueError("Mention messages must be at most 8000 characters")
    msg = Message(
        channel_id=channel.channel_id,
        sender_key=key_id,
        handle=participant.handle,
        text=text,
        kind=kind,
        context=context,
    )

    # Persist to DB
    if db is not None:
        db_msg = MessageDB(
            message_id=msg.message_id,
            channel_id=msg.channel_id,
            sender_key=msg.sender_key,
            handle=msg.handle,
            text=msg.text,
            kind=msg.kind,
            timestamp=msg.timestamp,
            context=context.model_dump(mode="json", exclude_none=True) if context else None,
        )
        db.add(db_msg)
        persist_deliveries(msg, recipients, db)
        db.commit()
        notify(channel.channel_id, recipients)
        if project is not None and durable:
            refs, effects = pj.create_mention_items(
                db,
                project,
                participant.handle,
                msg.message_id,
                channel.channel_id,
                text,
                durable,
            )
            msg._queued_items = refs
            msg._followups = (project.project_id, effects)

    # Publish only after the message and its deliveries are durably committed.
    channel.messages.append(msg)
    mark_seen(participant)
    participant.last_active = msg.timestamp
    queue = _message_queues.get(channel.channel_id)
    if queue is not None:
        await queue.put(msg)

    return msg


async def publish_followups(msg: Message) -> None:
    """Post the notices and wake-ups of a message's durable mentions, once.

    Callers run this after broadcasting the message itself, so listeners see
    the line before the "X → role:reviewer · instruction · …" notice.
    """
    followups, msg._followups = msg._followups, None
    if not followups:
        return
    from agent_ptt import projects as pj
    from agent_ptt.db import SessionLocal

    project_id, effects = followups
    with SessionLocal() as db:
        project = db.get(pj.ProjectDB, project_id)
        if project is not None:
            await pj.publish(db, project, effects)


# The server registers how a message reaches the channel's agent WebSockets
# (server.py owns the socket registry); None until the app is imported.
_broadcaster: Callable[[Message], Awaitable[None]] | None = None


def set_broadcaster(broadcaster: Callable[[Message], Awaitable[None]] | None) -> None:
    """Install the coroutine that broadcasts a message event to a channel."""
    global _broadcaster
    _broadcaster = broadcaster


async def post_notice(
    channel_id: str, handle: str, text: str, sender: str, db: Session
) -> Message | None:
    """Post a server-side, text-only ``notice`` without a joined participant.

    Used by the project inbox (projects.py). ``sender`` is a public, non-secret
    identifier (e.g. ``project-member:<member_id>``), stored as the message's
    sender so ``sender_id`` stays stable per author; never pass a key. The
    notice is appended to history, persisted, queued (the TTS worker skips
    notices) and broadcast like any other message. Returns None when the
    channel is missing or archived.
    """
    channel = _channels.get(channel_id)
    if channel is None or channel.archived:
        return None
    msg = Message(channel_id=channel_id, sender_key=sender, handle=handle, text=text, kind="notice")
    db.add(
        MessageDB(
            message_id=msg.message_id,
            channel_id=msg.channel_id,
            sender_key=msg.sender_key,
            handle=msg.handle,
            text=msg.text,
            kind=msg.kind,
            timestamp=msg.timestamp,
        )
    )
    db.commit()
    channel.messages.append(msg)
    queue = _message_queues.get(channel_id)
    if queue is not None:
        await queue.put(msg)
    if _broadcaster is not None:
        try:
            await _broadcaster(msg)
        except Exception as exc:  # A dead socket must not fail the caller.
            logger.warning("Notice broadcast failed: %s", exc)
    return msg


def get_history(channel_id: str) -> list[Message]:
    """Retrieve conversation history for a channel."""
    channel = _channels.get(channel_id)
    if channel is None:
        return []
    return list(channel.messages)


def get_message(channel_id: str, message_id: str, db: Session | None = None) -> Message | None:
    """Find one message by ID: live history first, then the archive."""
    channel = _channels.get(channel_id)
    if channel is not None:
        for msg in channel.messages:
            if msg.message_id == message_id:
                return msg
    if db is None:
        return None
    row = db.get(MessageDB, message_id)
    if row is None or row.channel_id != channel_id:
        return None
    msg = Message.model_validate(row)
    msg.timestamp = _as_utc(row.timestamp)
    from agent_ptt.mentions import attach_receipts

    attach_receipts([msg], channel_id, db)
    return msg


def context_summary(msg: Message) -> dict[str, Any] | None:
    """The light view of a message's context carried by broadcasts and history.

    Only keys with a value are present: ``files`` (a count), ``branch`` and
    ``repo``. None when the message has no context at all.
    """
    context = msg.context
    if context is None:
        return None
    summary: dict[str, Any] = {}
    if context.files is not None:
        summary["files"] = len(context.files)
    if context.branch is not None:
        summary["branch"] = context.branch
    if context.repo is not None:
        summary["repo"] = context.repo
    return summary


def get_message_queue(channel_id: str) -> asyncio.Queue[Message] | None:
    """Get the TTS message queue for a channel."""
    return _message_queues.get(channel_id)


# ---------------------------------------------------------------------------
# IRC-style commands
# ---------------------------------------------------------------------------


class CommandError(Exception):
    """A command that can't run: unknown name, bad key, missing target."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


COMMANDS = ("names", "whois", "me", "notice", "topic", "away", "back")


def _touch(participant: ParticipantKey) -> None:
    mark_seen(participant)
    participant.last_active = participant.last_seen


def find_participant_by_handle(channel: Channel, handle: str) -> ParticipantKey | None:
    """Case-insensitive prefix match, exact match preferred."""
    needle = handle.strip().lower()
    if not needle:
        return None
    participants = list(channel.participants.values())
    for participant in participants:
        if participant.handle.lower() == needle:
            return participant
    for participant in participants:
        if participant.handle.lower().startswith(needle):
            return participant
    return None


def last_message_from(channel: Channel, key_id: str) -> Message | None:
    for msg in reversed(channel.messages):
        if msg.sender_key == key_id:
            return msg
    return None


async def run_command(
    channel_id: str,
    key_id: str,
    name: str,
    args: str = "",
    db: Session | None = None,
) -> dict[str, Any]:
    """Run an IRC-style command and return its ``result`` dict.

    Raises CommandError(400) for an unknown command, CommandError(404) when
    the key isn't a participant of the channel or a ``whois`` target isn't
    found. Broadcasting to WebSocket clients is the server's job; the
    dispatcher only mutates channel state and posts messages.
    """
    name = (name or "").strip().lower()
    args = (args or "").strip()
    if name not in COMMANDS:
        raise CommandError(400, f"unknown command: {name}")

    channel = _channels.get(channel_id)
    participant = channel.participants.get(key_id) if channel is not None else None
    if channel is None or participant is None:
        raise CommandError(404, "Participant not found in channel")

    _touch(participant)

    if name == "names":
        return {"participants": [p.model_dump(mode="json") for p in channel.participants.values()]}

    if name == "whois":
        target = find_participant_by_handle(channel, args)
        if target is None:
            raise CommandError(404, f"no such participant: {args}")
        last = last_message_from(channel, target.key_id)
        return {
            "participant": target.model_dump(mode="json"),
            "last_message": last.model_dump(mode="json") if last else None,
        }

    if name in ("me", "notice"):
        if not args:
            raise CommandError(400, f"{name} needs some text")
        kind: MessageKind = "action" if name == "me" else "notice"
        msg = await send_message(key_id, args, db=db, kind=kind)
        if msg is None:
            raise CommandError(404, "Participant not found in channel")
        if kind == "action":
            participant.doing = args
        return {"message": msg.model_dump(mode="json")}

    if name == "topic":
        if args:
            channel.topic = args
            channel.topic_set_by = participant.handle
        return {"topic": channel.topic, "topic_set_by": channel.topic_set_by}

    if name == "away":
        participant.state = "away"
        participant.away_reason = args or None
        participant.idle_away = False
        participant.since = datetime.now(UTC)
        return {"participant": participant.model_dump(mode="json")}

    # back
    if participant.state != "active":
        participant.since = datetime.now(UTC)
    participant.state = "active"
    participant.away_reason = None
    participant.idle_away = False
    return {"participant": participant.model_dump(mode="json")}
