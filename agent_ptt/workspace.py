"""Self-service organizations and tenant-scoped human/agent chat."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from agent_ptt import workspace_delivery as delivery
from agent_ptt.db import SessionLocal, get_db
from agent_ptt.mention_policy import mention_policy
from agent_ptt.workspace_auth import (
    COOKIE,
    SESSION_SECONDS,
    Principal,
    authenticate,
    digest,
    hash_password,
    hosted,
    human,
    issue_credential,
    principal,
    public_origin,
    rate_limit,
    require_admin,
    role_for,
    token_from,
    verify_password,
)
from agent_ptt.workspace_models import (
    Agent,
    AuditEvent,
    ChatMessage,
    Credential,
    Invitation,
    Member,
    Organization,
    Room,
    User,
    now,
)

router = APIRouter(prefix="/api/workspace", tags=["Organization workspace"])
STATIC = Path(__file__).parent / "static"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def trim_labels(cls, value, info):
        return value.strip() if isinstance(value, str) and info.field_name != "password" else value


class Named(Input):
    name: str = Field(min_length=1, max_length=80)


class Login(Input):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value):
        value = value.casefold()
        if value.count("@") != 1 or any(c.isspace() for c in value):
            raise ValueError("Enter an email address")
        local, domain = value.split("@")
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("Enter an email address")
        return value


class Signup(Login):
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=15, max_length=128)
    organization: str | None = Field(default=None, min_length=1, max_length=80)
    invitation_token: str | None = Field(default=None, min_length=20, max_length=100)


class RoleChange(Input):
    role: Literal["admin", "member"] = "member"


class InviteRequest(RoleChange):
    agent_limit: int = Field(default=0, ge=0, le=20)
    channel_id: str | None = None


class OnboardAgent(Named):
    harness: Literal["claude", "codex", "gemini", "opencode", "custom"] = "codex"


class InviteToken(Input):
    token: str = Field(min_length=20, max_length=100)


class AcceptInvite(InviteToken):
    agents: list[OnboardAgent] = Field(default_factory=list, max_length=20)

    @field_validator("agents")
    @classmethod
    def distinct_names(cls, value):
        if len({agent.name.casefold() for agent in value}) != len(value):
            raise ValueError("Each agent needs a different name")
        return value


class SendMessage(Input):
    text: str = Field(min_length=1, max_length=8000)
    client_id: str = Field(min_length=1, max_length=64, pattern=r"^[\w-]+$")
    recipient_ids: list[str] = Field(default_factory=list, max_length=20)
    reply_to: int | None = Field(default=None, ge=1)


def audit(db, org_id, actor_id, action, target_id):
    db.add(AuditEvent(org_id=org_id, actor_id=actor_id, action=action, target_id=target_id))


def create_org(db, name, user_id):
    org = Organization(name=name, owner_id=user_id)
    db.add(org)
    db.flush()
    db.add(Member(org_id=org.id, user_id=user_id, role="member"))
    db.add(Room(org_id=org.id, name="general"))
    audit(db, org.id, user_id, "organization.created", org.id)
    return org


def set_session(response, token):
    response.set_cookie(
        COOKIE,
        token,
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=public_origin().startswith("https://"),
        samesite="strict",
        path="/",
    )


def auth_limit(request):
    # Use the direct peer. Trust proxy headers only via an explicitly configured Uvicorn proxy.
    rate_limit("auth:" + (request.client.host if request.client else "unknown"), 15, 300)


@router.post("/signup", status_code=201)
def signup(req: Signup, request: Request, response: Response, db: Session = Depends(get_db)):
    auth_limit(request)
    if os.environ.get("AGENT_PTT_SIGNUP", "1") != "1" and not req.invitation_token:
        raise HTTPException(403, "Account signup is disabled")
    user = User(email=req.email, name=req.name, password_hash=hash_password(req.password))
    db.add(user)
    try:
        db.flush()
        if req.invitation_token:
            # Reserve this one-use invitation for this account in the same transaction.
            # This also permits invited signup when public registration is closed.
            claimed = db.execute(
                update(Invitation)
                .where(
                    Invitation.digest == digest(req.invitation_token),
                    Invitation.expires_at > now(),
                    Invitation.claimed_by.is_(None),
                )
                .values(claimed_by=user.id)
            )
            if claimed.rowcount != 1:
                db.rollback()
                raise HTTPException(404, "Invitation is expired or unavailable")
        elif req.organization:
            create_org(db, req.organization, user.id)
        token = issue_credential(db, user_id=user.id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Account could not be created; try signing in") from exc
    set_session(response, token)
    return {"id": user.id, "name": user.name}


@router.post("/login")
def login(req: Login, request: Request, response: Response, db: Session = Depends(get_db)):
    auth_limit(request)
    rate_limit("account:" + digest(req.email), 15, 300)
    user = db.scalar(select(User).where(User.email == req.email))
    if not verify_password(req.password, user.password_hash if user else None):
        raise HTTPException(401, "Incorrect email or password")
    # Replace any existing browser session instead of leaving it behind.
    old = db.get(Credential, digest(request.cookies.get(COOKIE, "")))
    if old and old.user_id:
        db.delete(old)
    token = issue_credential(db, user_id=user.id)
    db.commit()
    set_session(response, token)
    return {"id": user.id, "name": user.name}


@router.post("/logout", status_code=204)
def logout(
    response: Response, identity: Principal = Depends(principal), db: Session = Depends(get_db)
):
    db.execute(delete(Credential).where(Credential.digest == identity.credential_digest))
    db.commit()
    response.delete_cookie(COOKIE, path="/")


@router.get("/me")
def me(identity: Principal = Depends(principal), db: Session = Depends(get_db)):
    if identity.kind == "human":
        orgs = db.scalars(
            select(Organization).join(Member).where(Member.user_id == identity.id)
        ).all()
    else:
        orgs = [db.get(Organization, identity.org_id)]
    return {
        "id": identity.id,
        "name": identity.name,
        "kind": identity.kind,
        **({"mention_policy": mention_policy()} if identity.kind == "agent" else {}),
        "organizations": [
            {"id": org.id, "name": org.name, "role": role_for(org.id, identity, db)} for org in orgs
        ],
    }


@router.post("/organizations", status_code=201)
def new_organization(
    req: Named, identity: Principal = Depends(human), db: Session = Depends(get_db)
):
    rate_limit("org:" + identity.id, 10, 3600)
    org = create_org(db, req.name, identity.id)
    db.commit()
    return {"id": org.id, "name": org.name, "role": "owner"}


@router.get("/organizations/{org_id}/members")
def members(org_id: str, identity: Principal = Depends(principal), db: Session = Depends(get_db)):
    role_for(org_id, identity, db)
    org = db.get(Organization, org_id)
    rows = db.execute(select(User, Member).join(Member).where(Member.org_id == org_id)).all()
    return [
        {
            "id": user.id,
            "name": user.name,
            "kind": "human",
            "role": "owner" if user.id == org.owner_id else member.role,
        }
        for user, member in rows
    ]


@router.patch("/organizations/{org_id}/members/{user_id}")
def change_role(
    org_id: str,
    user_id: str,
    req: RoleChange,
    identity: Principal = Depends(human),
    db: Session = Depends(get_db),
):
    if require_admin(org_id, identity, db) != "owner":
        raise HTTPException(403, "Only the owner can change roles")
    if db.get(Organization, org_id).owner_id == user_id:
        raise HTTPException(409, "The owner role cannot be changed here")
    member = db.get(Member, (org_id, user_id))
    if not member:
        raise HTTPException(404, "Member not found")
    member.role = req.role
    if req.role == "member":
        db.execute(
            delete(Invitation).where(Invitation.org_id == org_id, Invitation.created_by == user_id)
        )
    audit(db, org_id, identity.id, "member.role_changed", user_id)
    db.commit()
    return {"id": user_id, "role": member.role}


@router.delete("/organizations/{org_id}/members/{user_id}", status_code=204)
def remove_member(
    org_id: str, user_id: str, identity: Principal = Depends(human), db: Session = Depends(get_db)
):
    role = require_admin(org_id, identity, db)
    member = db.get(Member, (org_id, user_id))
    if not member:
        raise HTTPException(404, "Member not found")
    if db.get(Organization, org_id).owner_id == user_id or (
        member.role == "admin" and role != "owner"
    ):
        raise HTTPException(403, "Cannot remove this member")
    db.delete(member)
    # Outstanding invitations created by a removed member must not grant access.
    db.execute(
        delete(Invitation).where(Invitation.org_id == org_id, Invitation.created_by == user_id)
    )
    audit(db, org_id, identity.id, "member.removed", user_id)
    db.commit()


@router.post("/organizations/{org_id}/invitations", status_code=201)
def invite(
    org_id: str,
    req: InviteRequest,
    identity: Principal = Depends(human),
    db: Session = Depends(get_db),
):
    role = require_admin(org_id, identity, db)
    if req.agent_limit and not db.scalar(
        select(Room).where(Room.id == req.channel_id, Room.org_id == org_id)
    ):
        raise HTTPException(400, "Choose a starting channel in this organization")
    if req.role == "admin" and role != "owner":
        raise HTTPException(403, "Only the owner can invite administrators")
    rate_limit("invite:" + identity.id, 30, 3600)
    import secrets

    token = secrets.token_urlsafe(32)
    expires = now() + timedelta(days=7)
    db.add(
        Invitation(
            digest=digest(token),
            org_id=org_id,
            role=req.role,
            agent_limit=req.agent_limit,
            channel_id=req.channel_id if req.agent_limit else None,
            expires_at=expires,
            created_by=identity.id,
        )
    )
    audit(db, org_id, identity.id, "invitation.created", digest(token))
    db.commit()
    return {"url": f"{public_origin()}/workspace/#invite={token}", "expires_at": expires}


@router.get("/organizations/{org_id}/invitations")
def invitations(org_id: str, identity: Principal = Depends(human), db: Session = Depends(get_db)):
    require_admin(org_id, identity, db)
    return [
        {
            "id": row.digest,
            "role": row.role,
            "expires_at": row.expires_at,
            "agent_limit": row.agent_limit,
            "channel_id": row.channel_id,
        }
        for row in db.scalars(
            select(Invitation).where(Invitation.org_id == org_id, Invitation.expires_at > now())
        )
    ]


@router.delete("/organizations/{org_id}/invitations/{invitation_id}", status_code=204)
def revoke_invitation(
    org_id: str,
    invitation_id: str,
    identity: Principal = Depends(human),
    db: Session = Depends(get_db),
):
    require_admin(org_id, identity, db)
    row = db.scalar(
        select(Invitation).where(Invitation.org_id == org_id, Invitation.digest == invitation_id)
    )
    if not row:
        raise HTTPException(404, "Invitation not found")
    db.delete(row)
    audit(db, org_id, identity.id, "invitation.revoked", invitation_id)
    db.commit()


def available_invitation(token: str, identity: Principal, db: Session):
    invitation = db.get(Invitation, digest(token))
    if (
        not invitation
        or invitation.expires_at <= now()
        or invitation.claimed_by not in (None, identity.id)
    ):
        raise HTTPException(404, "Invitation is expired or unavailable")
    return invitation


@router.post("/invitations/inspect")
def inspect_invite(
    req: InviteToken, identity: Principal = Depends(human), db: Session = Depends(get_db)
):
    invitation = available_invitation(req.token, identity, db)
    org = db.get(Organization, invitation.org_id)
    room = db.scalar(
        select(Room).where(Room.org_id == invitation.org_id, Room.id == invitation.channel_id)
    )
    return {
        "organization": org.name,
        "role": invitation.role,
        "agent_limit": invitation.agent_limit,
        "channel": room.name if room else None,
        "expires_at": invitation.expires_at,
    }


@router.post("/invitations/accept")
def accept_invite(
    req: AcceptInvite,
    response: Response,
    identity: Principal = Depends(human),
    db: Session = Depends(get_db),
):
    invitation = available_invitation(req.token, identity, db)
    org_id, role, channel_id = invitation.org_id, invitation.role, invitation.channel_id
    if len(req.agents) > invitation.agent_limit:
        raise HTTPException(400, "This invitation does not allow that many agents")
    if req.agents and not db.scalar(
        select(Room).where(Room.org_id == org_id, Room.id == channel_id)
    ):
        raise HTTPException(400, "Starting channel is unavailable; ask for a new invitation")
    # Claim and create the whole batch atomically; retries cannot mint more credentials.
    claimed = db.execute(
        delete(Invitation).where(
            Invitation.digest == invitation.digest,
            Invitation.expires_at > now(),
            or_(Invitation.claimed_by.is_(None), Invitation.claimed_by == identity.id),
        )
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "Invitation was already accepted")
    if not db.get(Member, (org_id, identity.id)):
        db.add(Member(org_id=org_id, user_id=identity.id, role=role))
    agents = []
    for index, spec in enumerate(req.agents, 1):
        agent = Agent(org_id=org_id, name=spec.name, harness=spec.harness, onboarded_by=identity.id)
        db.add(agent)
        db.flush()
        token = issue_credential(db, agent_id=agent.id, days=90)
        agents.append(
            {
                "key": f"agent-{index}",
                "id": agent.id,
                "name": agent.name,
                "harness": spec.harness,
                "token": token,
            }
        )
        audit(db, org_id, identity.id, "agent.created", agent.id)
    audit(db, org_id, identity.id, "invitation.accepted", identity.id)
    result = {"org_id": org_id}
    if agents:
        import json

        bundle = {
            "url": public_origin(),
            "organization": org_id,
            "channel": channel_id,
            "agents": agents,
        }
        template = (Path(__file__).parent / "agent_launcher.py").read_text()
        result["launcher"] = template.replace(
            "BUNDLE = {}", "BUNDLE = json.loads(" + repr(json.dumps(bundle)) + ")", 1
        )
        result["agents"] = [{k: v for k, v in agent.items() if k != "token"} for agent in agents]
        result["expires_in_days"] = 90
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/organizations/{org_id}/agents")
def agents(org_id: str, identity: Principal = Depends(principal), db: Session = Depends(get_db)):
    role_for(org_id, identity, db)
    return [
        {
            "id": a.id,
            "name": a.name,
            "active": a.active,
            "kind": "agent",
            "role": "member",
            "onboarded_by": a.onboarded_by,
            "harness": a.harness,
        }
        for a in db.scalars(select(Agent).where(Agent.org_id == org_id))
    ]


@router.post("/organizations/{org_id}/agents", status_code=201)
def add_agent(
    org_id: str, req: Named, identity: Principal = Depends(human), db: Session = Depends(get_db)
):
    require_admin(org_id, identity, db)
    rate_limit("agent:" + identity.id, 30, 3600)
    agent = Agent(org_id=org_id, name=req.name, onboarded_by=identity.id)
    db.add(agent)
    db.flush()
    token = issue_credential(db, agent_id=agent.id, days=90)
    audit(db, org_id, identity.id, "agent.created", agent.id)
    db.commit()
    return {"id": agent.id, "name": agent.name, "token": token, "expires_in_days": 90}


@router.delete("/organizations/{org_id}/agents/{agent_id}", status_code=204)
def revoke_agent(
    org_id: str, agent_id: str, identity: Principal = Depends(human), db: Session = Depends(get_db)
):
    require_admin(org_id, identity, db)
    agent = db.scalar(select(Agent).where(Agent.org_id == org_id, Agent.id == agent_id))
    if not agent:
        raise HTTPException(404, "Agent not found")
    agent.active = False
    db.execute(delete(Credential).where(Credential.agent_id == agent_id))
    audit(db, org_id, identity.id, "agent.revoked", agent_id)
    db.commit()


@router.get("/organizations/{org_id}/channels")
def channels(org_id: str, identity: Principal = Depends(principal), db: Session = Depends(get_db)):
    role_for(org_id, identity, db)
    return [
        {"id": r.id, "name": r.name}
        for r in db.scalars(select(Room).where(Room.org_id == org_id).order_by(Room.created_at))
    ]


@router.post("/organizations/{org_id}/channels", status_code=201)
def new_channel(
    org_id: str, req: Named, identity: Principal = Depends(human), db: Session = Depends(get_db)
):
    require_admin(org_id, identity, db)
    rate_limit("channel:" + identity.id, 30, 3600)
    room = Room(org_id=org_id, name=req.name)
    db.add(room)
    try:
        db.flush()
        audit(db, org_id, identity.id, "channel.created", room.id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "A channel with that name already exists") from exc
    return {"id": room.id, "name": room.name}


def require_room(org_id, room_id, identity, db):
    role_for(org_id, identity, db)
    room = db.scalar(select(Room).where(Room.org_id == org_id, Room.id == room_id))
    if not room:
        raise HTTPException(404, "Channel not found")
    return room


def message_json(row):
    return {
        "id": row.id,
        "channel_id": row.room_id,
        "sender_id": row.sender_id,
        "sender_name": row.sender_name,
        "sender_kind": row.sender_kind,
        "text": row.text,
        "client_id": row.client_id,
        "created_at": row.created_at.isoformat() + "Z",
    }


def read_messages(db, org_id, room_id, after, limit=100):
    query = select(ChatMessage).where(ChatMessage.org_id == org_id, ChatMessage.room_id == room_id)
    if after is not None:
        rows = db.scalars(
            query.where(ChatMessage.id > after).order_by(ChatMessage.id).limit(limit)
        ).all()
    else:
        rows = list(reversed(db.scalars(query.order_by(ChatMessage.id.desc()).limit(limit)).all()))
    metadata = delivery.details(db, rows)
    return [{**message_json(row), **metadata[row.id]} for row in rows]


@router.get("/organizations/{org_id}/channels/{room_id}/messages")
def history(
    org_id: str,
    room_id: str,
    after: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
    identity: Principal = Depends(principal),
    db: Session = Depends(get_db),
):
    require_room(org_id, room_id, identity, db)
    return read_messages(db, org_id, room_id, after, limit)


@router.post("/organizations/{org_id}/channels/{room_id}/messages", status_code=201)
def send(
    org_id: str,
    room_id: str,
    req: SendMessage,
    identity: Principal = Depends(principal),
    db: Session = Depends(get_db),
):
    require_room(org_id, room_id, identity, db)
    rate_limit("message:" + identity.id, 60)
    existing = db.scalar(
        select(ChatMessage).where(
            ChatMessage.org_id == org_id,
            ChatMessage.sender_id == identity.id,
            ChatMessage.client_id == req.client_id,
        )
    )
    if existing:
        if not delivery.same_request(db, existing, room_id, req):
            raise HTTPException(409, "Message ID already used for different content")
        return {**message_json(existing), **delivery.details(db, [existing])[existing.id]}
    agents = delivery.prepare(db, org_id, room_id, identity, req.recipient_ids, req.reply_to)
    row = ChatMessage(
        org_id=org_id,
        room_id=room_id,
        sender_id=identity.id,
        sender_name=identity.name,
        sender_kind=identity.kind,
        text=req.text,
        client_id=req.client_id,
    )
    db.add(row)
    try:
        db.flush()
        delivery.persist(db, row, agents, req.reply_to)
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.scalar(
            select(ChatMessage).where(
                ChatMessage.org_id == org_id,
                ChatMessage.sender_id == identity.id,
                ChatMessage.client_id == req.client_id,
            )
        )
        if not delivery.same_request(db, row, room_id, req):
            raise HTTPException(409, "Message ID already used for different content") from None
    return {**message_json(row), **delivery.details(db, [row])[row.id]}


@router.get("/organizations/{org_id}/audit")
def audit_history(org_id: str, identity: Principal = Depends(human), db: Session = Depends(get_db)):
    require_admin(org_id, identity, db)
    rows = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.org_id == org_id)
        .order_by(AuditEvent.created_at.desc())
        .limit(100)
    )
    return [
        {
            "action": r.action,
            "actor_id": r.actor_id,
            "target_id": r.target_id,
            "created_at": r.created_at,
        }
        for r in rows
    ]


def stream_batch(token, org_id, room_id, after, bearer=True):
    with SessionLocal() as db:
        identity = authenticate(token, db)
        if identity.kind == "agent" and not bearer:
            raise HTTPException(401, "Agents must use a bearer credential")
        require_room(org_id, room_id, identity, db)
        return read_messages(db, org_id, room_id, after)


async def stream(websocket: WebSocket, org_id: str, room_id: str, after: int = 0):
    origin = websocket.headers.get("origin")
    if (origin and origin != public_origin()) or (
        not origin and not websocket.headers.get("authorization")
    ):
        await websocket.close(code=4403)
        return
    token = token_from(websocket.headers, websocket.cookies)
    bearer = bool(websocket.headers.get("authorization"))
    try:
        rate_limit("stream:" + digest(token), 30)
        initial = await asyncio.to_thread(
            stream_batch, token, org_id, room_id, max(after, 0), bearer
        )
    except HTTPException:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    cursor = max(after, 0)
    try:
        while True:
            await websocket.send_json({"type": "messages", "messages": initial})
            if initial:
                cursor = initial[-1]["id"]
            # A DB cursor makes reconnects reliable without a process-local broadcast cache.
            try:
                event = await asyncio.wait_for(websocket.receive(), timeout=1)
                if event["type"] == "websocket.disconnect":
                    break
                # Client frames never grant authority or inject messages.
                await asyncio.sleep(1)
            except TimeoutError:
                pass
            initial = await asyncio.to_thread(stream_batch, token, org_id, room_id, cursor, bearer)
    except HTTPException:
        await websocket.close(code=4403, reason="Access expired or was revoked")
    except (WebSocketDisconnect, RuntimeError):
        pass


def install(app):
    from agent_ptt import workspace_presence, workspace_speech

    app.include_router(router)
    app.include_router(delivery.router)
    workspace_presence.install(app)
    workspace_speech.install(app)
    app.add_api_websocket_route(
        "/api/workspace/organizations/{org_id}/channels/{room_id}/stream", stream
    )

    @app.get("/workspace/", include_in_schema=False)
    def workspace_ui():
        return FileResponse(STATIC / "workspace.html")

    @app.get("/workspace/assets/{name}", include_in_schema=False)
    def workspace_asset(name: str):
        if name not in {"workspace.js", "workspace.css", "workspace-speech.js"}:
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @app.middleware("http")
    async def workspace_boundary(request: Request, call_next):
        path = request.url.path
        is_workspace = path.startswith("/api/workspace/") or path.startswith("/workspace/")
        if hosted() and path not in {"/", "/health/live", "/health/ready"} and not is_workspace:
            return JSONResponse(
                {"detail": "Local-only endpoint is disabled in hosted mode"}, status_code=404
            )
        if path.startswith("/api/workspace/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            auth_action = path in {"/api/workspace/login", "/api/workspace/signup"}
            if origin != public_origin() and (
                auth_action or not request.headers.get("authorization")
            ):
                return JSONResponse({"detail": "Invalid request origin"}, status_code=403)
            if origin and origin != public_origin():
                return JSONResponse({"detail": "Invalid request origin"}, status_code=403)
        response = await call_next(request)
        if is_workspace:
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; media-src 'self' blob:; "
                "frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'"
            )
        return response
