"""Project membership shared by session_start.py, tell.py, wait_inbox.py and receive.py.

Stdlib only and portable (no fcntl): these scripts also run on macOS and Windows.
Everything lives under ``~/.agent-ptt`` resolved on the machine that runs them.

A session's project membership is stored in ``~/.agent-ptt/projects/<digest>.json``,
keyed by (server, project, session), or by (server, project, handle) when
``AGENT_PTT_HANDLE`` pins the handle. A small per-session pointer file
(``session-<digest>.json``) lets the agent-run scripts find it again from just
the session ID, without the hook's cwd.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "session_routing", Path(__file__).with_name("session_routing.py")
)
routing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(routing)

HERE = Path(__file__).resolve().parent
PROJECTS_DIR = Path.home() / ".agent-ptt" / "projects"
INBOX_DIR = Path.home() / ".agent-ptt" / "inboxes"  # receive.py's channel state
HTTP_TIMEOUT = 5.0
DOING_MAX = 200
ORCHESTRATOR_ROLES = ("lead", "orchestrator")


# --- settings -------------------------------------------------------------


def base_url() -> str:
    return routing.setting("AGENT_PTT_URL", "http://localhost:8770").rstrip("/")


def pinned_handle() -> str:
    return routing.setting("AGENT_PTT_HANDLE", "").strip()[:80]


def roles() -> list[str]:
    raw = routing.setting("AGENT_PTT_ROLES", "")
    out: list[str] = []
    for role in raw.split(","):
        role = role.strip()
        if role and ":" not in role and len(role) <= 60 and role not in out:
            out.append(role)
    return out[:20]


def wants_wake(member_roles: list[str] | None = None) -> bool:
    if routing.setting("AGENT_PTT_WAKE", "") == "1":
        return True
    return any(
        marker in role.lower()
        for role in (member_roles if member_roles is not None else roles())
        for marker in ORCHESTRATOR_ROLES
    )


def harness(agent: str) -> str:
    return "codex" if agent.lower() == "codex" else "claude"


def machine() -> str:
    return (platform.node() or "").split(".")[0][:100]


# --- HTTP -----------------------------------------------------------------


def request(
    base: str,
    method: str,
    path: str,
    payload=None,
    *,
    member_key: str | None = None,
    participant_key: str | None = None,
    timeout: float = HTTP_TIMEOUT,
):
    headers = {"Content-Type": "application/json"}
    api_key = routing.setting("AGENT_PTT_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if member_key:
        headers["X-Member-Key"] = member_key
    if participant_key:
        headers["X-Participant-Key"] = participant_key
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    with routing.local_urlopen(req, timeout=timeout) as response:
        body = response.read()
    return json.loads(body) if body else None


# --- project identity -----------------------------------------------------


def _git(cwd: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=2, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def project_identity(cwd: str | None = None) -> dict:
    """``{"remote": url}`` for a repo with an origin, else ``{"name": main folder}``.

    Worktrees share the main checkout's remote; without one, the project is
    named after the main worktree's folder (same rule as the channel name).
    """
    directory = str(Path(cwd or Path.cwd()).resolve())
    remote = _git(directory, "remote", "get-url", "origin")
    if remote:
        return {"remote": remote}
    return {"name": routing.project_name(directory)}


# --- local state ----------------------------------------------------------


def _digest(*parts) -> str:
    return hashlib.sha256(json.dumps(list(parts)).encode()).hexdigest()


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        json.dump(data, out)
    os.replace(out.name, path)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def membership_path(base: str, identity: dict, session_id: str, handle: str = "") -> Path:
    scope = ["handle", handle.casefold()] if handle else ["session", session_id]
    return PROJECTS_DIR / (_digest(base, identity, *scope) + ".json")


def pointer_path(base: str, session_id: str) -> Path:
    return PROJECTS_DIR / ("session-" + _digest(base, session_id)[:32] + ".json")


def channel_state_path(base: str, agent: str, session_id: str) -> Path:
    """receive.py's per-session channel state (same digest as receive.deliver)."""
    return INBOX_DIR / (_digest(base, agent, session_id) + ".json")


def load_session(base: str, session_id: str) -> dict:
    """The pointer for a session: ``{session_id, agent, cwd, membership, ...}``."""
    return _read(pointer_path(base, session_id))


def save_session(base: str, session_id: str, **fields) -> dict:
    path = pointer_path(base, session_id)
    pointer = _read(path)
    pointer.update(fields, session_id=session_id, base=base, updated_at=time.time())
    _write(path, pointer)
    return pointer


def _within(child: str, parent: str) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
    except (ValueError, OSError):
        return False
    return True


