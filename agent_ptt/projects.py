"""Projects and the project inbox — where agents hand work to each other.

A project is keyed by its normalized git remote and owns a default channel.
Members (agents or humans) join it for a stable ``member_id`` and a secret
``member_key``. Work items are addressed to one member, a role or the whole
project; each matching member gets a *delivery* (its inbox entry, with a
monotonic ``seq`` cursor). Claimable items are taken with an atomic claim that
holds a lease; an expired or released lease puts the item back to open and
redelivers it. Every creation and state change posts a text-only notice in
the project's channel. See docs/projects.md for the concepts.

The functions here are called from the HTTP routes in ``projects_api.py`` on
the event loop; they take a SQLAlchemy session, and the async ones post
channel notices and wake long-polling inbox readers after committing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from agent_ptt import channel as ch
from agent_ptt.models import (
    WORK_ITEM_TITLE_MAX,
    ChannelDB,
    EventRef,
    MemberRef,
    ProjectDB,
    ProjectJoinRequest,
    ProjectMemberDB,
    WorkItemCreate,
    WorkItemDB,
    WorkItemDeliveryDB,
    WorkItemEventDB,
    WorkItemEventOut,
    WorkItemOut,
)

logger = logging.getLogger(__name__)

CLAIMABLE_KINDS = {"instruction", "handoff", "question"}
OPEN_STATES = ("pending", "delivered", "acked")
HELD_STATES = ("claimed", "waiting", "review")
FINAL_STATES = ("done", "declined")
DEFAULT_LEASE_SECONDS = 1800
LEASE_CHECK_SECONDS = 30.0
LISTENING_SECONDS = 90.0
SERVER_HANDLE = "agent-ptt"


class ProjectError(Exception):
    """An inbox rule the caller broke; mapped to an HTTP status by the routes."""

    def __init__(self, status: int, detail: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra or {}


def now_utc() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; they were always UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def lease_seconds() -> int:
    """Claim lease length: ``AGENT_PTT_CLAIM_LEASE_SECONDS`` or 30 minutes."""
    raw = os.environ.get("AGENT_PTT_CLAIM_LEASE_SECONDS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_LEASE_SECONDS
    except ValueError:
        value = DEFAULT_LEASE_SECONDS
    return value if value > 0 else DEFAULT_LEASE_SECONDS


# ---------------------------------------------------------------------------
# Remote normalization
# ---------------------------------------------------------------------------

_SCP_FORM = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$")


def normalize_remote(remote: str) -> str:
    """Canonical form of a git remote: ``host/owner/repo``, lowercased.

    Strips the scheme, ``user@``, port, trailing ``.git`` and ``/``; the scp
    form ``git@host:owner/repo`` becomes ``host/owner/repo``. So
    ``git@github.com:Owner/Repo.git`` and ``https://github.com/owner/repo``
    are the same project.
    """
    value = remote.strip()
    if "://" in value:
        value = value.split("://", 1)[1]
        host, _, path = value.partition("/")
        host = host.rsplit("@", 1)[-1]
        host = host.split(":", 1)[0]
        value = f"{host}/{path}" if path else host
    else:
        match = _SCP_FORM.match(value)
        if match:
            value = f"{match.group(1)}/{match.group(2)}"
    value = value.rstrip("/")
    if value.endswith(".git"):
        value = value[: -len(".git")]
    value = re.sub(r"/{2,}", "/", value.rstrip("/"))
    return value.lower()


def default_project_name(normalized_remote: str) -> str:
    return normalized_remote.rstrip("/").rsplit("/", 1)[-1] or normalized_remote


# ---------------------------------------------------------------------------
# Effects: notices and wake-ups, published after the commit
# ---------------------------------------------------------------------------


@dataclass
class Effects:
    """Side effects of an operation, applied once its changes are committed."""

    # (item_id or None, author handle, author member_id or None, text, is creation notice)
    notices: list[tuple[str | None, str, str | None, str, bool]] = field(default_factory=list)
    wake: set[str] = field(default_factory=set)

    def notice(
        self, text: str, handle: str, member_id: str | None, item_id: str | None, creation=False
    ) -> None:
        self.notices.append((item_id, handle, member_id, text, creation))


_member_events: dict[str, asyncio.Event] = {}
_active_waits: dict[str, int] = {}
_last_inbox_read: dict[str, datetime] = {}


def _event_for(member_id: str) -> asyncio.Event:
    event = _member_events.get(member_id)
    if event is None:
        event = _member_events[member_id] = asyncio.Event()
    return event


def wake(member_ids: Iterable[str]) -> None:
    """Wake every long-poll waiting on these members' inboxes."""
    for member_id in member_ids:
        event = _member_events.get(member_id)
        if event is not None:
            event.set()


