"""Data models — Pydantic schemas (API) + SQLAlchemy ORM (persistence).

Voice profiles use a stable engine/settings contract
so configs are portable between the two apps.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)
from sqlalchemy import JSON, Column, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase

# ---------------------------------------------------------------------------
# SQLAlchemy ORM base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""

    pass


# ---------------------------------------------------------------------------
# SQLAlchemy ORM models (persisted)
# ---------------------------------------------------------------------------


class VoiceProfileDB(Base):
    """Persisted voice profile — survives server restart."""

    __tablename__ = "voice_profiles"

    voice_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    display_name = Column(String, nullable=False)
    engine = Column(String, nullable=False, default="pocket-tts")
    # Engine-specific settings: pitch, speed, language, etc.
    settings = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))


class SessionIdentityDB(Base):
    """A CLI session's random name and saved voice, independent of channels."""

    __tablename__ = "session_identities"

    session_id = Column(String, primary_key=True)
    agent = Column(String, primary_key=True)
    handle = Column(String, nullable=False, unique=True)
    voice_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))


class ParticipantKeyDB(Base):
    """Persisted participation key — agent identity across sessions."""

    __tablename__ = "participant_keys"

    key_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    handle = Column(String, nullable=False)
    voice_id = Column(String, nullable=True)
    channel_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))


class PinnedVoiceDB(Base):
    """Auto-designed voice pinned to a handle — same voice across sessions."""

    __tablename__ = "pinned_voices"

    handle = Column(String, primary_key=True)  # lowercase
    voice_id = Column(String, nullable=False)
    source = Column(String, nullable=False, default="hash")
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))


class ArchivedChannelDB(Base):
    """Closed channels retain their records and transcript."""

    __tablename__ = "archived_channels"
    channel_id = Column(String, primary_key=True)
    archived_at = Column(DateTime, default=lambda: datetime.now(UTC))


class RevokedParticipantDB(Base):
    """Kicked keys stay revoked rather than triggering automatic hook rejoin."""

    __tablename__ = "revoked_participants"
    key_id = Column(String, primary_key=True)
    channel_id = Column(String, nullable=False)


class ChannelDB(Base):
    """Persisted channel — so a restart doesn't orphan its transcript."""

    __tablename__ = "channels"

    channel_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))


class MessageDB(Base):
    """Optional message archive — for history retrieval after restart."""

    __tablename__ = "messages"

    message_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id = Column(String, nullable=False, index=True)
    sender_key = Column(String, nullable=False)
    handle = Column(String, nullable=False)
    text = Column(Text, nullable=False)
    # "message" | "action" (/me) | "notice"
    kind = Column(String, nullable=False, default="message", server_default="message")
    timestamp = Column(DateTime, default=lambda: datetime.now(UTC))
    # Optional MessageContext (see the Pydantic model); never spoken.
    context = Column(JSON, nullable=True)


class MentionReceiverDB(Base):
    """Opted-in agent session bound to a channel participation key."""

    __tablename__ = "mention_receivers"

    key_id = Column(String, primary_key=True)
    recipient_id = Column(String, nullable=False, index=True)
    channel_id = Column(String, nullable=False, index=True)


class MessageDeliveryDB(Base):
    """Durable inbox and receipt, independent of a session's participation key."""

    __tablename__ = "message_deliveries"
    __table_args__ = (
        Index("ix_delivery_pending", "recipient_id", "channel_id", "delivered_at", "created_at"),
    )

    message_id = Column(String, primary_key=True)
    recipient_id = Column(String, primary_key=True)
    channel_id = Column(String, nullable=False, index=True)
    handle = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    delivered_at = Column(DateTime, nullable=True)


class ProjectDB(Base):
    """A collaboration project, keyed by its normalized git remote (see projects.py)."""

    __tablename__ = "projects"

    project_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False, index=True)
    remote = Column(String, nullable=True, unique=True)
    channel_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))


class ProjectMemberDB(Base):
    """An agent or human in a project; ``member_key`` is its bearer secret."""

    __tablename__ = "project_members"

    member_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, nullable=False, index=True)
    member_key = Column(String, nullable=False, unique=True)
    handle = Column(String, nullable=False)
    roles = Column(JSON, nullable=False, default=list)
    mode = Column(String, nullable=False, default="monitor")
    harness = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    last_seen = Column(DateTime, default=lambda: datetime.now(UTC))


class WorkItemDB(Base):
    """One item in a project inbox: instruction, handoff, question, answer, …"""

    __tablename__ = "work_items"
    __table_args__ = (
        UniqueConstraint("from_member_id", "client_id", name="ux_work_item_client"),
        Index("ix_work_items_project_updated", "project_id", "updated_at"),
    )

    item_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)
    to_address = Column(String, nullable=False)
    from_member_id = Column(String, nullable=False)
    title = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    context = Column(JSON, nullable=True)
    reply_to = Column(String, nullable=True, index=True)
    client_id = Column(String, nullable=True)
    state = Column(String, nullable=False, default="pending")
    assignee_member_id = Column(String, nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    notice_message_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, default=lambda: datetime.now(UTC))


