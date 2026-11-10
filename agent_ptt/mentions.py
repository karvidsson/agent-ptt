"""Explicit mention routing and durable per-session inboxes.

Only sessions that register a receiver can be addressed. No LLM, transcript
scans, or display-name guesses are involved in inbox delivery.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from agent_ptt.models import (
    Channel,
    MentionReceiverDB,
    Message,
    MessageDB,
    MessageDeliveryDB,
    ParticipantKey,
)

_MENTION = re.compile(r'(?<![\w@])@(?:role:([\w.-]+)|"([^"\n]+)"|([\w-]+))')
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
_PLAIN = re.compile(r"[\w-]+")
MAX_LISTED_HANDLES = 20


def _token(handle: str) -> str:
    return f"@{handle}" if _PLAIN.fullmatch(handle) else f'@"{handle}"'


def register_receiver(participant: ParticipantKey, session_id: str, agent: str, db: Session):
    recipient_id = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([agent, session_id])))
    db.merge(
        MentionReceiverDB(
            key_id=participant.key_id,
            recipient_id=recipient_id,
            channel_id=participant.channel_id,
        )
    )
    db.commit()
    participant.mention_id = recipient_id


def _unknown(
    name: str, receivers: list[str], members: list[str], in_project: bool, sender: str
) -> ValueError:
    """The 422 text: who can be addressed here, and how."""
    seen: set[str] = set()
    handles: list[str] = []
    for handle in [*receivers, *members]:
        if handle.casefold() in seen or handle.casefold() == sender.casefold():
            continue
        seen.add(handle.casefold())
        handles.append(_token(handle))
    listed = ", ".join(handles[:MAX_LISTED_HANDLES]) or "nobody yet"
    if len(handles) > MAX_LISTED_HANDLES:
        listed += f", … ({len(handles) - MAX_LISTED_HANDLES} more)"
    where = (
        "@role:<role> and @project also work here, and a project member who is not "
        "in the channel gets the mention in its project inbox"
        if in_project
        else "@role:<role> and @project work only in a project's channel"
    )
    return ValueError(
        f"Unknown or ambiguous mention {name}. Addressable here: {listed}. "
        f'Quote multi-word names (@"Name Here"); {where}.'
    )


def resolve_recipients(
    channel: Channel,
    sender_key: str,
    text: str,
    recipient_ids: list[str] | None,
    members: list[Any] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Route a message's mentions; [] explicitly disables routing.

    Returns the live receivers (stable recipient ID -> handle) and the
    durable project addresses (``role:<r>``, ``project``, ``agent:<member_id>``).
    ``members`` is the project's member list when ``channel`` is a project's
    default channel, else None: only then do ``@role:<r>``, ``@project`` and
    ``@Handle`` of an absent project member resolve (to the project inbox).
    Raises ValueError for a name that matches nobody.
    """
    available = {
        p.mention_id: p.handle
        for p in channel.participants.values()
        if p.mention_id and p.key_id != sender_key
    }
    if recipient_ids == []:
        return {}, []
    if recipient_ids is not None and any(rid not in available for rid in recipient_ids):
        raise ValueError("Mention recipient is not a receiving agent in this channel")

    sender = channel.participants.get(sender_key)
    sender_handle = sender.handle if sender else ""
    in_project = members is not None
    selected = {rid: available[rid] for rid in recipient_ids or []}
    durable: list[str] = []

    def fail(name: str) -> ValueError:
        return _unknown(
            name,
            sorted(set(available.values()), key=str.casefold),
            [m.handle for m in members or []],
            in_project,
            sender_handle,
        )

    def queue(address: str) -> None:
        if address not in durable:
            durable.append(address)

    for match in _MENTION.finditer(_CODE.sub("", text)):
        role, quoted, plain = match[1], match[2], match[3]
        if role is not None:
            role = role.rstrip(".")
            if not in_project or not role:
                raise fail(f"@role:{role}")
            queue(f"role:{role}")
            continue
        raw = quoted if quoted is not None else plain
        name = raw.casefold()
        if in_project and plain is not None and name == "project":
            queue("project")
            continue
        matches = [rid for rid, handle in available.items() if handle.casefold() == name]
        explicit = [rid for rid in matches if rid in (recipient_ids or [])]
        if explicit:
            matches = explicit
        if len(matches) == 1:
            selected[matches[0]] = available[matches[0]]
            continue
        label = f'@"{raw}"' if quoted is not None else f"@{raw}"
        if matches or not in_project:
            raise fail(label)
        if name == sender_handle.casefold():
            continue  # Addressing yourself queues nothing.
        found = [m for m in members or [] if m.handle.casefold() == name]
        if len(found) != 1:
            raise fail(label)
        queue(f"agent:{found[0].member_id}")
    if len(selected) > 20:
        raise ValueError("A message can mention at most 20 agents")
    if len(durable) > 20:
        raise ValueError("A message can queue at most 20 project inbox items")
    return selected, durable