def find_session(base: str, cwd: str | None = None) -> str | None:
    """This shell's session: the env, else the most recently active hooked
    session whose working directory contains ``cwd`` (or is inside it)."""
    for name in ("AGENT_PTT_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID"):
        value = os.environ.get(name, "").strip()
        if value and load_session(base, value):
            return value
    cwd = str(cwd or Path.cwd())
    best, best_at = None, -1.0
    for path in PROJECTS_DIR.glob("session-*.json"):
        pointer = _read(path)
        where = pointer.get("cwd") or ""
        if pointer.get("base") != base or not pointer.get("session_id") or not where:
            continue
        if not (_within(cwd, where) or _within(where, cwd)):
            continue
        at = float(pointer.get("updated_at") or 0)
        if at > best_at:
            best, best_at = pointer["session_id"], at
    return best


def load_membership(base: str, session_id: str) -> tuple[Path | None, dict]:
    pointer = load_session(base, session_id)
    name = pointer.get("membership")
    if not name or "/" in name or "\\" in name:
        return None, {}
    path = PROJECTS_DIR / name
    return path, _read(path)


def save_membership(path: Path, record: dict) -> None:
    _write(path, record)


def read_channel_state(base: str, session_id: str) -> tuple[Path | None, dict]:
    pointer = load_session(base, session_id)
    agent = pointer.get("agent")
    if not agent:
        return None, {}
    path = channel_state_path(base, agent, session_id)
    return path, _read(path)


def update_channel_state(path: Path, **fields) -> None:
    state = _read(path)
    state.update(fields)
    _write(path, state)


# --- joining --------------------------------------------------------------


def channel_join_payload(agent: str, session_id: str, handle: str) -> dict:
    payload = {
        "handle": handle,
        "session_id": session_id,
        "agent": agent,
        "receive_mentions": True,
    }
    pinned = pinned_handle()
    if pinned:
        # Asks the server to use this handle instead of the session's random name.
        # Servers without the flag ignore it and keep the session identity.
        payload.update(handle=pinned, pin_handle=True)
    return payload


def connect_channel(base: str, name: str, agent: str, session_id: str) -> dict:
    channel = request(base, "POST", "/channels", {"name": name, "reuse_existing": True})
    cid = channel["channel_id"]
    participant = request(
        base,
        "POST",
        f"/channels/{cid}/join",
        channel_join_payload(agent, session_id, agent),
    )
    return {
        "channel_id": cid,
        "key_id": participant["key_id"],
        "name": name,
        "handle": participant.get("handle"),
        "mention_policy": participant.get("mention_policy"),
        **wait_support(participant.get("mention_policy")),
    }


def wait_support(policy) -> dict:
    """``{"wait_supported", "wait_max_seconds"}`` from a join's mention_policy."""
    policy = policy if isinstance(policy, dict) else {}
    supported = policy.get("wait_supported") is True
    try:
        limit = int(policy.get("wait_max_seconds") or 0) if supported else 0
    except (TypeError, ValueError):
        limit = 0
    return {"wait_supported": supported, "wait_max_seconds": max(0, min(60, limit))}


def join_project(base: str, record: dict) -> dict:
    """Join or rejoin with ``record["join"]``; updates the record in place."""
    payload = dict(record["join"])
    response = None
    if record.get("member_key"):
        try:
            response = request(
                base, "POST", "/projects/join", {**payload, "member_key": record["member_key"]}
            )
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 403):
                raise
            response = None
    if response is None:
        if payload.get("handle") and pinned_handle():
            payload["resume_handle"] = True
        response = request(base, "POST", "/projects/join", payload)
    project, member = response.get("project") or {}, response.get("member") or {}
    if record.get("member_id") != member.get("member_id"):
        record["cursor"] = 0
    record.update(
        project_id=project.get("project_id"),
        project_name=project.get("name"),
        channel_id=project.get("channel_id"),
        member_id=member.get("member_id"),
        member_key=response.get("member_key") or record.get("member_key"),
        handle=member.get("handle") or payload.get("handle"),
        roles=member.get("roles") or [],
    )
    return record


# --- addressing and formatting ---------------------------------------------


def mention_text(handle: str) -> str:
    return "@" + handle if re.fullmatch(r"[\w.-]+", handle) else f'@"{handle}"'


def address(to: str) -> str:
    """``Handle``/``@Handle``/``@"Two Words"`` → ``agent:Handle``; roles and project pass."""
    to = to.strip()
    if to in ("project", "@project"):
        return "project"
    to = to.removeprefix("@")
    if to.startswith(("role:", "agent:")):
        return to
    if len(to) >= 2 and to[0] == to[-1] == '"':
        to = to[1:-1]
    return "agent:" + to.strip()


