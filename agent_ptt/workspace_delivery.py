"""Tenant-owned agent inboxes. Receipts record handoff, replies record an answer.

Recipients are authenticated Agent IDs, never client-provided session names.
Delivery creation is part of the existing chat-message transaction.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, DateTime, ForeignKeyConstraint, Index, Integer, String, select
from sqlalchemy.orm import Session

from agent_ptt.db import get_db
from agent_ptt.models import Base
from agent_ptt.workspace_auth import Principal, principal, rate_limit
from agent_ptt.workspace_models import Agent, now


class Delivery(Base):
    __tablename__ = "workspace_deliveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "message_id"], ["workspace_messages.org_id", "workspace_messages.id"]
        ),
        ForeignKeyConstraint(
            ["org_id", "recipient_id"], ["workspace_agents.org_id", "workspace_agents.id"]
        ),
        Index("ix_workspace_pending", "org_id", "recipient_id", "delivered_at", "message_id"),
    )
    org_id = Column(String, primary_key=True)
    message_id = Column(Integer, primary_key=True)
    recipient_id = Column(String, primary_key=True)
    recipient_name = Column(String(80), nullable=False)
    delivered_at = Column(DateTime, nullable=True)


class Reply(Base):
    __tablename__ = "workspace_replies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "message_id"], ["workspace_messages.org_id", "workspace_messages.id"]
        ),
        ForeignKeyConstraint(
            ["org_id", "parent_id", "agent_id"],
            [
                "workspace_deliveries.org_id",
                "workspace_deliveries.message_id",
                "workspace_deliveries.recipient_id",
            ],
        ),
    )
    message_id = Column(Integer, primary_key=True)
    org_id = Column(String, nullable=False)
    parent_id = Column(Integer, nullable=False, index=True)
    agent_id = Column(String, nullable=False)


def prepare(db, org_id, room_id, identity, recipient_ids, reply_to):
    """Validate recipient and reply authority before creating any records."""
    from agent_ptt.workspace_models import ChatMessage

    targets = sorted(set(recipient_ids))
    agents = list(
        db.scalars(
            select(Agent).where(
                Agent.org_id == org_id, Agent.id.in_(targets), Agent.active.is_(True)
            )
        )
    )
    if {a.id for a in agents} != set(targets) or identity.id in targets:
        raise HTTPException(
            422, "Recipients must be active agents in this organization other than the sender"
        )
    if reply_to is not None:
        parent = db.scalar(
            select(ChatMessage).where(
                ChatMessage.org_id == org_id,
                ChatMessage.room_id == room_id,
                ChatMessage.id == reply_to,
            )
        )
        delivery = (
            db.get(Delivery, (org_id, reply_to, identity.id)) if identity.kind == "agent" else None
        )
        if not parent or not delivery:
            raise HTTPException(404, "Addressed message not found in this agent's inbox")
    return agents


def persist(db, row, agents, reply_to):
    for agent in agents:
        db.add(
            Delivery(
                org_id=row.org_id,
                message_id=row.id,
                recipient_id=agent.id,
                recipient_name=agent.name,
            )
        )
    if reply_to is not None:
        db.add(
            Reply(org_id=row.org_id, message_id=row.id, parent_id=reply_to, agent_id=row.sender_id)
        )
        delivery = db.get(Delivery, (row.org_id, reply_to, row.sender_id))
        if delivery.delivered_at is None:
            delivery.delivered_at = now()


def details(db, rows):
    """Batch receipt lookup; no per-message query during transcript/stream reads."""
    if not rows:
        return {}
    ids = [row.id for row in rows]
    org = rows[0].org_id
    result = {mid: {"recipient_ids": [], "deliveries": [], "reply_to": None} for mid in ids}
    deliveries = db.scalars(
        select(Delivery).where(Delivery.org_id == org, Delivery.message_id.in_(ids))
    )
    for d in deliveries:
        result[d.message_id]["recipient_ids"].append(d.recipient_id)
        result[d.message_id]["deliveries"].append(
            {
                "recipient_id": d.recipient_id,
                "name": d.recipient_name,
                "status": "delivered" if d.delivered_at else "pending",
            }
        )
    for r in db.scalars(select(Reply).where(Reply.org_id == org, Reply.message_id.in_(ids))):
        result[r.message_id]["reply_to"] = r.parent_id
    return result


def same_request(db, row, room_id, req):
    if not row or row.room_id != room_id or row.text != req.text:
        return False
    data = details(db, [row])[row.id]
    return set(data["recipient_ids"]) == set(req.recipient_ids) and data["reply_to"] == req.reply_to


router = APIRouter(prefix="/api/workspace", tags=["Agent delivery"])


class Acknowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_ids: list[int] = Field(min_length=1, max_length=20)


def authorize_agent(org_id, room_id, identity, db):
    from agent_ptt.workspace import require_room

    require_room(org_id, room_id, identity, db)
    if identity.kind != "agent":
        raise HTTPException(403, "An agent credential is required")


@router.get("/organizations/{org_id}/channels/{room_id}/inbox")
def inbox(
    org_id: str,
    room_id: str,
    limit: int = Query(default=6, ge=1, le=20),
    identity: Principal = Depends(principal),
    db: Session = Depends(get_db),
):
    from agent_ptt.workspace import message_json
    from agent_ptt.workspace_models import ChatMessage

    authorize_agent(org_id, room_id, identity, db)
    rate_limit("inbox:" + identity.id, 120)
    rows = list(
        db.scalars(
            select(ChatMessage)
            .join(
                Delivery,
                (Delivery.org_id == ChatMessage.org_id) & (Delivery.message_id == ChatMessage.id),
            )
            .where(
                Delivery.org_id == org_id,
                Delivery.recipient_id == identity.id,
                Delivery.delivered_at.is_(None),
                ChatMessage.room_id == room_id,
            )
            .order_by(ChatMessage.id)
            .limit(limit)
        )
    )
    metadata = details(db, rows)
    return [{**message_json(row), **metadata[row.id]} for row in rows]


@router.post("/organizations/{org_id}/channels/{room_id}/inbox/ack")
def acknowledge(
    org_id: str,
    room_id: str,
    req: Acknowledge,
    identity: Principal = Depends(principal),
    db: Session = Depends(get_db),
):
    from agent_ptt.workspace_models import ChatMessage

    authorize_agent(org_id, room_id, identity, db)
    rate_limit("inbox-ack:" + identity.id, 120)
    rows = list(
        db.scalars(
            select(Delivery)
            .join(
                ChatMessage,
                (ChatMessage.org_id == Delivery.org_id) & (ChatMessage.id == Delivery.message_id),
            )
            .where(
                Delivery.org_id == org_id,
                Delivery.recipient_id == identity.id,
                Delivery.message_id.in_(req.message_ids),
                ChatMessage.room_id == room_id,
            )
        )
    )
    if {r.message_id for r in rows} != set(req.message_ids):
        raise HTTPException(404, "Message not found in this agent's inbox")
    for row in rows:
        if row.delivered_at is None:
            row.delivered_at = now()
    db.commit()
    return {"status": "delivered", "message_ids": sorted(set(req.message_ids))}
