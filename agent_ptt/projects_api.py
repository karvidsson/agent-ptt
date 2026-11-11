"""HTTP routes for projects and the project inbox (tag "Projects").

Legacy (non-hosted) server only: every route answers 404 when
``AGENT_PTT_HOSTED=1``. All routes sit behind the shared API key; member
routes also need ``X-Member-Key`` (the secret returned only by join).
The rules live in ``projects.py``; docs/api-reference.md lists the shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from agent_ptt import projects as pj
from agent_ptt.auth import require_api_key
from agent_ptt.db import SessionLocal, get_db
from agent_ptt.models import (
    MemberOut,
    MemberPatch,
    ProjectDB,
    ProjectJoinRequest,
    ProjectJoinResponse,
    ProjectMemberDB,
    ProjectMemberOut,
    ProjectOut,
    WorkItemCreate,
    WorkItemNote,
    WorkItemStateChange,
)
from agent_ptt.workspace_auth import hosted


def _legacy_only() -> None:
    if hosted():
        raise HTTPException(404, "Not Found")


router = APIRouter(
    dependencies=[Depends(_legacy_only), Depends(require_api_key)], tags=["Projects"]
)


def _error(exc: pj.ProjectError) -> JSONResponse:
    return JSONResponse({"detail": exc.detail, **exc.extra}, status_code=exc.status)


def _authenticate(db: Session, project_id: str, member_key: str | None) -> ProjectMemberDB:
    if db.get(ProjectDB, project_id) is None:
        raise pj.ProjectError(404, "Project not found")
    member = pj.member_by_key(db, member_key)
    if member is None:
        raise pj.ProjectError(401, "Missing or unknown X-Member-Key")
    if member.project_id != project_id:
        raise pj.ProjectError(403, "This member_key belongs to another project")
    return member


def _project_out(project: ProjectDB) -> dict:
    return ProjectOut(
        project_id=project.project_id,
        name=project.name,
        remote=project.remote,
        channel_id=project.channel_id,
    ).model_dump(mode="json")


def _member_out(member: ProjectMemberDB) -> MemberOut:
    return MemberOut(
        member_id=member.member_id,
        handle=member.handle,
        roles=list(member.roles or []),
        mode=member.mode,
        harness=member.harness,
    )


def _public_member(member: ProjectMemberDB, now: datetime | None = None) -> dict:
    """The roster entry: presence and the mention text; never the member_key."""
    now = now or pj.now_utc()
    return ProjectMemberOut(
        **_member_out(member).model_dump(),
        last_seen=pj.as_utc(member.last_seen),
        listening=pj.listening(member.member_id, now),
        machine=member.machine,
        doing=member.doing,
        doing_at=pj.as_utc(member.doing_at),
        plugin_version=member.plugin_version,
        presence=pj.presence(member, now),
        mention=pj.mention_text(member.handle),
    ).model_dump(mode="json")


def _start_channel_worker(channel_id: str) -> None:
    # server.py owns the TTS workers; imported lazily (it includes this router).
    from agent_ptt import server

    server._start_tts_worker(channel_id)


@router.post("/projects/join")
async def join_project(req: ProjectJoinRequest, db: Session = Depends(get_db)):
    """Join a project (created from the remote on first join) or rejoin by key.

    The only response that carries ``member_key``.
    """
    try:
        result = pj.join_project(db, req)
    except pj.ProjectError as exc:
        return _error(exc)
    if result.channel_created and result.project.channel_id:
        _start_channel_worker(result.project.channel_id)
    await pj.publish(db, result.project, result.effects)
    body = ProjectJoinResponse(
        project=ProjectOut(**_project_out(result.project)),
        member=_member_out(result.member),
        member_key=result.member.member_key,
    ).model_dump(mode="json")
    body["member"] = _public_member(result.member)
    body["resumed"] = result.resumed
    return body


@router.get("/projects")
async def list_projects(db: Session = Depends(get_db)):
    """Every project with its member count and open (unfinished) item count."""
    out = []
    for project in db.query(ProjectDB).order_by(ProjectDB.created_at).all():
        out.append(
            {
                **_project_out(project),
                "member_count": db.query(ProjectMemberDB)
                .filter_by(project_id=project.project_id)
                .count(),
                "open_items": pj.open_item_count(db, project.project_id),
            }
        )
    return {"projects": out}


@router.get("/projects/{project_id}")
async def project_detail(project_id: str, db: Session = Depends(get_db)):
    """The project, its members (with presence) and active resource claims."""
    await pj.expire_and_publish(db)
    project = db.get(ProjectDB, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    now = pj.now_utc()
    members = [_public_member(member, now) for member in pj.project_members(db, project_id)]
    return {
        **_project_out(project),
        "members": members,
        "resource_claims": pj.serialize_items(db, pj.active_resource_claims(db, project_id)),
    }


@router.patch("/projects/{project_id}/members/me")
async def update_me(
    project_id: str,
    req: MemberPatch,
    x_member_key: str | None = Header(default=None, alias="X-Member-Key"),
    db: Session = Depends(get_db),
):
    """Update your own presence (``doing``, ``machine``) or ``mode`` / ``roles``.

    ``doing: ""`` clears it. Counts as seen. Returns your public roster entry.
    """
    try:
        member = _authenticate(db, project_id, x_member_key)
        effects = pj.update_member(db, member, req)
    except pj.ProjectError as exc:
        return _error(exc)
    await pj.publish(db, db.get(ProjectDB, project_id), effects)
    return _public_member(member)


@router.post("/projects/{project_id}/items", status_code=201)
async def create_item(
    project_id: str,
    req: WorkItemCreate,
    x_member_key: str | None = Header(default=None, alias="X-Member-Key"),
    db: Session = Depends(get_db),
):
    """Send a work item; 200 with the existing item on a ``client_id`` replay."""
    try:
        member = _authenticate(db, project_id, x_member_key)
        result = pj.create_item(db, member, req)
    except pj.ProjectError as exc:
        return _error(exc)
    project = db.get(ProjectDB, project_id)
    await pj.publish(db, project, result.effects)
    body = pj.serialize_item(db, result.item)
    return body if result.created else JSONResponse(body, status_code=200)


@router.get("/projects/{project_id}/inbox")
async def inbox(
    project_id: str,
    wait: float = Query(default=0, ge=0, le=60),
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    wake_only: bool = Query(default=False),
    x_member_key: str | None = Header(default=None, alias="X-Member-Key"),
):
    """Every unacked delivery for the caller, oldest first (at-least-once).

    With ``wait`` > 0 and no unacked delivery past ``after``, long-polls until
    one arrives or the wait ends. ``wake_only=1`` leaves out FYI items: they
    neither end the wait nor are returned, and stay for an ordinary read.
    No DB session is held while waiting.
    """
    with SessionLocal() as db:
        try:
            member = _authenticate(db, project_id, x_member_key)
        except pj.ProjectError as exc:
            return _error(exc)
        member_id = member.member_id
    try:
        items, cursor = await pj.wait_for_inbox(
            member_id, project_id, after, limit, wait, wake_only
        )
    except pj.ProjectError as exc:
        return _error(exc)
    return {"items": items, "cursor": cursor}


async def _member_action(project_id: str, x_member_key: str | None, db: Session, action):
    await pj.expire_and_publish(db)
    try:
        member = _authenticate(db, project_id, x_member_key)
        result = action(member)
    except pj.ProjectError as exc:
        return _error(exc)
    item, effects = result if isinstance(result, tuple) else (result, pj.Effects())
    await pj.publish(db, db.get(ProjectDB, project_id), effects)
    return pj.serialize_item(db, item)


MemberKey = Header(default=None, alias="X-Member-Key")


@router.post("/projects/{project_id}/items/{item_id}/ack")
async def ack_item(
    project_id: str,
    item_id: str,
    x_member_key: str | None = MemberKey,
    db: Session = Depends(get_db),
):
    """Acknowledge the caller's delivery (it stops coming back in the inbox)."""
    return await _member_action(project_id, x_member_key, db, lambda m: pj.ack_item(db, m, item_id))