def persist_deliveries(msg: Message, recipients: dict[str, str], db: Session) -> None:
    """Participate in the caller's message transaction; do not commit here.

    An FYI message's deliveries are stored with ``wake=False``.
    """
    for recipient_id, handle in recipients.items():
        db.add(
            MessageDeliveryDB(
                message_id=msg.message_id,
                recipient_id=recipient_id,
                channel_id=msg.channel_id,
                handle=handle,
                created_at=msg.timestamp,
                wake=not msg.fyi,
            )
        )
    msg.deliveries = [
        {"recipient_id": rid, "handle": handle, "status": "pending"}
        for rid, handle in recipients.items()
    ]


def attach_receipts(messages: list[Message], channel_id: str, db: Session) -> None:
    by_id = {msg.message_id: msg for msg in messages}
    for msg in messages:
        msg.deliveries = []
    for row in db.query(MessageDeliveryDB).filter_by(channel_id=channel_id):
        if row.message_id in by_id:
            by_id[row.message_id].deliveries.append(
                {
                    "recipient_id": row.recipient_id,
                    "handle": row.handle,
                    "status": "delivered" if row.delivered_at else "pending",
                }
            )


def pending(
    channel_id: str, recipient_id: str, db: Session, limit: int = 6, wake_only: bool = False
) -> list[dict]:
    """Unacked mentions, oldest first; ``wake_only`` leaves out FYIs.

    Each message carries ``fyi`` (true when its delivery does not wake).
    """
    query = (
        db.query(MessageDB, MessageDeliveryDB.wake)
        .join(MessageDeliveryDB, MessageDB.message_id == MessageDeliveryDB.message_id)
        .filter(
            MessageDeliveryDB.channel_id == channel_id,
            MessageDeliveryDB.recipient_id == recipient_id,
            MessageDeliveryDB.delivered_at.is_(None),
        )
    )
    if wake_only:
        query = query.filter(MessageDeliveryDB.wake.is_(True))
    rows = (
        query.order_by(MessageDeliveryDB.created_at, MessageDeliveryDB.message_id)
        .limit(limit)
        .all()
    )
    out = []
    for row, wake in rows:
        data = Message.model_validate(row).model_dump(mode="json")
        data["fyi"] = wake is False
        out.append(data)
    return out


def acknowledge(channel_id: str, recipient_id: str, ids: list[str], db: Session) -> None:
    rows = (
        db.query(MessageDeliveryDB)
        .filter(
            MessageDeliveryDB.channel_id == channel_id,
            MessageDeliveryDB.recipient_id == recipient_id,
            MessageDeliveryDB.message_id.in_(ids),
        )
        .all()
    )
    if {row.message_id for row in rows} != set(ids):
        raise ValueError("Message is not in this recipient's inbox")
    for row in rows:
        if row.delivered_at is None:
            row.delivered_at = datetime.now(UTC)
    db.commit()


# ---------------------------------------------------------------------------
# Long-poll: wake inbox readers when a mention lands
# ---------------------------------------------------------------------------

_inbox_events: dict[tuple[str, str], asyncio.Event] = {}


def notify(channel_id: str, recipient_ids: Iterable[str]) -> None:
    """Wake every long-poll waiting on these recipients' channel inboxes."""
    for recipient_id in recipient_ids:
        event = _inbox_events.get((channel_id, recipient_id))
        if event is not None:
            event.set()


def reset_runtime_state() -> None:
    """Forget in-memory waiters (tests)."""
    _inbox_events.clear()


async def wait_pending(
    channel_id: str, recipient_id: str, limit: int, wait: float, wake_only: bool = False
) -> list[dict]:
    """``pending``, long-polling up to ``wait`` s while the inbox is empty.

    With ``wake_only`` an FYI neither ends the wait nor is returned; it stays
    pending for the next ordinary read. No DB session is held while waiting:
    each check opens and closes one.
    """
    from agent_ptt.db import SessionLocal

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, wait)
    key = (channel_id, recipient_id)
    while True:
        event = _inbox_events.setdefault(key, asyncio.Event())
        event.clear()
        with SessionLocal() as db:
            rows = pending(channel_id, recipient_id, db, limit, wake_only)
        remaining = deadline - loop.time()
        if rows or remaining <= 0:
            return rows
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=remaining)
