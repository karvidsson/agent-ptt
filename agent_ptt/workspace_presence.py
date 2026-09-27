"""Authenticated tenant session presence; chat traffic never updates this table.

Import before Base.metadata.create_all, then install(app) alongside workspace's
router/middleware. No dependency on workspace.py or process-local presence state.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_ptt.db import get_db
from agent_ptt.models import Base
from agent_ptt.workspace_auth import Principal, principal, rate_limit, role_for
from agent_ptt.workspace_models import Agent, Member, User, now

STATES = ("idle", "listening", "thinking", "working", "draining", "offline")
STALE_AFTER_SECONDS = 90
IDLE_STALE_AFTER_SECONDS = 3600
MAX_SESSIONS_PER_IDENTITY = 100
State = Literal["idle", "listening", "thinking", "working", "draining", "offline"]
SessionID = Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[\w.-]+$")]
router = APIRouter(prefix="/api/workspace/organizations/{org_id}", tags=["Workspace presence"])


class SessionPresence(Base):
    __tablename__ = "workspace_session_presence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["workspace_members.org_id", "workspace_members.user_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_id"],
            ["workspace_agents.org_id", "workspace_agents.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "channel_id"], ["workspace_rooms.org_id", "workspace_rooms.id"]
        ),
        CheckConstraint(
            "(user_id IS NOT NULL AND agent_id IS NULL) OR "
            "(user_id IS NULL AND agent_id IS NOT NULL)",
            name="ck_presence_identity",
        ),
        CheckConstraint(
            "state IN ('idle','listening','thinking','working','draining','offline')",
            name="ck_presence_state",
        ),
        CheckConstraint("tokens_in IS NULL OR tokens_in >= 0", name="ck_presence_tokens_in"),
        CheckConstraint("tokens_out IS NULL OR tokens_out >= 0", name="ck_presence_tokens_out"),
    )
    org_id = Column(String, primary_key=True)
    session_id = Column(String(128), primary_key=True)
    user_id = Column(String, nullable=True)
    agent_id = Column(String, nullable=True)
    state = Column(String(16), nullable=False)
    since = Column(DateTime, nullable=False)
    last_refresh = Column(DateTime, nullable=False)
    task = Column(String(128), nullable=True)
    channel_id = Column(String, nullable=True)
    tokens_in = Column(Integer, nullable=True)
    tokens_out = Column(Integer, nullable=True)


class PresenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: State
    task: str | None = Field(default=None, max_length=128)
    channel_id: str | None = Field(default=None, min_length=1, max_length=128)
    tokens_in: int | None = Field(default=None, ge=0, le=2_147_483_647, strict=True)
    tokens_out: int | None = Field(default=None, ge=0, le=2_147_483_647, strict=True)


def utc(value: datetime) -> str:
    """Workspace timestamps are stored as naive UTC, serialize explicitly as UTC."""
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def presence_json(row: SessionPresence, handle: str, clock: datetime) -> dict:
    age = max(0, (clock - row.last_refresh).total_seconds())
    stale = age > IDLE_STALE_AFTER_SECONDS
    return {
        "session_id": row.session_id,
        "principal_id": row.agent_id or row.user_id,
        "kind": "agent" if row.agent_id else "human",
        "agent": handle if row.agent_id else None,
        "handle": handle,
        "state": row.state,
        "since": utc(row.since),
        "task": row.task,
        "channel_id": row.channel_id,
        "last_refresh": utc(row.last_refresh),
        "presumed_hung": row.state in {"thinking", "working"} and age > STALE_AFTER_SECONDS,
        "stale": stale,
        "effective_state": "offline" if stale else row.state,
        "tokens_in": row.tokens_in,
        "tokens_out": row.tokens_out,
    }


def own_session(row: SessionPresence, identity: Principal) -> bool:
    return row.agent_id == identity.id if identity.kind == "agent" else row.user_id == identity.id


@router.post("/sessions/{session_id}/presence", status_code=204)
def update_presence(
    org_id: str,
    session_id: SessionID,
    body: PresenceUpdate,
    identity: Principal = Depends(principal),
    db: Session = Depends(get_db),
):
    role_for(org_id, identity, db)
    rate_limit(f"presence:{org_id}:{identity.kind}:{identity.id}", 240)
    if body.channel_id is not None:
        from agent_ptt.workspace_models import Room

        room = db.get(Room, body.channel_id)
        if not room or room.org_id != org_id:
            raise HTTPException(404, "Channel not found")

    row = db.get(SessionPresence, (org_id, session_id), with_for_update=True)
    clock = now()
    if row is None:
        owner_column = (
            SessionPresence.agent_id if identity.kind == "agent" else SessionPresence.user_id
        )
        existing = db.scalars(
            select(SessionPresence.session_id)
            .where(SessionPresence.org_id == org_id, owner_column == identity.id)
            .limit(MAX_SESSIONS_PER_IDENTITY)
        ).all()
        if len(existing) >= MAX_SESSIONS_PER_IDENTITY:
            raise HTTPException(429, "Session limit reached; reuse an existing session ID")
        row = SessionPresence(
            org_id=org_id,
            session_id=session_id,
            agent_id=identity.id if identity.kind == "agent" else None,
            user_id=identity.id if identity.kind == "human" else None,
            state=body.state,
            since=clock,
            last_refresh=clock,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            # Concurrent first heartbeat: reload the winner, never take ownership.
            db.rollback()
            row = db.get(SessionPresence, (org_id, session_id), with_for_update=True)
            if row is None:
                raise HTTPException(409, "Presence changed; retry the heartbeat") from None
    if not own_session(row, identity):
        raise HTTPException(404, "Session not found")
    if row.state != body.state:
        row.since = clock
    row.state = body.state
    row.last_refresh = clock
    for field in ("task", "channel_id", "tokens_in", "tokens_out"):
        if field in body.model_fields_set:
            setattr(row, field, getattr(body, field))
    db.commit()
    return Response(status_code=204)


@router.get("/sessions")
def sessions(
    org_id: str,
    channel_id: str | None = Query(default=None, min_length=1, max_length=128),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    identity: Principal = Depends(principal),
    db: Session = Depends(get_db),
):
    role_for(org_id, identity, db)
    # Current membership/agent status is authoritative, not a cached name or role.
    query = (
        select(SessionPresence, User.name, Agent.name)
        .outerjoin(User, SessionPresence.user_id == User.id)
        .outerjoin(
            Member,
            (Member.org_id == SessionPresence.org_id) & (Member.user_id == SessionPresence.user_id),
        )
        .outerjoin(
            Agent,
            (Agent.org_id == SessionPresence.org_id) & (Agent.id == SessionPresence.agent_id),
        )
        .where(
            SessionPresence.org_id == org_id,
            (Member.user_id.is_not(None)) | (Agent.active.is_(True)),
        )
    )
    if channel_id is not None:
        from agent_ptt.workspace_models import Room

        room = db.get(Room, channel_id)
        if not room or room.org_id != org_id:
            raise HTTPException(404, "Channel not found")
        query = query.where(SessionPresence.channel_id == channel_id)
    rows = db.execute(query.order_by(SessionPresence.session_id).offset(offset).limit(limit)).all()
    clock = now()
    return [
        presence_json(row, agent_name or user_name, clock) for row, user_name, agent_name in rows
    ]


def install(app) -> None:
    """Register routes; workspace.install owns the common origin/auth boundary."""
    app.include_router(router)