class WorkItemDeliveryDB(Base):
    """A work item handed to one member; ``seq`` is the inbox cursor."""

    __tablename__ = "work_item_deliveries"
    __table_args__ = (UniqueConstraint("item_id", "member_id", name="ux_work_item_delivery"),)

    seq = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(String, nullable=False, index=True)
    member_id = Column(String, nullable=False, index=True)
    state = Column(String, nullable=False, default="pending")
    delivered_at = Column(DateTime, nullable=True)
    acked_at = Column(DateTime, nullable=True)


class WorkItemEventDB(Base):
    """Who changed a work item, when, and from which state to which."""

    __tablename__ = "work_item_events"

    event_id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(String, nullable=False, index=True)
    at = Column(DateTime, default=lambda: datetime.now(UTC))
    by_member_id = Column(String, nullable=True)
    action = Column(String, nullable=False)
    from_state = Column(String, nullable=True)
    to_state = Column(String, nullable=True)
    note = Column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Pydantic schemas (API / transport)
# ---------------------------------------------------------------------------


class VoiceProfile(BaseModel):
    """Persisted speech voice profile."""

    voice_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    display_name: str
    engine: str = "pocket-tts"
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = {"from_attributes": True}


def public_participant_id(key_id: str) -> str:
    """The public handle for a participation key.

    ``key_id`` is a bearer secret: whoever holds it can speak as the
    participant. Rosters, transcripts and broadcasts show this one-way hash
    instead, and a client holding its own key can recompute it to recognize
    itself.
    """
    return hashlib.sha256(key_id.encode()).hexdigest()[:16]


class ParticipantKey(BaseModel):
    """UUID-based participation key issued on channel join.

    ``key_id`` is secret and excluded from serialization; only the join
    response returns it, explicitly.
    """

    key_id: str = Field(default_factory=lambda: str(uuid.uuid4()), exclude=True)
    handle: str
    voice_id: str | None = None
    channel_id: str | None = None
    mention_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Presence (IRC-style, in memory only). "offline" marks a participant
    # restored after a server restart that hasn't been seen since.
    state: Literal["active", "away", "offline"] = "active"
    away_reason: str | None = None
    since: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Last time the participant said or did something (say, command).
    last_active: datetime = Field(default_factory=lambda: datetime.now(UTC))
    doing: str | None = None
    # Last sign of life of any kind (join, say, command, inbox poll,
    # heartbeat, agent WebSocket); the idle reaper reads it. Internal.
    last_seen: datetime = Field(default_factory=lambda: datetime.now(UTC), exclude=True)

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def participant_id(self) -> str:
        return public_participant_id(self.key_id)


MessageKind = Literal["message", "action", "notice"]

MESSAGE_CONTEXT_MAX_BYTES = 4096
MESSAGE_CONTEXT_MAX_FILES = 50


class ContextFile(BaseModel):
    """A file the sender touched since its last spoken line."""

    path: str
    op: Literal["edit", "read", "run"]

    model_config = {"extra": "forbid"}


class MessageContext(BaseModel):
    """The detail behind a spoken line, pulled on demand, never synthesized.

    Every key is optional. Bounded to ``MESSAGE_CONTEXT_MAX_FILES`` file
    entries and ``MESSAGE_CONTEXT_MAX_BYTES`` of serialized JSON so the
    archive and the single-message endpoint stay light.
    """

    agent: str | None = None
    session_id: str | None = None
    event: str | None = None
    cwd: str | None = None
    repo: str | None = None
    branch: str | None = None
    worktree: str | None = None
    dirty: int | None = None
    files: list[ContextFile] | None = Field(default=None, max_length=MESSAGE_CONTEXT_MAX_FILES)
    tools: dict[str, int] | None = None
    task: str | None = Field(default=None, max_length=200)
    transcript: str | None = None
    trace_id: str | None = None
    host: str | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _within_size_budget(self) -> MessageContext:
        size = len(self.model_dump_json(exclude_none=True).encode("utf-8"))
        if size > MESSAGE_CONTEXT_MAX_BYTES:
            raise ValueError(
                f"context is {size} bytes serialized; at most {MESSAGE_CONTEXT_MAX_BYTES} allowed"
            )
        return self


class HeartbeatRequest(BaseModel):
    """Keeps a participant without an agent WebSocket from being reaped as idle."""

    key_id: str = Field(min_length=1, description="Secret participation key returned by join")


class Message(BaseModel):
    """A single message in a channel."""

    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    channel_id: str
    sender_key: str = Field(exclude=True)
    handle: str
    text: str
    kind: MessageKind = "message"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deliveries: list[dict[str, Any]] = Field(default_factory=list)
    context: MessageContext | None = None

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def sender_id(self) -> str:
        """Public ID of the sender (see ``public_participant_id``)."""
        return public_participant_id(self.sender_key)

    @field_validator("kind", mode="before")
    @classmethod
    def _default_kind(cls, value: Any) -> Any:
        # Rows archived before `kind` existed (and unflushed ORM rows) carry None.
        return "message" if value is None else value


