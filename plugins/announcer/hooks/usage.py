#!/usr/bin/env python3
"""Token usage for the project roster: read counts locally, report them with one PATCH.

Run from the async Stop hook (and SessionEnd) by announce.py, never from a synchronous
hook. It costs no model tokens and injects nothing into the agent's context: it reads
local files and sends ``usage`` with ``PATCH /projects/{p}/members/me`` (wire format in
the task 37 design)::

    {"session_id": "...", "harness": "claude",
     "by_model": {"<model>": {"input", "cache_read", "cache_write", "output", "reasoning"}},
     "as_of": "<iso>", "limits": {...}}   # limits: optional, account scope

The counts are cumulative for the session; the server replaces them, so a retried or
lost PATCH is harmless. ``input`` never includes cache reads, and ``reasoning`` is a
breakdown of ``output``, not an addition to it.

Readers, one per harness, all incremental (a byte offset per file, kept in
``~/.agent-ptt/usage/<digest>.json``):

- **Claude Code**: ``message.usage`` on assistant lines of the transcript, deduplicated by
  ``message.id`` (one response is written as several identical lines), plus every
  ``<session>/subagents/agent-*.jsonl``, rolled into the session. A file that shrank is
  read again from the start. Subscription windows come from ``~/.agent-ptt/cc-ratelimits.json``,
  written by the opt-in statusline wrapper (``plugins/announcer/statusline.py``), when
  it is less than 30 minutes old.
- **Codex**: the rollout's ``token_count`` events (``total_token_usage`` is the session
  total; each increase is credited to the model of the latest ``turn_context``), and the
  event's ``rate_limits`` / ``plan_type`` / ``credits`` as limits.
- **Cursor**: cursor_hook.py calls ``record_cursor`` with the counts of each
  ``afterAgentResponse`` / ``stop`` payload (per generation); the reply text is never kept.
- **Antigravity**: nothing is reported.

Only counts, model names, the session id and limit percentages leave the machine: no
message text is ever copied out of a transcript. At most one PATCH per 30 seconds per
session, only when something changed (SessionEnd sends a pending change at once).
Stdlib only, portable, and silent on every failure.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

USAGE_DIR = Path.home() / ".agent-ptt" / "usage"
CC_LIMITS_FILE = Path.home() / ".agent-ptt" / "cc-ratelimits.json"
FIELDS = ("input", "cache_read", "cache_write", "output", "reasoning")
DEBOUNCE_SECONDS = 30.0
LIMITS_FRESH_SECONDS = 30 * 60
SEEN_TAIL = 64  # repeated lines of one response are adjacent; this many ids is plenty
LOCK_STALE_SECONDS = 60.0
HTTP_TIMEOUT = 3.0
LINE_MARKERS = (b'"usage"', b"token_count", b"turn_context")  # all other lines: unparsed
CLAUDE_WINDOWS = {"five_hour": "5h", "seven_day": "7d"}


def _load_link():
    spec = importlib.util.spec_from_file_location(
        "project_link", Path(__file__).with_name("project_link.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- small helpers -----------------------------------------------------------


def _count(value) -> int:
    """A token count as an int >= 0 (anything else is 0)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _zero() -> dict:
    return dict.fromkeys(FIELDS, 0)


def _add(total: dict, counts: dict, sign: int = 1) -> None:
    for field in FIELDS:
        total[field] = max(0, int(total.get(field, 0)) + sign * int(counts.get(field, 0)))


def _merge(*maps: dict) -> dict:
    """Sum several {model: counts} maps."""
    out: dict = {}
    for by_model in maps:
        for model, counts in (by_model or {}).items():
            _add(out.setdefault(model, _zero()), counts)
    return {model: counts for model, counts in out.items() if any(counts.values())}


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _epoch_iso(value) -> str | None:
    """An epoch-seconds reset time as ISO (an ISO string passes through)."""
    if isinstance(value, str) and value.strip():
        return value.strip()[:40]
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        with contextlib.suppress(OverflowError, OSError, ValueError):
            return iso(float(value))
    return None


def _pct(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value)))


def window_name(minutes) -> str:
    minutes = _count(minutes)
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m" if minutes else "window"


