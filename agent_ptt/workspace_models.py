"""Organization-owned chat records, isolated from the legacy local voice rooms.

Additive tables allow existing local databases to keep their data unchanged.
Tenant ownership is explicit, including composite foreign keys on chat records.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from agent_ptt.models import Base


def new_id():
    return str(uuid.uuid4())


def now():
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "workspace_users"
    id = Column(String, primary_key=True, default=new_id)
    email = Column(String(254), unique=True, nullable=False)
    name = Column(String(80), nullable=False)
    password_hash = Column(Text, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)


class Organization(Base):
    __tablename__ = "workspace_organizations"
    id = Column(String, primary_key=True, default=new_id)
    name = Column(String(80), nullable=False)
    # Ownership is a single explicit relationship: the owner cannot be removed
    # through ordinary role/member editing.
    owner_id = Column(String, ForeignKey("workspace_users.id"), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)


class Member(Base):
    __tablename__ = "workspace_members"
    org_id = Column(String, ForeignKey("workspace_organizations.id"), primary_key=True)
    user_id = Column(String, ForeignKey("workspace_users.id"), primary_key=True)
    role = Column(String, nullable=False, default="member")


class Credential(Base):
    __tablename__ = "workspace_credentials"
    digest = Column(String(64), primary_key=True)
    user_id = Column(String, ForeignKey("workspace_users.id"), nullable=True)
    agent_id = Column(String, ForeignKey("workspace_agents.id"), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)


class Agent(Base):
    __tablename__ = "workspace_agents"
    __table_args__ = (UniqueConstraint("org_id", "id"),)
    id = Column(String, primary_key=True, default=new_id)
    org_id = Column(String, ForeignKey("workspace_organizations.id"), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    onboarded_by = Column(String, nullable=True)
    harness = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=now, nullable=False)


class Invitation(Base):
    __tablename__ = "workspace_invitations"
    digest = Column(String(64), primary_key=True)
    org_id = Column(String, ForeignKey("workspace_organizations.id"), nullable=False)
    agent_limit = Column(Integer, nullable=False, default=0, server_default="0")
    channel_id = Column(String, nullable=True)
    claimed_by = Column(String, nullable=True)
    # Invitations are possession-based, one-use links, not unverified email claims.
    role = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_by = Column(String, ForeignKey("workspace_users.id"), nullable=False)


class Room(Base):
    __tablename__ = "workspace_rooms"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("org_id", "name"),
    )
    id = Column(String, primary_key=True, default=new_id)
    org_id = Column(String, ForeignKey("workspace_organizations.id"), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)


class JoinLink(Base):
    __tablename__ = "workspace_join_links"
    org_id = Column(String, ForeignKey("workspace_organizations.id"), primary_key=True)
    digest = Column(String(64), unique=True, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    created_by = Column(String, ForeignKey("workspace_users.id"), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)


class AgentEnrollment(Base):
    __tablename__ = "workspace_agent_enrollments"
    # Keep the enrollment even after credential revocation/expiry, so retrying
    # cannot recreate a revoked identity or silently renew its access.
    credential_digest = Column(String(64), primary_key=True)
    agent_id = Column(String, ForeignKey("workspace_agents.id"), nullable=False, unique=True)
    link_digest = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)


class ChatMessage(Base):
    __tablename__ = "workspace_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "room_id"], ["workspace_rooms.org_id", "workspace_rooms.id"]
        ),
        UniqueConstraint("org_id", "sender_id", "client_id"),
        Index("ux_workspace_message_tenant", "org_id", "id", unique=True),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(String, ForeignKey("workspace_organizations.id"), nullable=False, index=True)
    room_id = Column(String, nullable=False, index=True)
    sender_id = Column(String, nullable=False)
    sender_name = Column(String(80), nullable=False)
    sender_kind = Column(String, nullable=False)
    client_id = Column(String(64), nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)


class AuditEvent(Base):
    __tablename__ = "workspace_audit"
    id = Column(String, primary_key=True, default=new_id)
    org_id = Column(String, ForeignKey("workspace_organizations.id"), nullable=False, index=True)
    actor_id = Column(String, nullable=False)
    action = Column(String, nullable=False)
    target_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