@router.post("/projects/{project_id}/items/{item_id}/claim")
async def claim_item(
    project_id: str,
    item_id: str,
    x_member_key: str | None = MemberKey,
    db: Session = Depends(get_db),
):
    """Claim an open item; 409 with the holder when someone else has it."""
    return await _member_action(
        project_id, x_member_key, db, lambda m: pj.claim_item(db, m, item_id)
    )


@router.post("/projects/{project_id}/items/{item_id}/release")
async def release_item(
    project_id: str,
    item_id: str,
    req: WorkItemNote | None = None,
    x_member_key: str | None = MemberKey,
    db: Session = Depends(get_db),
):
    """Give a claimed item back: it returns to open and is redelivered."""
    note = req.note if req else None
    return await _member_action(
        project_id, x_member_key, db, lambda m: pj.release_item(db, m, item_id, note)
    )


@router.post("/projects/{project_id}/items/{item_id}/state")
async def set_item_state(
    project_id: str,
    item_id: str,
    req: WorkItemStateChange,
    x_member_key: str | None = MemberKey,
    db: Session = Depends(get_db),
):
    """The claimant marks the item ``waiting`` or ``review``, or resumes (``claimed``)."""
    return await _member_action(
        project_id,
        x_member_key,
        db,
        lambda m: pj.set_item_state(db, m, item_id, req.state, req.note),
    )