def read_lines(path: Path, offset: int):
    """Yield (record, end_offset) for complete JSON lines from ``offset`` on.

    Stops before a partial last line (no newline yet): the next run reads it whole.
    Lines without ``"usage"`` or ``token_count``/``turn_context`` are skipped unparsed.
    """
    with open(path, "rb") as handle:
        handle.seek(offset)
        position = offset
        for line in handle:
            if not line.endswith(b"\n"):
                return
            position += len(line)
            if not any(marker in line for marker in LINE_MARKERS):
                yield None, position
                continue
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            yield (record if isinstance(record, dict) else None), position


# --- state -------------------------------------------------------------------


def state_path(harness: str, session_id: str) -> Path:
    digest = hashlib.sha256(json.dumps([harness, session_id]).encode()).hexdigest()[:32]
    return USAGE_DIR / f"{digest}.json"


def load_state(harness: str, session_id: str) -> dict:
    try:
        data = json.loads(state_path(harness, session_id).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(harness: str, session_id: str, state: dict) -> None:
    path = state_path(harness, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as out:
        json.dump(state, out)
    os.replace(out.name, path)


@contextlib.contextmanager
def session_lock(harness: str, session_id: str, wait: float = 3.0):
    """One reader per session at a time (portable: an O_EXCL lock file). Yields
    False when another run holds it past ``wait``; a stale lock is taken over."""
    path = state_path(harness, session_id).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    held = False
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            held = True
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS:
                    path.unlink()
                    continue
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    try:
        yield held
    finally:
        if held:
            with contextlib.suppress(OSError):
                path.unlink()


# --- Claude Code ---------------------------------------------------------------


def claude_counts(usage: dict) -> dict:
    details = usage.get("output_tokens_details")
    return {
        "input": _count(usage.get("input_tokens")),
        "cache_read": _count(usage.get("cache_read_input_tokens")),
        "cache_write": _count(usage.get("cache_creation_input_tokens")),
        "output": _count(usage.get("output_tokens")),
        "reasoning": _count(details.get("thinking_tokens")) if isinstance(details, dict) else 0,
    }


def _remember(entry: dict, key: str, model: str, counts: dict) -> None:
    """Count one response once: a repeated id replaces its earlier counts."""
    seen = entry.setdefault("seen", {})
    by_model = entry.setdefault("by_model", {})
    previous = seen.pop(key, None)
    if previous:
        old_model, old_counts = previous
        _add(by_model.setdefault(old_model, _zero()), old_counts, -1)
    _add(by_model.setdefault(model, _zero()), counts)
    seen[key] = [model, counts]
    while len(seen) > SEEN_TAIL:
        seen.pop(next(iter(seen)))


def read_claude_file(path: Path, entry: dict) -> dict:
    """Fold the new lines of one Claude transcript into its ``entry`` (offset, seen
    tail, per-model sums). A file that shrank is counted again from the start."""
    try:
        size = path.stat().st_size
    except OSError:
        return entry
    if size < int(entry.get("offset", 0)):
        entry = {}
    offset = int(entry.get("offset", 0))
    if size == offset:
        return entry
    entry.setdefault("by_model", {})
    for record, end in read_lines(path, offset):
        entry["offset"] = end
        if not record or record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            continue
        model = str(message.get("model") or "unknown")[:100]
        if model == "<synthetic>":
            continue
        key = str(message.get("id") or record.get("requestId") or record.get("uuid") or "")
        if not key:
            continue
        _remember(entry, key, model, claude_counts(message["usage"]))
    return entry


def claude_files(transcript_path: str) -> list[Path]:
    """The session transcript and its subagents' (``<session>/subagents/agent-*.jsonl``)."""
    if not transcript_path:
        return []
    main = Path(transcript_path)
    files = [main]
    folder = main.with_suffix("") / "subagents"
    with contextlib.suppress(OSError):
        files += sorted(folder.glob("agent-*.jsonl"))
    return files


def read_claude(transcript_path: str, state: dict) -> dict:
    """Update ``state["files"]`` and return the session's {model: counts}."""
    files = state.setdefault("files", {})
    for path in claude_files(transcript_path):
        files[str(path)] = read_claude_file(path, files.get(str(path)) or {})
    state["by_model"] = _merge(*(entry.get("by_model") or {} for entry in files.values()))
    return state["by_model"]


def claude_limits(now: float | None = None) -> dict | None:
    """The 5h/7d windows the statusline wrapper saved, if under 30 minutes old."""
    now = time.time() if now is None else now
    try:
        data = json.loads(CC_LIMITS_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    at = data.get("rate_limits_at")
    limits = data.get("rate_limits")
    if not isinstance(at, (int, float)) or not isinstance(limits, dict):
        return None
    if now - float(at) > LIMITS_FRESH_SECONDS:
        return None
    windows = []
    for key, window in limits.items():
        if not isinstance(window, dict):
            continue
        used = _pct(window.get("used_percentage"))
        if used is None:
            continue
        windows.append(
            {
                "name": CLAUDE_WINDOWS.get(key, str(key)[:20]),
                "used_pct": used,
                "resets_at": _epoch_iso(window.get("resets_at")),
            }
        )
    if not windows:
        return None
    return {"source": "claude-statusline", "plan": None, "windows": windows, "reported_at": iso(at)}


# --- Codex ---------------------------------------------------------------------


def codex_counts(total: dict) -> dict:
    """``input_tokens`` includes the cached input; send it without."""
    cached = _count(total.get("cached_input_tokens"))
    return {
        "input": max(0, _count(total.get("input_tokens")) - cached),
        "cache_read": cached,
        "cache_write": _count(total.get("cache_write_input_tokens")),
        "output": _count(total.get("output_tokens")),
        "reasoning": _count(total.get("reasoning_output_tokens")),
    }


def codex_limits(rate_limits: dict, timestamp: str | None) -> dict | None:
    windows = []
    for key in ("primary", "secondary"):
        window = rate_limits.get(key)
        if not isinstance(window, dict):
            continue
        used = _pct(window.get("used_percent"))
        if used is None:
            continue
        windows.append(
            {
                "name": window_name(window.get("window_minutes")),
                "used_pct": used,
                "resets_at": _epoch_iso(window.get("resets_at")),
                "window_minutes": _count(window.get("window_minutes")),
            }
        )
    plan = rate_limits.get("plan_type")
    limits = {
        "source": "codex-rollout",
        "plan": str(plan)[:40] if isinstance(plan, str) and plan else None,
        "windows": windows,
        "reported_at": str(timestamp)[:40] if timestamp else iso(time.time()),
    }
    credits = rate_limits.get("credits")
    if isinstance(credits, dict):
        limits["credits"] = {
            "has_credits": bool(credits.get("has_credits")),
            "unlimited": bool(credits.get("unlimited")),
            "balance": str(credits.get("balance") or "")[:40] or None,
        }
    reached = rate_limits.get("rate_limit_reached_type")
    if isinstance(reached, str) and reached:
        limits["reached"] = reached[:40]
    return limits if windows or limits["plan"] or "credits" in limits else None


def read_codex(transcript_path: str, state: dict) -> dict:
    """Fold new rollout lines into ``state``; return the session's {model: counts}."""
    if not transcript_path:
        return state.get("by_model") or {}
    path = Path(transcript_path)
    try:
        size = path.stat().st_size
    except OSError:
        return state.get("by_model") or {}
    if state.get("path") != str(path) or size < int(state.get("offset", 0)):
        state.clear()
        state["path"] = str(path)
    offset = int(state.get("offset", 0))
    by_model = state.setdefault("by_model", {})
    last = state.get("last_total") or _zero()
    for record, end in read_lines(path, offset):
        state["offset"] = end
        if not record:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "turn_context":
            if isinstance(payload.get("model"), str) and payload["model"]:
                state["model"] = payload["model"][:100]
            continue
        if record.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        total = info.get("total_token_usage") if isinstance(info, dict) else None
        if isinstance(total, dict):
            counts = codex_counts(total)
            delta = {field: max(0, counts[field] - int(last.get(field, 0))) for field in FIELDS}
            _add(by_model.setdefault(state.get("model") or "unknown", _zero()), delta)
            last = counts
        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict):
            limits = codex_limits(rate_limits, record.get("timestamp"))
            if limits:
                state["limits"] = limits
    state["last_total"] = last
    state["by_model"] = {m: c for m, c in by_model.items() if any(c.values())}
    return state["by_model"]


# --- Cursor --------------------------------------------------------------------


def cursor_counts(raw: dict) -> dict | None:
    """The counts of one Cursor generation; None when the payload has none.
    ``input_tokens`` includes the cache reads (probed with cursor-agent 2026.11.19)."""
    keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    if not any(isinstance(raw.get(key), (int, float)) for key in keys):
        return None
    cache_read = _count(raw.get("cache_read_tokens"))
    return {
        "input": max(0, _count(raw.get("input_tokens")) - cache_read),
        "cache_read": cache_read,
        "cache_write": _count(raw.get("cache_write_tokens")),
        "output": _count(raw.get("output_tokens")),
        "reasoning": 0,
    }


def record_cursor(raw: dict) -> bool:
    """Add one ``afterAgentResponse`` / ``stop`` payload's counts to its conversation's
    state. A generation seen twice (both events carry the same counts) counts once.
    Reads only the count fields, model and ids: never the reply text."""
    session_id = str(raw.get("conversation_id") or raw.get("session_id") or "")
    counts = cursor_counts(raw)
    if not session_id or counts is None:
        return False
    key = str(raw.get("generation_id") or "")
    if not key:
        return False
    model = str(raw.get("model") or "unknown")[:100]
    with session_lock("cursor", session_id) as held:
        if not held:
            return False
        state = load_state("cursor", session_id)
        reader = state.setdefault("reader", {})
        _remember(reader, key, model, counts)
        reader["by_model"] = {m: c for m, c in reader["by_model"].items() if any(c.values())}
        save_state("cursor", session_id, state)
    return True


# --- reporting -----------------------------------------------------------------


def collect(harness: str, event: dict, state: dict, now: float) -> tuple[dict, dict | None]:
    """Update the reader state for this harness; return (by_model, limits)."""
    reader = state.setdefault("reader", {})
    transcript = str(event.get("transcript_path") or "")
    if harness == "claude":
        return read_claude(transcript, reader), claude_limits(now)
    if harness == "codex":
        by_model = read_codex(transcript, reader)
        return by_model, reader.get("limits")
    if harness == "cursor":
        return dict(reader.get("by_model") or {}), None
    return {}, None


def _fingerprint(by_model: dict, limits: dict | None) -> str:
    limits = {k: v for k, v in (limits or {}).items() if k != "reported_at"}
    return json.dumps([by_model, limits], sort_keys=True)


def build_usage(session_id: str, harness: str, by_model: dict, limits, now: float) -> dict:
    usage = {
        "session_id": session_id,
        "harness": harness,
        "by_model": by_model,
        "as_of": iso(now),
    }
    if limits:
        usage["limits"] = limits
    return usage


def report(event: dict, *, final: bool = False, now: float | None = None, link=None) -> bool:
    """Read this session's usage and PATCH it when it changed. True when sent.

    ``final`` (SessionEnd) skips the 30 s debounce so the last turn isn't lost.
    """
    now = time.time() if now is None else now
    link = link or _load_link()
    routing = link.routing
    if routing.hooks_disabled() or routing.setting("AGENT_PTT_USAGE", "1") == "0":
        return False
    if event.get("agent_id") or "/subagents/" in str(event.get("transcript_path") or ""):
        return False  # a subagent's turn: its file is read with the session's
    routing.bind_event(event)
    session_id, agent = routing.session_context(event)
    if not session_id or session_id == "unknown":
        return False
    harness = link.harness(agent)
    if harness == "antigravity":
        return False
    base = link.base_url()
    _, record = link.load_membership(base, session_id)
    if not record.get("member_key") or not record.get("project_id"):
        return False
    with session_lock(harness, session_id) as held:
        if not held:
            return False
        state = load_state(harness, session_id)
        by_model, limits = collect(harness, event, state, now)
        fingerprint = _fingerprint(by_model, limits)
        due = final or now - float(state.get("sent_at") or 0) >= DEBOUNCE_SECONDS
        send = (by_model or limits) and fingerprint != state.get("sent") and due
        if send:
            usage = build_usage(session_id, harness, by_model, limits, now)
            try:
                link.request(
                    base,
                    "PATCH",
                    f"/projects/{record['project_id']}/members/me",
                    {"usage": usage},
                    member_key=record["member_key"],
                    timeout=HTTP_TIMEOUT if not final else 1.5,
                )
            except Exception:
                send = False
            else:
                state["sent"] = fingerprint
                state["sent_at"] = now
        save_state(harness, session_id, state)
    return bool(send)


if __name__ == "__main__":  # manual check: python3 usage.py < hook-event.json
    import sys

    with contextlib.suppress(Exception):
        report(json.load(sys.stdin), final="--final" in sys.argv)
    sys.exit(0)