def listening(member_id: str, now: datetime | None = None) -> bool:
    """A member is listening while it long-polls or read its inbox recently."""
    if _active_waits.get(member_id):
        return True
    last = _last_inbox_read.get(member_id)
    return last is not None and ((now or now_utc()) - last).total_seconds() < LISTENING_SECONDS


def reset_runtime_state() -> None:
    """Forget in-memory waiters (tests)."""
    _member_events.clear()
    _active_waits.clear()
    _last_inbox_read.clear()


def _notice_sender(member_id: str | None) -> str:
    # A public, non-secret author tag; never a member_key.
    return f"project-member:{member_id}" if member_id else "project-server"


async def publish(db: Session, project: ProjectDB, effects: Effects) -> None:
    """Post the collected channel notices, then wake inbox long-polls."""
    channel_id = project.channel_id
    for item_id, handle, member_id, text, creation in effects.notices:
        if not channel_id:
            break
        try:
            msg = await ch.post_notice(channel_id, handle, text, _notice_sender(member_id), db)
        except Exception as exc:  # Notices are best-effort; the item is committed.
            logger.warning("Project notice failed: %s", exc)
            continue
        if msg is not None and creation and item_id:
            item = db.get(WorkItemDB, item_id)
            if item is not None and item.notice_message_id is None:
                item.notice_message_id = msg.message_id
                db.commit()
    wake(effects.wake)


# ---------------------------------------------------------------------------
# Projects and members
# ---------------------------------------------------------------------------


def _ensure_channel(db: Session, project: ProjectDB) -> bool:
    """Point the project at a live channel; returns True if a channel was created.

    Reuses the existing (unarchived) channel named after the project, so a
    team already talking in "agent-ptt" keeps its room. A new channel row is
    persisted in ``db``'s transaction, committed with the join.
    """
    if project.channel_id:
        current = ch.get_channel(project.channel_id)
        if current is not None and not current.archived:
            return False
    for existing in ch.list_channels():
        if existing.name == project.name:
            project.channel_id = existing.channel_id
            return False
    created = ch.create_channel(project.name)
    db.merge(
        ChannelDB(channel_id=created.channel_id, name=created.name, created_at=created.created_at)
    )
    project.channel_id = created.channel_id
    return True


def member_by_key(db: Session, member_key: str | None) -> ProjectMemberDB | None:
    if not member_key:
        return None
    return db.query(ProjectMemberDB).filter_by(member_key=member_key).first()


def get_project(db: Session, project_id: str) -> ProjectDB | None:
    return db.get(ProjectDB, project_id)


@dataclass
class JoinResult:
    project: ProjectDB
    member: ProjectMemberDB
    channel_created: bool
    effects: Effects


def join_project(db: Session, req: ProjectJoinRequest) -> JoinResult:
    """Find or create the project, then create or update the member."""
    rejoining = member_by_key(db, req.member_key) if req.member_key else None
    if req.member_key and rejoining is None:
        raise ProjectError(401, "Unknown member_key")

    project: ProjectDB | None = None
    if req.project_id:
        project = db.get(ProjectDB, req.project_id)
        if project is None:
            raise ProjectError(404, "Project not found")
    elif rejoining is not None and not req.remote:
        project = db.get(ProjectDB, rejoining.project_id)
    if project is None and req.remote:
        remote = normalize_remote(req.remote)
        if not remote:
            raise ProjectError(422, "remote is empty after normalization")
        project = db.query(ProjectDB).filter_by(remote=remote).first()
        if project is None:
            project = ProjectDB(name=req.name or default_project_name(remote), remote=remote)
            db.add(project)
    if project is None and req.name:
        project = (
            db.query(ProjectDB).filter_by(name=req.name).order_by(ProjectDB.created_at).first()
        )
        if project is None:
            project = ProjectDB(name=req.name, remote=None)
            db.add(project)
    assert project is not None
    db.flush()

    if rejoining is not None and rejoining.project_id != project.project_id:
        raise ProjectError(403, "member_key belongs to another project")

    channel_created = _ensure_channel(db, project)
    now = now_utc()
    if rejoining is not None:
        member = rejoining
        member.handle = req.handle
        if req.roles is not None:
            member.roles = list(req.roles)
        if req.mode is not None:
            member.mode = req.mode
        if req.harness is not None:
            member.harness = req.harness
        member.last_seen = now
    else:
        member = ProjectMemberDB(
            project_id=project.project_id,
            member_key=secrets.token_urlsafe(32),
            handle=req.handle,
            roles=list(req.roles or []),
            mode=req.mode or "monitor",
            harness=req.harness,
            created_at=now,
            last_seen=now,
        )
        db.add(member)
    db.flush()
    effects = Effects()
    _deliver_open_items_to(db, member, effects)
    db.commit()
    return JoinResult(project, member, channel_created, effects)


def project_members(db: Session, project_id: str) -> list[ProjectMemberDB]:
    return (
        db.query(ProjectMemberDB)
        .filter_by(project_id=project_id)
        .order_by(ProjectMemberDB.created_at)
        .all()
    )