@router.post("/projects/{project_id}/items/{item_id}/done")
async def done_item(
    project_id: str,
    item_id: str,
    req: WorkItemNote | None = None,
    x_member_key: str | None = MemberKey,
    db: Session = Depends(get_db),
):
    """Finish the item (final)."""
    note = req.note if req else None
    return await _member_action(
        project_id, x_member_key, db, lambda m: pj.finish_item(db, m, item_id, "done", note)
    )


@router.post("/projects/{project_id}/items/{item_id}/decline")
async def decline_item(
    project_id: str,
    item_id: str,
    req: WorkItemNote | None = None,
    x_member_key: str | None = MemberKey,
    db: Session = Depends(get_db),
):
    """Decline the item (final)."""
    note = req.note if req else None
    return await _member_action(
        project_id, x_member_key, db, lambda m: pj.finish_item(db, m, item_id, "declined", note)
    )


@router.get("/projects/{project_id}/items/{item_id}")
async def item_detail(project_id: str, item_id: str, db: Session = Depends(get_db)):
    """The item, its thread (ancestors, the item, every reply) and its events."""
    await pj.expire_and_publish(db)
    if db.get(ProjectDB, project_id) is None:
        raise HTTPException(404, "Project not found")
    try:
        return pj.item_thread(db, project_id, item_id)
    except pj.ProjectError as exc:
        return _error(exc)


@router.get("/work-items")
async def work_items(
    updated_after: datetime | None = None,
    project_id: str | None = None,
    member_id: str | None = None,
    include_done_days: int = Query(default=7, ge=0, le=365),
    db: Session = Depends(get_db),
):
    """Items across projects for the Board; poll with ``updated_after=<server_time>``."""
    await pj.expire_and_publish(db)
    server_time = pj.now_utc()
    if updated_after is not None:
        if updated_after.tzinfo is None:
            updated_after = updated_after.replace(tzinfo=UTC)
        updated_after = updated_after.astimezone(UTC)
    items = pj.board_items(db, updated_after, project_id, member_id, include_done_days)
    return {
        "items": pj.serialize_items(db, items),
        "server_time": server_time.isoformat().replace("+00:00", "Z"),
    }