def one_line(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def item_line(item: dict) -> str:
    sender = (item.get("from") or {}).get("handle") or "?"
    # Same line as the CLI's `agent-ptt inbox --follow`.
    title = " ".join((item.get("title") or item.get("body") or "").split())
    return f"[ptt] {item.get('kind', 'item')} from {sender}: {title} (item {item.get('item_id')})"


def mention_line(message: dict) -> str:
    text = one_line(message.get("text") or "", 200)
    sender = message.get("handle") or "?"
    return f"[ptt] mention from {sender}: {text} (message {message.get('message_id')})"


def _quote(value: str) -> str:
    return f'"{value}"' if re.search(r"[\s\\'&()]", value) else value


def script_command(name: str, *args: str) -> str:
    """The absolute command for a bundled script, computed on this machine."""
    return " ".join([_quote(sys.executable or "python3"), _quote(str(HERE / name)), *args])


# --- roster -----------------------------------------------------------------

PRESENCE_ORDER = {"listening": 0, "active": 1, "idle": 2, "away": 3}
PRESENT = ("listening", "active")


def presence(member: dict) -> str:
    if member.get("presence"):
        return member["presence"]
    return "listening" if member.get("listening") else "active"


def roster(members: list[dict], participants: list[dict], me: dict) -> list[dict]:
    """Project members plus channel-only participants, present first, without me."""
    mine = {me.get("member_id") or "", (me.get("handle") or "").casefold()}
    people, seen = [], set()
    for member in members:
        handle = (member.get("handle") or "").casefold()
        if member.get("member_id") in mine or handle in mine:
            continue
        people.append({**member, "presence": presence(member)})
        seen.add(handle)
    for participant in participants:
        handle = participant.get("handle") or ""
        if not handle or handle.casefold() in seen or handle.casefold() in mine:
            continue
        seen.add(handle.casefold())
        people.append(
            {
                "handle": handle,
                "presence": "active" if participant.get("state") == "active" else "away",
                "doing": participant.get("doing"),
            }
        )
    people.sort(key=lambda p: PRESENCE_ORDER.get(p["presence"], 9))
    return people


def describe(person: dict) -> str:
    """``Theo (codex on mbp)`` — who, and where they run."""
    where = " on ".join(x for x in (person.get("harness"), person.get("machine")) if x)
    return (person.get("handle") or "?") + (f" ({where})" if where else "")


def person_line(person: dict) -> str:
    head = person.get("mention") or mention_text(person.get("handle") or "?")
    if person.get("roles"):
        head += " [" + ", ".join(person["roles"]) + "]"
    bits = [head]
    where = " on ".join(x for x in (person.get("harness"), person.get("machine")) if x)
    if where:
        bits.append(where)
    bits.append(person.get("presence") or "?")
    if person.get("doing"):
        bits.append("doing: " + one_line(person["doing"], 60))
    return "- " + " · ".join(bits)


def fetch_people(base: str, record: dict, channel_id: str | None) -> list[dict]:
    """The roster for a session, best effort: project members + channel participants."""
    members, participants = [], []
    if record.get("project_id"):
        try:
            detail = request(base, "GET", f"/projects/{record['project_id']}", timeout=3)
            members = detail.get("members") or []
        except Exception:
            pass
    if channel_id:
        try:
            channel = request(base, "GET", f"/channels/{channel_id}", timeout=3)
            participants = list((channel.get("participants") or {}).values())
        except Exception:
            pass
    me = {"member_id": record.get("member_id"), "handle": record.get("handle")}
    return roster(members, participants, me)


def snapshot(people: list[dict]) -> dict:
    """``{key: present?}`` for roster-change notices."""
    return {
        (p.get("member_id") or "h:" + (p.get("handle") or "").casefold()): p["presence"] in PRESENT
        for p in people
    }


def roster_changes(old: dict | None, people: list[dict]) -> list[str]:
    """Short "X (codex on mbp) joined, doing …" lines for joins and departures."""
    if old is None:
        return []
    lines = []
    for person in people:
        key = person.get("member_id") or "h:" + (person.get("handle") or "").casefold()
        present = person["presence"] in PRESENT
        if present and not old.get(key):
            line = f"{describe(person)} joined"
            if person.get("doing"):
                line += ", doing: " + one_line(person["doing"], 80)
            mention = person.get("mention") or mention_text(person.get("handle") or "?")
            lines.append(f"{line}. Address them as {mention}.")
        elif not present and old.get(key):
            lines.append(f"{describe(person)} went away (messages to them are queued).")
    return lines


ETIQUETTE = (
    "When someone else works on this project: introduce yourself with what you're doing,"
    " agree a split before editing, answer @mentions, post handoffs and findings (not"
    " narration), and don't edit files another agent has claimed."
)