def touch_member(member: ProjectMemberDB) -> None:
    member.last_seen = now_utc()


# ---------------------------------------------------------------------------
# Addressing and deliveries
# ---------------------------------------------------------------------------


def _matches(member: ProjectMemberDB, to_address: str) -> bool:
    if to_address == "project":
        return True
    scope, _, target = to_address.partition(":")
    if scope == "role":
        return target in (member.roles or [])
    if scope == "agent":
        return target == member.member_id
    return False


def recipients(db: Session, item: WorkItemDB) -> list[ProjectMemberDB]:
    return [
        member
        for member in project_members(db, item.project_id)
        if member.member_id != item.from_member_id and _matches(member, item.to_address)
    ]


def resolve_address(db: Session, project_id: str, to: str) -> str:
    """Canonical ``to``: ``agent:<member_id>``, ``role:<role>`` or ``project``."""
    to = to.strip()
    if to == "project":
        return to
    scope, sep, target = to.partition(":")
    target = target.strip()
    if not sep or not target or scope not in ("agent", "role"):
        raise ProjectError(422, "to must be agent:<member>, role:<role> or project")
    if scope == "role":
        return f"role:{target}"
    members = project_members(db, project_id)
    for member in members:
        if member.member_id == target:
            return f"agent:{member.member_id}"
    matches = [m for m in members if m.handle.lower() == target.lower()]
    if len(matches) == 1:
        return f"agent:{matches[0].member_id}"
    if matches:
        raise ProjectError(422, f"handle {target!r} is ambiguous in this project; use member_id")
    raise ProjectError(422, f"no member {target!r} in this project")


def _upsert_deliveries(
    db: Session, item: WorkItemDB, members: Iterable[ProjectMemberDB], reset: bool
) -> set[str]:
    """Create (or, with ``reset``, re-open) deliveries; returns member_ids to wake.

    A reset delivery gets a fresh ``seq`` so long-polls with an older cursor
    see it as new.
    """
    woken: set[str] = set()
    for member in members:
        existing = (
            db.query(WorkItemDeliveryDB)
            .filter_by(item_id=item.item_id, member_id=member.member_id)
            .first()
        )
        if existing is None:
            db.add(WorkItemDeliveryDB(item_id=item.item_id, member_id=member.member_id))
            woken.add(member.member_id)
        elif reset and existing.state != "pending":
            db.delete(existing)
            db.flush()
            db.add(WorkItemDeliveryDB(item_id=item.item_id, member_id=member.member_id))
            woken.add(member.member_id)
    db.flush()
    return woken


def _deliver_open_items_to(db: Session, member: ProjectMemberDB, effects: Effects) -> None:
    """Late joiners (and new roles) get the still-open project and role items."""
    addresses = ["project", *(f"role:{role}" for role in member.roles or [])]
    items = (
        db.query(WorkItemDB)
        .filter(
            WorkItemDB.project_id == member.project_id,
            WorkItemDB.to_address.in_(addresses),
            WorkItemDB.state.in_(OPEN_STATES),
            WorkItemDB.from_member_id != member.member_id,
        )
        .all()
    )
    for item in items:
        effects.wake |= _upsert_deliveries(db, item, [member], reset=False)


# ---------------------------------------------------------------------------
# Events and state
# ---------------------------------------------------------------------------


def _event(
    db: Session,
    item: WorkItemDB,
    action: str,
    by: str | None,
    from_state: str | None,
    to_state: str | None,
    note: str | None = None,
    at: datetime | None = None,
) -> None:
    db.add(
        WorkItemEventDB(
            item_id=item.item_id,
            at=at or now_utc(),
            by_member_id=by,
            action=action,
            from_state=from_state,
            to_state=to_state,
            note=note,
        )
    )


def _set_state(
    db: Session,
    item: WorkItemDB,
    state: str,
    action: str,
    by: str | None,
    note: str | None = None,
) -> None:
    now = now_utc()
    previous = item.state
    item.state = state
    item.updated_at = now
    _event(db, item, action, by, previous, state, note, at=now)


def _renew(item: WorkItemDB) -> None:
    item.lease_expires_at = now_utc() + timedelta(seconds=lease_seconds())


def _quote(title: str) -> str:
    return f'"{title}"'


def _reopen(
    db: Session, item: WorkItemDB, action: str, by: str | None, note: str | None, effects: Effects
) -> None:
    """Back to ``pending``: clear the assignee and redeliver to every recipient."""
    item.assignee_member_id = None
    item.lease_expires_at = None
    _set_state(db, item, "pending", action, by, note)
    effects.wake |= _upsert_deliveries(db, item, recipients(db, item), reset=True)


