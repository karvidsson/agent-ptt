"""Reusable organization enrollment, independent of channel selection."""

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_ptt.db import get_db
from agent_ptt.workspace import InviteToken, Named, audit, auth_limit
from agent_ptt.workspace_auth import (
    Principal,
    digest,
    human,
    public_origin,
    rate_limit,
    require_admin,
)
from agent_ptt.workspace_models import (
    Agent,
    AgentEnrollment,
    Credential,
    JoinLink,
    Member,
    Organization,
    now,
)

router = APIRouter(prefix="/api/workspace", tags=["Organization join links"])


class EnrollAgent(InviteToken, Named):
    # Generated and saved by the client before enrollment. Retrying a lost
    # response uses the same secret; the server stores only its digest.
    credential: str = Field(min_length=43, max_length=100, pattern=r"^[A-Za-z0-9_-]+$", repr=False)


def lock_link(db: Session, token: str) -> JoinLink:
    token_digest = digest(token)
    # An UPDATE locks the link on both SQLite and PostgreSQL. Enrollment and
    # revocation/rotation serialize on the same row for the whole transaction.
    result = db.execute(
        update(JoinLink)
        .where(JoinLink.digest == token_digest, JoinLink.active.is_(True))
        .values(active=True)
    )
    if result.rowcount != 1:
        raise HTTPException(
            404, "Join link is unavailable; ask an administrator for the current link"
        )
    return db.scalar(select(JoinLink).where(JoinLink.digest == token_digest))


def join_human(db, link, user_id):
    if not db.get(Member, (link.org_id, user_id)):
        db.add(Member(org_id=link.org_id, user_id=user_id, role="member"))
        audit(db, link.org_id, user_id, "join_link.accepted", link.digest)


@router.get("/organizations/{org_id}/join-link")
def link_status(org_id: str, identity: Principal = Depends(human), db: Session = Depends(get_db)):
    require_admin(org_id, identity, db)
    link = db.get(JoinLink, org_id)
    return {"active": bool(link and link.active), "created_at": link.created_at if link else None}


@router.post("/organizations/{org_id}/join-link", status_code=201)
def rotate_link(org_id: str, identity: Principal = Depends(human), db: Session = Depends(get_db)):
    require_admin(org_id, identity, db)
    rate_limit("join-link:" + identity.id, 30, 3600)
    token = secrets.token_urlsafe(32)
    values = dict(digest=digest(token), active=True, created_by=identity.id, created_at=now())
    changed = db.execute(update(JoinLink).where(JoinLink.org_id == org_id).values(**values))
    if not changed.rowcount:
        db.add(JoinLink(org_id=org_id, **values))
    audit(db, org_id, identity.id, "join_link.rotated", values["digest"])
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Another administrator changed the link; try again") from exc
    return {"url": f"{public_origin()}/workspace/#join={token}"}


@router.delete("/organizations/{org_id}/join-link", status_code=204)
def revoke_link(org_id: str, identity: Principal = Depends(human), db: Session = Depends(get_db)):
    require_admin(org_id, identity, db)
    db.execute(update(JoinLink).where(JoinLink.org_id == org_id).values(active=False))
    audit(db, org_id, identity.id, "join_link.revoked", org_id)
    db.commit()


@router.post("/join/inspect")
def inspect_link(req: InviteToken, request: Request, db: Session = Depends(get_db)):
    auth_limit(request)
    link = db.scalar(select(JoinLink).where(JoinLink.digest == digest(req.token), JoinLink.active))
    if not link:
        raise HTTPException(404, "Join link is unavailable")
    return {"organization": db.get(Organization, link.org_id).name, "role": "member"}


@router.post("/join/person")
def accept_person(
    req: InviteToken, identity: Principal = Depends(human), db: Session = Depends(get_db)
):
    link = lock_link(db, req.token)
    join_human(db, link, identity.id)
    db.commit()
    return {"org_id": link.org_id}


@router.post("/join/agent")
def accept_agent(req: EnrollAgent, request: Request, db: Session = Depends(get_db)):
    auth_limit(request)
    if secrets.compare_digest(req.token, req.credential):
        raise HTTPException(400, "Generate a separate random agent credential")
    link = lock_link(db, req.token)
    key = digest(req.credential)
    enrollment = db.get(AgentEnrollment, key)
    if enrollment:
        agent = db.get(Agent, enrollment.agent_id)
        credential = db.get(Credential, key)
        if enrollment.link_digest != link.digest or agent.org_id != link.org_id:
            raise HTTPException(409, "Use a separate credential for this enrollment")
        if not agent.active or not credential or credential.expires_at <= now():
            raise HTTPException(
                403, "Agent access expired or was revoked; contact an administrator"
            )
        if agent.name != req.name:
            raise HTTPException(409, "This enrollment already belongs to a differently named agent")
    else:
        if db.get(Credential, key):
            raise HTTPException(409, "Use a separate credential for this enrollment")
        agent = Agent(org_id=link.org_id, name=req.name)
        db.add(agent)
        db.flush()
        credential = Credential(
            digest=key, agent_id=agent.id, expires_at=now() + timedelta(days=90)
        )
        db.add(credential)
        db.add(AgentEnrollment(credential_digest=key, agent_id=agent.id, link_digest=link.digest))
        audit(db, link.org_id, agent.id, "agent.enrolled", link.digest)
    result = {
        "id": agent.id,
        "name": agent.name,
        "org_id": agent.org_id,
        "expires_at": credential.expires_at,
        "status": "registered",
    }
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Enrollment conflicted; retry with the same credential") from exc
    return result
