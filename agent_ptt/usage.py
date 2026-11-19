"""Token usage per project member, and the subscription windows per machine.

Plugins send ``usage`` on ``PATCH /projects/{p}/members/me`` (see
``models.UsageReport``): cumulative counts for one harness session, per model.
``apply_report`` keeps the last snapshot per (member, session, model) and adds
only the growth since then to today's ``member_usage`` row, so a replayed or
retried report adds nothing. A total lower than the snapshot means the
harness reset its count (a new transcript, a cleared state file): the new
counts are usage since the reset, so they are added as they are and become
the new base. Nothing ever goes negative.

Days are server local time (``local_day``), like the transcript date dividers.
Per-session rows older than ``FOLD_DAYS`` are folded into one row per member,
model and day (``session_id = ""``), at most once a day, on write. Snapshots
are kept: they are one small row per session and model, and they stop an old
session that is resumed later from being counted twice.

The headline number is ``input + cache_write + output``. Cache reads dominate
real sessions (they are re-read context, billed at a tenth of input), so they
are reported next to it, not in it. ``reasoning`` is part of ``output``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from agent_ptt import pricing
from agent_ptt.models import (
    AccountLimitsDB,
    MemberUsageDB,
    MemberUsageSnapshotDB,
    ProjectDB,
    ProjectMemberDB,
    UsageReport,
)

PARTS = ("input", "cache_read", "cache_write", "output", "reasoning")
HEADLINE_PARTS = ("input", "cache_write", "output")
COUNTED_PARTS = ("input", "cache_read", "cache_write", "output")  # reasoning is in output
FOLD_DAYS = 30

_folded_on: str | None = None


def local_day(now: datetime | None = None) -> str:
    """The server's local calendar day, ``YYYY-MM-DD``."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone().date().isoformat()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def reset_runtime_state() -> None:
    global _folded_on
    _folded_on = None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _delta(old: MemberUsageSnapshotDB | None, new: dict[str, int]) -> dict[str, int]:
    if old is None:
        return dict(new)
    old_total = sum(getattr(old, part) for part in COUNTED_PARTS)
    new_total = sum(new[part] for part in COUNTED_PARTS)
    if new_total < old_total:  # The harness started counting again.
        return dict(new)
    return {part: max(0, new[part] - getattr(old, part)) for part in PARTS}


def apply_report(
    db: Session, member: ProjectMemberDB, report: UsageReport, now: datetime | None = None
) -> None:
    """Record one usage report for ``member`` (the caller commits)."""
    now = now or datetime.now(UTC)
    today = local_day(now)
    harness = report.harness or member.harness
    for model, counts in report.by_model.items():
        new = counts.model_dump(include=set(PARTS))
        snap = (
            db.query(MemberUsageSnapshotDB)
            .filter_by(member_id=member.member_id, session_id=report.session_id, model=model)
            .one_or_none()
        )
        delta = _delta(snap, new)
        if snap is None:
            snap = MemberUsageSnapshotDB(
                member_id=member.member_id, session_id=report.session_id, model=model
            )
            db.add(snap)
        for part in PARTS:
            setattr(snap, part, new[part])
        snap.updated_at = now
        if not any(delta.values()):
            continue
        row = (
            db.query(MemberUsageDB)
            .filter_by(
                member_id=member.member_id, session_id=report.session_id, model=model, day=today
            )
            .one_or_none()
        )
        if row is None:
            row = MemberUsageDB(
                project_id=member.project_id,
                member_id=member.member_id,
                session_id=report.session_id,
                model=model,
                day=today,
                **{part: 0 for part in PARTS},
            )
            db.add(row)
        for part in PARTS:
            setattr(row, part, (getattr(row, part) or 0) + delta[part])
        row.harness = harness
        row.updated_at = now
    if report.limits is not None and harness:
        _apply_limits(db, member, harness, report, now)
    db.flush()
    maybe_fold(db, now)


def _apply_limits(
    db: Session, member: ProjectMemberDB, harness: str, report: UsageReport, now: datetime
) -> None:
    limits = report.limits
    reported_at = _as_utc(limits.reported_at or report.as_of) or now
    reported_at = min(reported_at, now)  # A fast client clock can't pin it "fresh".
    machine = member.machine or ""
    row = db.query(AccountLimitsDB).filter_by(machine=machine, harness=harness).one_or_none()
    if row is not None and _as_utc(row.reported_at) > reported_at:
        return  # An older report arriving late.
    if row is None:
        row = AccountLimitsDB(machine=machine, harness=harness)
        db.add(row)
    row.source = limits.source
    row.plan = limits.plan
    row.windows = [w.model_dump(mode="json") for w in limits.windows]
    row.reported_at = reported_at
    row.reported_by = member.member_id


def maybe_fold(db: Session, now: datetime | None = None) -> int:
    """Fold old per-session rows, at most once per local day; returns rows folded."""
    global _folded_on
    today = local_day(now)
    if _folded_on == today:
        return 0
    _folded_on = today
    return fold_old_sessions(db, today)


