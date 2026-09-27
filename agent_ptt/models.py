"""Data models — Pydantic schemas (API) + SQLAlchemy ORM (persistence).

Voice profiles use a stable engine/settings contract
so configs are portable between the two apps.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import JSON, Column, DateTime, Index, String, Text
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


class ParticipantKey(BaseModel):
    """UUID-based participation key issued on channel join."""

    key_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    handle: str
    voice_id: str | None = None
    channel_id: str | None = None
    mention_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Presence (IRC-style, in memory only)
    state: Literal["active", "away"] = "active"
    away_reason: str | None = None
    since: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_active: datetime = Field(default_factory=lambda: datetime.now(UTC))
    doing: str | None = None

    model_config = {"from_attributes": True}


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


class Message(BaseModel):
    """A single message in a channel."""

    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    channel_id: str
    sender_key: str
    handle: str
    text: str
    kind: MessageKind = "message"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deliveries: list[dict[str, Any]] = Field(default_factory=list)
    context: MessageContext | None = None

    model_config = {"from_attributes": True}

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