class Channel(BaseModel):
    """An active voice channel — ephemeral, lives in memory."""

    channel_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    archived: bool = False
    participants: dict[str, ParticipantKey] = Field(default_factory=dict)
    messages: list[Message] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    topic: str | None = None
    topic_set_by: str | None = None

    @field_serializer("participants", mode="wrap")
    def _participants_by_public_id(self, value: dict[str, ParticipantKey], handler) -> Any:
        # The in-memory dict is keyed by secret key_id; never expose those keys.
        return {public_participant_id(key): data for key, data in handler(value).items()}


# ---------------------------------------------------------------------------
# Projects and work items (see projects.py and docs/projects.md)
# ---------------------------------------------------------------------------

WorkItemKind = Literal["instruction", "handoff", "question", "answer", "claim", "release", "note"]
WorkItemState = Literal[
    "pending", "delivered", "acked", "claimed", "waiting", "review", "done", "declined"
]
MemberMode = Literal["monitor", "hooks"]
WORK_ITEM_BODY_MAX = 8000
WORK_ITEM_TITLE_MAX = 120


class ProjectJoinRequest(BaseModel):
    """Join (or rejoin, with ``member_key``) a project."""

    remote: str | None = Field(default=None, max_length=500, description="git remote URL")
    project_id: str | None = None
    name: str | None = Field(default=None, max_length=160)
    handle: str = Field(min_length=1, max_length=80)
    roles: list[str] | None = Field(default=None, max_length=20)
    # None keeps the stored value on a rejoin; a new member defaults to "monitor".
    mode: MemberMode | None = None
    harness: str | None = Field(default=None, max_length=40)
    member_key: str | None = Field(default=None, description="Rejoin as this member")

    @field_validator("roles")
    @classmethod
    def _clean_roles(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        roles: list[str] = []
        for role in value:
            role = role.strip()
            if not role or len(role) > 60 or ":" in role:
                raise ValueError(f"invalid role: {role!r}")
            if role not in roles:
                roles.append(role)
        return roles

    @model_validator(mode="after")
    def _one_locator(self) -> ProjectJoinRequest:
        if not (self.remote or self.project_id or self.name):
            raise ValueError("one of remote, project_id or name is required")
        return self


class ProjectOut(BaseModel):
    project_id: str
    name: str
    remote: str | None = None
    channel_id: str | None = None


class MemberOut(BaseModel):
    member_id: str
    handle: str
    roles: list[str] = Field(default_factory=list)
    mode: MemberMode = "monitor"
    harness: str | None = None


class ProjectMemberOut(MemberOut):
    last_seen: datetime | None = None
    listening: bool = False


class ProjectJoinResponse(BaseModel):
    project: ProjectOut
    member: MemberOut
    member_key: str


class ProjectSummary(ProjectOut):
    member_count: int
    open_items: int


class MemberRef(BaseModel):
    member_id: str
    handle: str
    harness: str | None = None


class EventRef(BaseModel):
    action: str
    by: MemberRef | None = None
    at: datetime


class WorkItemOut(BaseModel):
    """The public item shape used by the inbox, the board and threads."""

    item_id: str
    project_id: str
    project_name: str
    kind: WorkItemKind
    to: str
    # `to` with an agent address shown by handle (`agent:Finn`), for display.
    to_label: str = ""
    from_: MemberRef | None = Field(alias="from", serialization_alias="from")
    title: str
    body: str
    context: dict[str, Any] | None = None
    reply_to: str | None = None
    state: WorkItemState
    assignee: MemberRef | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_event: EventRef | None = None
    channel_id: str | None = None
    notice_message_id: str | None = None
    open_questions: int = 0

    model_config = {"populate_by_name": True}


class WorkItemCreate(BaseModel):
    kind: WorkItemKind
    to: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=WORK_ITEM_TITLE_MAX)
    body: str = Field(min_length=1, max_length=WORK_ITEM_BODY_MAX)
    context: dict[str, Any] | None = None
    reply_to: str | None = None
    client_id: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("body")
    @classmethod
    def _body_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body must not be empty")
        return value

    @field_validator("context")
    @classmethod
    def _context_size(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            import json

            if len(json.dumps(value, default=str).encode()) > 16_384:
                raise ValueError("context must be at most 16384 bytes serialized")
        return value


class WorkItemNote(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class WorkItemStateChange(WorkItemNote):
    state: Literal["waiting", "review", "claimed"]


class WorkItemEventOut(BaseModel):
    action: str
    by: MemberRef | None = None
    at: datetime
    from_state: str | None = None
    to_state: str | None = None
    note: str | None = None