def fold_old_sessions(db: Session, today: str) -> int:
    """Merge per-session rows older than ``FOLD_DAYS`` into ``session_id = ""`` rows."""
    cutoff = (date.fromisoformat(today) - timedelta(days=FOLD_DAYS)).isoformat()
    old = (
        db.query(MemberUsageDB)
        .filter(MemberUsageDB.day < cutoff, MemberUsageDB.session_id != "")
        .all()
    )
    if not old:
        return 0
    folded: dict[tuple[str, str, str], MemberUsageDB] = {}
    for row in old:
        key = (row.member_id, row.model, row.day)
        target = folded.get(key)
        if target is None:
            target = (
                db.query(MemberUsageDB)
                .filter_by(member_id=row.member_id, session_id="", model=row.model, day=row.day)
                .one_or_none()
            )
            if target is None:
                target = MemberUsageDB(
                    project_id=row.project_id,
                    member_id=row.member_id,
                    session_id="",
                    harness=row.harness,
                    model=row.model,
                    day=row.day,
                    **{part: 0 for part in PARTS},
                )
                db.add(target)
            folded[key] = target
        for part in PARTS:
            setattr(target, part, (getattr(target, part) or 0) + (getattr(row, part) or 0))
        target.updated_at = row.updated_at
        db.delete(row)
    db.flush()
    return len(old)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _empty() -> dict[str, int]:
    return {part: 0 for part in PARTS}


def _finish(by_model: dict[str, dict[str, int]], estimate: bool) -> dict[str, Any]:
    """Sum per-model counts into one bucket with its headline (and ``est_usd``)."""
    out: dict[str, Any] = _empty()
    for counts in by_model.values():
        for part in PARTS:
            out[part] += counts[part]
    out["headline"] = sum(out[part] for part in HEADLINE_PARTS)
    if estimate:
        priced = [pricing.estimate_usd(model, c) for model, c in by_model.items()]
        known = [usd for usd in priced if usd is not None]
        out["est_usd"] = round(sum(known), 4) if known else None
    return out


def _model_entry(counts: dict[str, int], model: str, estimate: bool) -> dict[str, Any]:
    entry: dict[str, Any] = dict(counts)
    entry["headline"] = sum(counts[part] for part in HEADLINE_PARTS)
    if estimate:
        entry["est_usd"] = pricing.estimate_usd(model, counts)
    return entry


def _sums(query, today: str):
    """Columns for total and today sums of every part."""
    total = [func.coalesce(func.sum(getattr(MemberUsageDB, p)), 0) for p in PARTS]
    day = [
        func.coalesce(func.sum(case((MemberUsageDB.day == today, getattr(MemberUsageDB, p)))), 0)
        for p in PARTS
    ]
    return query.add_columns(*total, *day)


def _split(values) -> tuple[dict[str, int], dict[str, int]]:
    values = [int(v or 0) for v in values]
    n = len(PARTS)
    return dict(zip(PARTS, values[:n], strict=True)), dict(zip(PARTS, values[n:], strict=True))


def member_summaries(
    db: Session,
    members: Iterable[ProjectMemberDB],
    now: datetime | None = None,
    estimate: bool = False,
) -> dict[str, dict[str, Any]]:
    """``{member_id: {session, today, total, by_model, harness, session_id, updated_at}}``.

    Members that never reported usage are absent.
    """
    members = list(members)
    ids = [m.member_id for m in members]
    if not ids:
        return {}
    today = local_day(now)
    rows = _sums(
        db.query(MemberUsageDB.member_id, MemberUsageDB.model).filter(
            MemberUsageDB.member_id.in_(ids)
        ),
        today,
    ).group_by(MemberUsageDB.member_id, MemberUsageDB.model)
    totals: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
    todays: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
    for member_id, model, *values in rows:
        totals[member_id][model], todays[member_id][model] = _split(values)

    # The current session: the one reported most recently.
    current: dict[str, MemberUsageSnapshotDB] = {}
    for snap in db.query(MemberUsageSnapshotDB).filter(MemberUsageSnapshotDB.member_id.in_(ids)):
        best = current.get(snap.member_id)
        if best is None or _as_utc(snap.updated_at) > _as_utc(best.updated_at):
            current[snap.member_id] = snap

    harness_of = {
        member_id: harness
        for member_id, harness in db.query(MemberUsageDB.member_id, MemberUsageDB.harness)
        .filter(MemberUsageDB.member_id.in_(ids), MemberUsageDB.harness.isnot(None))
        .order_by(MemberUsageDB.updated_at)
    }
    out: dict[str, dict[str, Any]] = {}
    for member in members:
        mid = member.member_id
        if mid not in totals and mid not in current:
            continue
        session_by_model: dict[str, dict[str, int]] = {}
        snap = current.get(mid)
        if snap is not None:
            rows = _sums(
                db.query(MemberUsageDB.model).filter_by(member_id=mid, session_id=snap.session_id),
                today,
            ).group_by(MemberUsageDB.model)
            for model, *values in rows:
                session_by_model[model] = _split(values)[0]
        by_model = {
            model: {
                "today": _model_entry(todays[mid][model], model, estimate),
                "total": _model_entry(counts, model, estimate),
            }
            for model, counts in sorted(totals[mid].items())
        }
        out[mid] = {
            "session": _finish(session_by_model, estimate),
            "today": _finish(todays[mid], estimate),
            "total": _finish(totals[mid], estimate),
            "by_model": by_model,
            "harness": harness_of.get(mid) or member.harness,
            "session_id": snap.session_id if snap else None,
            "updated_at": _as_utc(snap.updated_at).isoformat() if snap else None,
        }
    return out