def expire_leases(db: Session, now: datetime | None = None) -> dict[str, Effects]:
    """Return every held item whose lease ran out to ``pending``.

    Returns the effects per project_id so the caller can publish them.
    """
    now = now or now_utc()
    expired = (
        db.query(WorkItemDB)
        .filter(
            WorkItemDB.state.in_(HELD_STATES),
            WorkItemDB.lease_expires_at.is_not(None),
            WorkItemDB.lease_expires_at < now,
        )
        .all()
    )
    by_project: dict[str, Effects] = {}
    for item in expired:
        effects = by_project.setdefault(item.project_id, Effects())
        _reopen(db, item, "lease_expired", None, None, effects)
        effects.notice(
            f"{_quote(item.title)} went back to open (lease expired)",
            SERVER_HANDLE,
            None,
            item.item_id,
        )
    if expired:
        db.commit()
    return by_project


async def expire_and_publish(db: Session) -> int:
    """Expire leases and publish the notices; returns how many items reopened."""
    by_project = expire_leases(db)
    count = 0
    for project_id, effects in by_project.items():
        count += len(effects.notices)
        project = db.get(ProjectDB, project_id)
        if project is not None:
            await publish(db, project, effects)
    return count


async def lease_expiry_loop(interval: float = LEASE_CHECK_SECONDS) -> None:
    """Background task (server lifespan): expire claim leases periodically."""
    from agent_ptt.db import SessionLocal

    while True:
        await asyncio.sleep(interval)
        try:
            with SessionLocal() as db:
                await expire_and_publish(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Lease expiry error: {exc}")


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def _item_in_project(db: Session, project_id: str, item_id: str) -> WorkItemDB:
    item = db.get(WorkItemDB, item_id)
    if item is None or item.project_id != project_id:
        raise ProjectError(404, "Work item not found in this project")
    return item


def _default_title(body: str) -> str:
    first = body.strip().splitlines()[0].strip() if body.strip() else ""
    return first[:WORK_ITEM_TITLE_MAX]


def _address_label(db: Session, to_address: str) -> str:
    if to_address.startswith("agent:"):
        member = db.get(ProjectMemberDB, to_address.removeprefix("agent:"))
        return f"agent:{member.handle}" if member else to_address
    return to_address


def _open_question_count(db: Session, item_id: str, excluding: str | None = None) -> int:
    query = db.query(WorkItemDB).filter(
        WorkItemDB.reply_to == item_id,
        WorkItemDB.kind == "question",
        WorkItemDB.state.not_in(FINAL_STATES),
    )
    if excluding:
        query = query.filter(WorkItemDB.item_id != excluding)
    return query.count()


@dataclass
class CreateResult:
    item: WorkItemDB
    created: bool
    effects: Effects


def create_item(db: Session, member: ProjectMemberDB, req: WorkItemCreate) -> CreateResult:
    """Validate, address and store a new work item with its deliveries."""
    effects = Effects()
    if req.client_id:
        existing = (
            db.query(WorkItemDB)
            .filter_by(from_member_id=member.member_id, client_id=req.client_id)
            .first()
        )
        if existing is not None:
            return CreateResult(existing, False, effects)

    parent: WorkItemDB | None = None
    if req.reply_to:
        parent = db.get(WorkItemDB, req.reply_to)
        if parent is None or parent.project_id != member.project_id:
            raise ProjectError(422, "reply_to is not an item in this project")

    if req.kind == "answer" and (parent is None or parent.kind != "question"):
        raise ProjectError(422, "an answer needs reply_to pointing at a question")
    context = req.context
    if req.kind == "claim" and not (context and str(context.get("resource") or "").strip()):
        raise ProjectError(422, "a claim needs context.resource (e.g. branch:fix-login)")
    if req.kind == "release" and (parent is None or parent.kind != "claim"):
        raise ProjectError(422, "a release needs reply_to pointing at a claim")

    if req.to:
        to_address = resolve_address(db, member.project_id, req.to)
    elif req.kind in ("question", "answer") and parent is not None:
        to_address = f"agent:{parent.from_member_id}"
    elif req.kind == "release" and parent is not None:
        to_address = parent.to_address
    elif req.kind in ("claim", "note"):
        to_address = "project"
    else:
        raise ProjectError(422, "to is required (agent:<member>, role:<role> or project)")

    now = now_utc()
    item = WorkItemDB(
        item_id=str(uuid.uuid4()),
        project_id=member.project_id,
        kind=req.kind,
        to_address=to_address,
        from_member_id=member.member_id,
        title=(req.title or "").strip()[:WORK_ITEM_TITLE_MAX] or _default_title(req.body),
        body=req.body,
        context=context,
        reply_to=parent.item_id if parent else None,
        client_id=req.client_id,
        state="pending",
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    db.flush()
    _event(db, item, "created", member.member_id, None, "pending", at=now)
    effects.wake |= _upsert_deliveries(db, item, recipients(db, item), reset=False)
    effects.notice(
        f"{member.handle} → {_address_label(db, to_address)} · {item.kind} · {item.title}",
        member.handle,
        member.member_id,
        item.item_id,
        creation=True,
    )

    if parent is not None:
        if parent.assignee_member_id == member.member_id and parent.state in HELD_STATES:
            _renew(parent)
        if (
            req.kind == "question"
            and parent.assignee_member_id == member.member_id
            and parent.state in ("claimed", "review")
        ):
            _set_state(db, parent, "waiting", "waiting", member.member_id, item.title)
            effects.notice(
                f"{member.handle} is waiting on {_quote(parent.title)}",
                member.handle,
                member.member_id,
                parent.item_id,
            )
        if req.kind == "answer":
            _settle_question(db, parent, item, member, effects)
        if req.kind == "release" and parent.state not in FINAL_STATES:
            _set_state(db, parent, "done", "released", member.member_id, item.title)
            effects.notice(
                f"{member.handle} released {_quote(parent.title)}",
                member.handle,
                member.member_id,
                parent.item_id,
            )
    touch_member(member)
    db.commit()
    return CreateResult(item, True, effects)


def _settle_question(
    db: Session,
    question: WorkItemDB,
    answer: WorkItemDB,
    member: ProjectMemberDB,
    effects: Effects,
) -> None:
    if question.state not in FINAL_STATES:
        question.assignee_member_id = question.assignee_member_id or member.member_id
        question.lease_expires_at = None
        _set_state(db, question, "done", "answered", member.member_id, answer.title)
    if not question.reply_to:
        return
    parent = db.get(WorkItemDB, question.reply_to)
    if parent is None or parent.state != "waiting":
        return
    db.flush()
    if _open_question_count(db, parent.item_id) == 0:
        _set_state(db, parent, "claimed", "resumed", member.member_id)
        if parent.assignee_member_id:
            _renew(parent)
            effects.wake.add(parent.assignee_member_id)
        assignee = db.get(ProjectMemberDB, parent.assignee_member_id or "")
        who = assignee.handle if assignee else member.handle
        effects.notice(
            f"{who} is back on {_quote(parent.title)}",
            who,
            assignee.member_id if assignee else member.member_id,
            parent.item_id,
        )


def _delivery(db: Session, item: WorkItemDB, member: ProjectMemberDB) -> WorkItemDeliveryDB | None:
    return (
        db.query(WorkItemDeliveryDB)
        .filter_by(item_id=item.item_id, member_id=member.member_id)
        .first()
    )


def _ack_delivery(db: Session, item: WorkItemDB, member: ProjectMemberDB) -> bool:
    """Ack the member's delivery; returns True if it changed."""
    delivery = _delivery(db, item, member)
    if delivery is None or delivery.state == "acked":
        return False
    now = now_utc()
    delivery.state = "acked"
    delivery.acked_at = now
    delivery.delivered_at = delivery.delivered_at or now
    return True


def _related(item: WorkItemDB, member: ProjectMemberDB, db: Session) -> bool:
    return (
        item.from_member_id == member.member_id
        or item.assignee_member_id == member.member_id
        or _delivery(db, item, member) is not None
    )


def ack_item(db: Session, member: ProjectMemberDB, item_id: str) -> WorkItemDB:
    item = _item_in_project(db, member.project_id, item_id)
    if _delivery(db, item, member) is None:
        raise ProjectError(403, "This item was not delivered to you")
    if _ack_delivery(db, item, member):
        if item.state in ("pending", "delivered"):
            _set_state(db, item, "acked", "acked", member.member_id)
        else:
            _event(db, item, "acked", member.member_id, item.state, item.state)
    if item.assignee_member_id == member.member_id and item.state in HELD_STATES:
        _renew(item)
    touch_member(member)
    db.commit()
    return item


def _held_by(item: WorkItemDB, db: Session) -> dict[str, Any]:
    holder = db.get(ProjectMemberDB, item.assignee_member_id or "")
    return {
        "assignee": {
            "member_id": holder.member_id if holder else item.assignee_member_id,
            "handle": holder.handle if holder else None,
        }
    }


def _claim_checks(db: Session, item: WorkItemDB, member: ProjectMemberDB) -> None:
    if item.kind not in CLAIMABLE_KINDS or (item.kind == "question" and item.reply_to):
        raise ProjectError(422, f"{item.kind} items are not claimable")
    if item.to_address.startswith("agent:") and item.to_address != f"agent:{member.member_id}":
        raise ProjectError(403, "This item is addressed to another member")
    if _delivery(db, item, member) is None:
        raise ProjectError(403, "This item was not delivered to you")


def claim_item(db: Session, member: ProjectMemberDB, item_id: str) -> tuple[WorkItemDB, Effects]:
    """Atomically claim an open item: the first claim wins and holds a lease."""
    effects = Effects()
    item = _item_in_project(db, member.project_id, item_id)
    _claim_checks(db, item, member)
    if item.assignee_member_id == member.member_id and item.state in HELD_STATES:
        _renew(item)
        touch_member(member)
        db.commit()
        return item, effects
    if item.state in FINAL_STATES:
        raise ProjectError(409, f"Item is already {item.state}")
    now = now_utc()
    previous = item.state
    # The conditional UPDATE is the atomic step: only one claimant can move
    # an open, unassigned row; a loser sees rowcount 0.
    result = db.execute(
        update(WorkItemDB)
        .where(
            WorkItemDB.item_id == item.item_id,
            WorkItemDB.state.in_(OPEN_STATES),
            WorkItemDB.assignee_member_id.is_(None),
        )
        .values(
            state="claimed",
            assignee_member_id=member.member_id,
            lease_expires_at=now + timedelta(seconds=lease_seconds()),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        db.refresh(item)
        if item.assignee_member_id == member.member_id:
            return item, effects
        if item.state in FINAL_STATES:
            raise ProjectError(409, f"Item is already {item.state}")
        raise ProjectError(409, "Item is claimed by another member", _held_by(item, db))
    db.refresh(item)
    _event(db, item, "claimed", member.member_id, previous, "claimed", at=now)
    _ack_delivery(db, item, member)
    touch_member(member)
    effects.notice(
        f"{member.handle} claimed {_quote(item.title)}",
        member.handle,
        member.member_id,
        item.item_id,
    )
    db.commit()
    return item, effects


def _require_assignee(item: WorkItemDB, member: ProjectMemberDB) -> None:
    if item.assignee_member_id != member.member_id or item.state not in HELD_STATES:
        if item.state in FINAL_STATES:
            raise ProjectError(409, f"Item is already {item.state}")
        raise ProjectError(403, "Only the member holding the claim can do that")


def release_item(
    db: Session, member: ProjectMemberDB, item_id: str, note: str | None
) -> tuple[WorkItemDB, Effects]:
    effects = Effects()
    item = _item_in_project(db, member.project_id, item_id)
    _require_assignee(item, member)
    _reopen(db, item, "released", member.member_id, note, effects)
    effects.notice(
        f"{member.handle} released {_quote(item.title)}",
        member.handle,
        member.member_id,
        item.item_id,
    )
    touch_member(member)
    db.commit()
    return item, effects


_STATE_ACTIONS = {"waiting": "waiting", "review": "review", "claimed": "resumed"}


def set_item_state(
    db: Session, member: ProjectMemberDB, item_id: str, state: str, note: str | None
) -> tuple[WorkItemDB, Effects]:
    effects = Effects()
    item = _item_in_project(db, member.project_id, item_id)
    _require_assignee(item, member)
    _renew(item)
    if item.state != state:
        _set_state(db, item, state, _STATE_ACTIONS[state], member.member_id, note)
        text = {
            "waiting": f"{member.handle} is waiting on {_quote(item.title)}",
            "review": f"{member.handle} put {_quote(item.title)} up for review",
            "claimed": f"{member.handle} is back on {_quote(item.title)}",
        }[state]
        effects.notice(text, member.handle, member.member_id, item.item_id)
    touch_member(member)
    db.commit()
    return item, effects


def finish_item(
    db: Session, member: ProjectMemberDB, item_id: str, final: str, note: str | None
) -> tuple[WorkItemDB, Effects]:
    """``done`` or ``declined``; final, repeatable with the same outcome."""
    effects = Effects()
    item = _item_in_project(db, member.project_id, item_id)
    if item.state in FINAL_STATES:
        if item.state == final and item.assignee_member_id == member.member_id:
            return item, effects
        if not _related(item, member, db):
            raise ProjectError(403, "This item is not yours to finish")
        raise ProjectError(409, f"Item is already {item.state}")
    direct = item.to_address == f"agent:{member.member_id}"
    if item.assignee_member_id == member.member_id and item.state in HELD_STATES:
        pass
    elif direct and item.state in OPEN_STATES:
        item.assignee_member_id = member.member_id  # implicit claim
        _ack_delivery(db, item, member)
    elif item.assignee_member_id and item.assignee_member_id != member.member_id:
        raise ProjectError(409, "Item is claimed by another member", _held_by(item, db))
    else:
        raise ProjectError(403, "Claim the item before finishing it")
    item.lease_expires_at = None
    _set_state(db, item, final, final, member.member_id, note)
    verb = "finished" if final == "done" else "declined"
    effects.notice(
        f"{member.handle} {verb} {_quote(item.title)}",
        member.handle,
        member.member_id,
        item.item_id,
    )
    touch_member(member)
    db.commit()
    return item, effects


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


def _visible_to(item: WorkItemDB, member: ProjectMemberDB) -> bool:
    """Unacked deliveries stay in the inbox while the item is open to the member."""
    if item.state in FINAL_STATES:
        return False
    return item.assignee_member_id is None or item.assignee_member_id == member.member_id


def read_inbox(
    db: Session, member: ProjectMemberDB, after: int, limit: int
) -> tuple[list[tuple[WorkItemDB, WorkItemDeliveryDB]], bool]:
    """The member's unacked deliveries, oldest first; marks them ``delivered``.

    Returns the entries and whether any has ``seq > after`` (what a long-poll
    waits for). Also renews the member's leases and records it as seen.
    """
    rows = (
        db.query(WorkItemDeliveryDB, WorkItemDB)
        .join(WorkItemDB, WorkItemDB.item_id == WorkItemDeliveryDB.item_id)
        .filter(
            WorkItemDeliveryDB.member_id == member.member_id,
            WorkItemDeliveryDB.state != "acked",
            WorkItemDB.project_id == member.project_id,
        )
        .order_by(WorkItemDeliveryDB.seq)
        .all()
    )
    entries = [(item, delivery) for delivery, item in rows if _visible_to(item, member)][:limit]
    now = now_utc()
    for item, delivery in entries:
        if delivery.state == "pending":
            delivery.state = "delivered"
            delivery.delivered_at = now
        if item.state == "pending":
            _set_state(db, item, "delivered", "delivered", member.member_id)
    for held in (
        db.query(WorkItemDB)
        .filter(
            WorkItemDB.assignee_member_id == member.member_id,
            WorkItemDB.state.in_(HELD_STATES),
        )
        .all()
    ):
        _renew(held)
    touch_member(member)
    _last_inbox_read[member.member_id] = now
    db.commit()
    return entries, any(delivery.seq > after for _, delivery in entries)


async def wait_for_inbox(
    member_id: str, project_id: str, after: int, limit: int, wait: float
) -> tuple[list[dict[str, Any]], int]:
    """Read the inbox, long-polling up to ``wait`` s for a delivery past ``after``.

    No DB session is held while waiting: each check opens and closes one.
    """
    from agent_ptt.db import SessionLocal

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, wait)
    _active_waits[member_id] = _active_waits.get(member_id, 0) + 1
    try:
        while True:
            event = _event_for(member_id)
            event.clear()
            with SessionLocal() as db:
                await expire_and_publish(db)
                member = db.get(ProjectMemberDB, member_id)
                if member is None:
                    raise ProjectError(401, "Unknown member")
                entries, fresh = read_inbox(db, member, after, limit)
                remaining = deadline - loop.time()
                if fresh or remaining <= 0 or wait <= 0:
                    items = serialize_items(db, [item for item, _ in entries])
                    payload = []
                    for data, (_, delivery) in zip(items, entries, strict=True):
                        data["delivery"] = {"seq": delivery.seq, "state": delivery.state}
                        payload.append(data)
                    cursor = max([after, *(d.seq for _, d in entries)])
                    return payload, cursor
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=remaining)
    finally:
        remaining_waits = _active_waits.get(member_id, 1) - 1
        if remaining_waits > 0:
            _active_waits[member_id] = remaining_waits
        else:
            _active_waits.pop(member_id, None)
        _last_inbox_read[member_id] = now_utc()


# ---------------------------------------------------------------------------
# Serialization and queries
# ---------------------------------------------------------------------------


def _member_ref(member: ProjectMemberDB | None) -> MemberRef | None:
    if member is None:
        return None
    return MemberRef(member_id=member.member_id, handle=member.handle, harness=member.harness)


def _members_by_id(db: Session, ids: Iterable[str | None]) -> dict[str, ProjectMemberDB]:
    wanted = {member_id for member_id in ids if member_id}
    if not wanted:
        return {}
    return {
        m.member_id: m
        for m in db.query(ProjectMemberDB).filter(ProjectMemberDB.member_id.in_(wanted)).all()
    }


def serialize_items(db: Session, items: list[WorkItemDB]) -> list[dict[str, Any]]:
    """The public item shape (never a member_key), batch-loading its relations."""
    if not items:
        return []
    ids = [item.item_id for item in items]
    projects = {
        p.project_id: p
        for p in db.query(ProjectDB)
        .filter(ProjectDB.project_id.in_({item.project_id for item in items}))
        .all()
    }
    last_events: dict[str, WorkItemEventDB] = {}
    for event in (
        db.query(WorkItemEventDB)
        .filter(WorkItemEventDB.item_id.in_(ids))
        .order_by(WorkItemEventDB.event_id)
        .all()
    ):
        last_events[event.item_id] = event
    open_questions: dict[str, int] = {}
    for (parent_id,) in (
        db.query(WorkItemDB.reply_to)
        .filter(
            WorkItemDB.reply_to.in_(ids),
            WorkItemDB.kind == "question",
            WorkItemDB.state.not_in(FINAL_STATES),
        )
        .all()
    ):
        open_questions[parent_id] = open_questions.get(parent_id, 0) + 1
    members = _members_by_id(
        db,
        [
            *(i.from_member_id for i in items),
            *(i.assignee_member_id for i in items),
            *(e.by_member_id for e in last_events.values()),
            *(
                i.to_address.removeprefix("agent:")
                for i in items
                if i.to_address.startswith("agent:")
            ),
        ],
    )
    out: list[dict[str, Any]] = []
    for item in items:
        project = projects.get(item.project_id)
        event = last_events.get(item.item_id)
        to = item.to_address
        to_label = to
        if to.startswith("agent:"):
            target = members.get(to.removeprefix("agent:"))
            to_label = f"agent:{target.handle}" if target else to
        public = WorkItemOut(
            item_id=item.item_id,
            project_id=item.project_id,
            project_name=project.name if project else "",
            kind=item.kind,
            to=to,
            to_label=to_label,
            from_=_member_ref(members.get(item.from_member_id)),
            title=item.title,
            body=item.body,
            context=item.context,
            reply_to=item.reply_to,
            state=item.state,
            assignee=_member_ref(members.get(item.assignee_member_id or "")),
            lease_expires_at=as_utc(item.lease_expires_at),
            created_at=as_utc(item.created_at),
            updated_at=as_utc(item.updated_at),
            last_event=EventRef(
                action=event.action,
                by=_member_ref(members.get(event.by_member_id or "")),
                at=as_utc(event.at),
            )
            if event
            else None,
            channel_id=project.channel_id if project else None,
            notice_message_id=item.notice_message_id,
            open_questions=open_questions.get(item.item_id, 0),
        )
        out.append(public.model_dump(mode="json", by_alias=True))
    return out


def serialize_item(db: Session, item: WorkItemDB) -> dict[str, Any]:
    return serialize_items(db, [item])[0]


def item_thread(db: Session, project_id: str, item_id: str) -> dict[str, Any]:
    """The item, its ``reply_to`` ancestors, every reply below it, and its events."""
    item = _item_in_project(db, project_id, item_id)
    ancestors: list[WorkItemDB] = []
    seen = {item.item_id}
    cursor = item
    while cursor.reply_to and cursor.reply_to not in seen:
        parent = db.get(WorkItemDB, cursor.reply_to)
        if parent is None:
            break
        ancestors.append(parent)
        seen.add(parent.item_id)
        cursor = parent
    ancestors.reverse()
    descendants: list[WorkItemDB] = []
    frontier = [item.item_id]
    while frontier:
        children = (
            db.query(WorkItemDB)
            .filter(WorkItemDB.reply_to.in_(frontier), WorkItemDB.item_id.not_in(seen))
            .all()
        )
        frontier = []
        for child in children:
            seen.add(child.item_id)
            descendants.append(child)
            frontier.append(child.item_id)
    descendants.sort(key=lambda i: as_utc(i.created_at))
    thread = serialize_items(db, [*ancestors, item, *descendants])
    events = (
        db.query(WorkItemEventDB)
        .filter_by(item_id=item.item_id)
        .order_by(WorkItemEventDB.event_id)
        .all()
    )
    members = _members_by_id(db, [e.by_member_id for e in events])
    return {
        "item": thread[len(ancestors)],
        "thread": thread,
        "events": [
            WorkItemEventOut(
                action=e.action,
                by=_member_ref(members.get(e.by_member_id or "")),
                at=as_utc(e.at),
                from_state=e.from_state,
                to_state=e.to_state,
                note=e.note,
            ).model_dump(mode="json")
            for e in events
        ],
    }


def active_resource_claims(db: Session, project_id: str) -> list[WorkItemDB]:
    return (
        db.query(WorkItemDB)
        .filter(
            WorkItemDB.project_id == project_id,
            WorkItemDB.kind == "claim",
            WorkItemDB.state.not_in(FINAL_STATES),
        )
        .order_by(WorkItemDB.created_at)
        .all()
    )


def open_item_count(db: Session, project_id: str) -> int:
    return (
        db.query(WorkItemDB)
        .filter(WorkItemDB.project_id == project_id, WorkItemDB.state.not_in(FINAL_STATES))
        .count()
    )


def board_items(
    db: Session,
    updated_after: datetime | None,
    project_id: str | None,
    member_id: str | None,
    include_done_days: int,
) -> list[WorkItemDB]:
    """Items for the board: all open ones, finished ones from the last N days."""
    cutoff = now_utc() - timedelta(days=include_done_days)
    query = db.query(WorkItemDB).filter(
        or_(WorkItemDB.state.not_in(FINAL_STATES), WorkItemDB.updated_at >= cutoff)
    )
    if updated_after is not None:
        query = query.filter(WorkItemDB.updated_at > updated_after)
    if project_id:
        query = query.filter(WorkItemDB.project_id == project_id)
    if member_id:
        query = query.filter(
            or_(WorkItemDB.assignee_member_id == member_id, WorkItemDB.from_member_id == member_id)
        )
    return query.order_by(WorkItemDB.updated_at).all()
