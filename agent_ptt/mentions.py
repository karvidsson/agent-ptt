"""Explicit mention routing and durable per-session inboxes.

Only sessions that register a receiver can be addressed. No LLM, transcript
scans, or display-name guesses are involved in inbox delivery.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from agent_ptt.models import (
    Channel,
    MentionReceiverDB,
    Message,
    MessageDB,
    MessageDeliveryDB,
    ParticipantKey,
)

_MENTION = re.compile(r'(?<![\w@])@(?:"([^"\n]+)"|([\w-]+))')
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)


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


def resolve_recipients(
    channel: Channel, sender_key: str, text: str, recipient_ids: list[str] | None
) -> dict[str, str]:
    """Return stable recipient IDs and display names; [] explicitly disables routing."""
    available = {
        p.mention_id: p.handle
        for p in channel.participants.values()
        if p.mention_id and p.key_id != sender_key
    }
    if recipient_ids == []:
        return {}
    if recipient_ids is not None and any(rid not in available for rid in recipient_ids):
        raise ValueError("Mention recipient is not a receiving agent in this channel")

    selected = {rid: available[rid] for rid in recipient_ids or []}
    for match in _MENTION.finditer(_CODE.sub("", text)):
        name = (match[1] or match[2]).casefold()
        matches = [rid for rid, handle in available.items() if handle.casefold() == name]
        explicit = [rid for rid in matches if rid in (recipient_ids or [])]
        if explicit:
            matches = explicit
        if len(matches) != 1:
            raise ValueError(
                f"Unknown or ambiguous mention @{match[1] or match[2]}; "
                'select an agent from the mention menu (quote multi-word names: @"Name Here")'
            )
        selected[matches[0]] = available[matches[0]]
    if len(selected) > 20:
        raise ValueError("A message can mention at most 20 agents")
    return selected


def persist_deliveries(msg: Message, recipients: dict[str, str], db: Session) -> None:
    """Participate in the caller's message transaction; do not commit here."""
    for recipient_id, handle in recipients.items():
        db.add(
            MessageDeliveryDB(
                message_id=msg.message_id,
                recipient_id=recipient_id,
                channel_id=msg.channel_id,
                handle=handle,
                created_at=msg.timestamp,
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


def pending(channel_id: str, recipient_id: str, db: Session, limit: int = 6) -> list[dict]:
    rows = (
        db.query(MessageDB)
        .join(MessageDeliveryDB, MessageDB.message_id == MessageDeliveryDB.message_id)
        .filter(
            MessageDeliveryDB.channel_id == channel_id,
            MessageDeliveryDB.recipient_id == recipient_id,
            MessageDeliveryDB.delivered_at.is_(None),
        )
        .order_by(MessageDeliveryDB.created_at, MessageDeliveryDB.message_id)
        .limit(limit)
        .all()
    )
    return [Message.model_validate(row).model_dump(mode="json") for row in rows]


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
