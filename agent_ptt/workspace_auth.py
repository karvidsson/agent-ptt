"""Authentication and authorization shared by hosted REST and streaming chat."""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from agent_ptt.db import get_db
from agent_ptt.workspace_models import Agent, Credential, Member, Organization, User, now

COOKIE = "ptt_session"
SESSION_SECONDS = 7 * 24 * 60 * 60
_hash_slots = threading.BoundedSemaphore(2)
_rate_lock = threading.Lock()
_attempts: dict[str, deque] = defaultdict(deque)


def hosted() -> bool:
    return os.environ.get("AGENT_PTT_HOSTED", "0") == "1"


def public_origin() -> str:
    value = os.environ.get("AGENT_PTT_PUBLIC_ORIGIN", "http://localhost:8770").rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or (parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
    ):
        raise RuntimeError(
            "AGENT_PTT_PUBLIC_ORIGIN must be an HTTPS origin (HTTP only on localhost)"
        )
    return value


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    with _hash_slots:
        derived = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=2**17, r=8, p=1, maxmem=256 * 1024 * 1024
        ).hex()
    return f"scrypt${salt}${derived}"


def verify_password(password: str, encoded: str | None) -> bool:
    # The missing-account path performs the same expensive work.
    salt = encoded.split("$")[1] if encoded else "0" * 32
    candidate = hash_password(password, salt)
    return secrets.compare_digest(candidate, encoded or "")


def issue_credential(db: Session, *, user_id=None, agent_id=None, days=7) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        Credential(
            digest=digest(token),
            user_id=user_id,
            agent_id=agent_id,
            expires_at=now() + timedelta(days=days),
        )
    )
    return token


def rate_limit(key: str, limit: int, seconds: int = 60):
    clock = time.monotonic()
    with _rate_lock:
        if len(_attempts) > 10000:
            for old in list(_attempts):
                if not _attempts[old] or _attempts[old][-1] < clock - 3600:
                    del _attempts[old]
            if len(_attempts) > 10000:
                raise HTTPException(429, "Server is busy; try again later")
        attempts = _attempts[key]
        while attempts and attempts[0] <= clock - seconds:
            attempts.popleft()
        if len(attempts) >= limit:
            raise HTTPException(
                429, "Too many requests; try again later", headers={"Retry-After": str(seconds)}
            )
        attempts.append(clock)


def token_from(headers, cookies) -> str:
    authorization = headers.get("authorization", "")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        return token if scheme.lower() == "bearer" else ""
    return cookies.get(COOKIE, "")


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    kind: str
    credential_digest: str
    org_id: str | None = None


def authenticate(token: str, db: Session) -> Principal:
    row = db.get(Credential, digest(token)) if token else None
    if not row or row.expires_at <= now():
        raise HTTPException(401, "Please sign in")
    if row.user_id:
        user = db.get(User, row.user_id)
        if user:
            return Principal(user.id, user.name, "human", row.digest)
    if row.agent_id:
        agent = db.get(Agent, row.agent_id)
        if agent and agent.active:
            return Principal(agent.id, agent.name, "agent", row.digest, agent.org_id)
    raise HTTPException(401, "Credential revoked")


def principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    identity = authenticate(token_from(request.headers, request.cookies), db)
    if identity.kind == "agent" and not request.headers.get("authorization"):
        raise HTTPException(401, "Agents must use a bearer credential")
    return identity


def human(identity: Principal = Depends(principal)) -> Principal:
    if identity.kind != "human":
        raise HTTPException(403, "A human account is required")
    return identity


def role_for(org_id: str, identity: Principal, db: Session) -> str:
    if identity.kind == "agent":
        if identity.org_id == org_id:
            return "member"
    else:
        membership = db.get(Member, (org_id, identity.id))
        if membership:
            org = db.get(Organization, org_id)
            return "owner" if org.owner_id == identity.id else membership.role
    # Do not reveal whether another tenant exists.
    raise HTTPException(404, "Organization not found")


def require_admin(org_id: str, identity: Principal, db: Session) -> str:
    role = role_for(org_id, identity, db)
    if role not in {"owner", "admin"}:
        raise HTTPException(403, "An organization administrator is required")
    return role