def _bucket_totals(
    db: Session, today: str, estimate: bool, keys: tuple = (), filters: tuple = ()
) -> dict[Any, dict[str, Any]]:
    """Today/total buckets grouped by ``keys`` (one key: plain values; none: ``()``)."""
    rows = _sums(db.query(*keys, MemberUsageDB.model).filter(*filters), today).group_by(
        *keys, MemberUsageDB.model
    )
    totals: dict[Any, dict[str, dict[str, int]]] = defaultdict(dict)
    todays: dict[Any, dict[str, dict[str, int]]] = defaultdict(dict)
    n = len(keys)
    for row in rows:
        key = row[0] if n == 1 else tuple(row[:n])
        model = row[n]
        totals[key][model], todays[key][model] = _split(row[n + 1 :])
    return {
        key: {"today": _finish(todays[key], estimate), "total": _finish(totals[key], estimate)}
        for key in totals
    }


def _no_usage(estimate: bool) -> dict[str, Any]:
    return {"today": _finish({}, estimate), "total": _finish({}, estimate)}


def limits_out(row: AccountLimitsDB, db: Session | None = None) -> dict[str, Any]:
    by = None
    if db is not None and row.reported_by:
        member = db.get(ProjectMemberDB, row.reported_by)
        if member is not None:
            by = {"member_id": member.member_id, "handle": member.handle}
    return {
        "machine": row.machine or None,
        "harness": row.harness,
        "source": row.source,
        "plan": row.plan,
        "windows": list(row.windows or []),
        "reported_at": _as_utc(row.reported_at).isoformat() if row.reported_at else None,
        "reported_by": by,
    }


def limits_for(db: Session, machine: str | None, harness: str | None) -> dict[str, Any] | None:
    if not harness:
        return None
    row = db.query(AccountLimitsDB).filter_by(machine=machine or "", harness=harness).one_or_none()
    return limits_out(row) if row else None


def project_usage(
    db: Session,
    project: ProjectDB,
    members: list[ProjectMemberDB],
    now: datetime | None = None,
    estimate: bool = False,
) -> dict[str, Any]:
    """``GET /projects/{p}/usage``."""
    today = local_day(now)
    summaries = member_summaries(db, members, now, estimate)
    totals = _bucket_totals(
        db, today, estimate, filters=(MemberUsageDB.project_id == project.project_id,)
    ).get((), _no_usage(estimate))
    member_rows = []
    seen_limits: dict[tuple[str, str], dict[str, Any]] = {}
    for member in members:
        summary = summaries.get(member.member_id)
        harness = (summary or {}).get("harness") or member.harness
        member_rows.append(
            {
                "member_id": member.member_id,
                "handle": member.handle,
                "harness": harness,
                "machine": member.machine,
                "usage": summary,
            }
        )
        limits = limits_for(db, member.machine, harness)
        if limits is not None:
            seen_limits[(member.machine or "", harness)] = limits
    return {
        "project_id": project.project_id,
        "day": today,
        "today": totals["today"],
        "total": totals["total"],
        "members": member_rows,
        "limits": list(seen_limits.values()),
        "estimate": estimate,
    }


def server_usage(db: Session, now: datetime | None = None, estimate: bool = False) -> dict:
    """``GET /usage``: totals per project and per harness, plus every account window."""
    today = local_day(now)
    per_project = _bucket_totals(db, today, estimate, (MemberUsageDB.project_id,))
    per_harness = _bucket_totals(db, today, estimate, (MemberUsageDB.harness,))
    grand = _bucket_totals(db, today, estimate).get((), _no_usage(estimate))
    names = {p.project_id: p.name for p in db.query(ProjectDB).all()}
    return {
        "day": today,
        "today": grand["today"],
        "total": grand["total"],
        "projects": [
            {"project_id": pid, "name": names.get(pid), **bucket}
            for pid, bucket in sorted(per_project.items(), key=lambda kv: names.get(kv[0]) or "")
        ],
        "harnesses": [
            {"harness": harness, **bucket}
            for harness, bucket in sorted(per_harness.items(), key=lambda kv: kv[0] or "")
        ],
        "limits": [
            limits_out(row, db)
            for row in db.query(AccountLimitsDB).order_by(
                AccountLimitsDB.machine, AccountLimitsDB.harness
            )
        ],
        "estimate": estimate,
    }
