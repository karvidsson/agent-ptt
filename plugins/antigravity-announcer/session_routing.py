"""Shared session/channel routing, bundled with each standalone plugin."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_FILE = Path.home() / ".agent-ptt" / "announcer.env"
ROUTES_DIR = Path.home() / ".agent-ptt" / "session-channels"
SESSIONS_DIR = Path.home() / ".agent-ptt" / "workspace-sessions"
_BOUND_EVENT = None


def bind_event(event):
    global _BOUND_EVENT
    _BOUND_EVENT = event


def attached_profile():
    if _BOUND_EVENT and _BOUND_EVENT.get("agent_id"):
        return None
    session, harness = session_context(_BOUND_EVENT)
    if not session or session == "unknown":
        return None
    key = hashlib.sha256(json.dumps([harness.lower(), session]).encode()).hexdigest()
    path = SESSIONS_DIR / (key + ".json")
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    # A broken attachment must never fall back to a global/shared identity.
    if record.get("version") != 1 or not Path(record.get("profile", "")).is_absolute():
        raise ValueError("Invalid session attachment; attach this session again")
    return record["profile"]


def workspace_profile():
    return os.environ.get("AGENT_PTT_WORKSPACE_PROFILE") or attached_profile()


def setting(name: str, default: str) -> str:
    profile = workspace_profile()
    fields = {
        "AGENT_PTT_URL": "url",
        "AGENT_PTT_WORKSPACE_ORG": "organization",
        "AGENT_PTT_WORKSPACE_CHANNEL": "channel",
        "AGENT_PTT_WORKSPACE_TOKEN": "token",
    }
    if profile and name == "AGENT_PTT_MODE":
        return "workspace"
    if profile and name in fields:
        # Explicit per-agent profile follows channel changes made during a session.
        # Never fall back to another identity's global settings on an unreadable file.
        try:
            return str(json.loads(Path(profile).read_text()).get(fields[name], ""))
        except (OSError, ValueError, TypeError, AttributeError):
            return ""
    if name in os.environ:
        return os.environ[name]
    try:
        for line in CONFIG_FILE.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip().strip("\"'")
    except (OSError, UnicodeError):
        pass
    return default


def foreign_cursor_hook(event: dict | None = None) -> bool:
    """True when Cursor runs this script through its import of Claude Code hooks or
    plugins, not through the Cursor plugin's cursor_hook.py: the caller exits quietly.

    Cursor sets CURSOR_VERSION for every hook it runs (not for the agent's shell
    commands) and puts ``cursor_version`` in its payloads. cursor_hook.py marks its
    own calls with AGENT_PTT_AGENT=Cursor, and Claude Code sets CLAUDECODE for its
    hooks, so a real Claude Code session started from a Cursor terminal still counts.
    """
    if os.environ.get("AGENT_PTT_AGENT", "").strip().lower() == "cursor":
        return False
    if os.environ.get("CLAUDECODE"):
        return False
    cursor = os.environ.get("CURSOR_VERSION")
    return bool(cursor or (isinstance(event, dict) and event.get("cursor_version")))


def session_context(event: dict | None = None) -> tuple[str | None, str]:
    event = event or {}
    source = os.environ.get("AGENT_PTT_AGENT") or (
        "Codex" if os.environ.get("CODEX_THREAD_ID") else "Claude"
    )
    session_id = (
        event.get("session_id")
        or event.get("thread_id")
        or os.environ.get("AGENT_PTT_SESSION_ID")
        or os.environ.get("CODEX_THREAD_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
    )
    return session_id, source


def project_name(cwd: str | None = None) -> str:
    directory = Path(cwd or Path.cwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                return Path(line.removeprefix("worktree ")).name or "somewhere"
    except (OSError, subprocess.SubprocessError):
        pass
    return directory.name or "somewhere"


def route_path(session_id: str, agent: str, server: str) -> Path:
    key = json.dumps([server.rstrip("/"), agent, session_id]).encode()
    return ROUTES_DIR / (hashlib.sha256(key).hexdigest() + ".json")


def save_channel(session_id: str, agent: str, server: str, name: str | None) -> None:
    if not session_id or session_id == "unknown":
        raise ValueError("A stable session ID is required to select a channel")
    path = route_path(session_id, agent, server)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement: overlapping hooks can never read half a selection.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        json.dump({"channel": name}, out)
    os.replace(out.name, path)


def channel_name(session_id: str | None, agent: str, server: str, project: str) -> str:
    if session_id and session_id != "unknown":
        try:
            route = json.loads(route_path(session_id, agent, server).read_text())
            if "channel" in route:
                return route["channel"] or project
        except (OSError, ValueError, TypeError):
            pass
    # Only process-local overrides count. The legacy global config is ignored.
    return os.environ.get("AGENT_PTT_CHANNEL", "").strip() or project


class WorkspaceRequired(RuntimeError):
    """The legacy transport cannot participate in an organization."""


def local_urlopen(request, *, timeout):
    """Back off across hook processes when the server disables legacy endpoints."""
    parsed = urllib.parse.urlsplit(request.full_url)
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    cache = ROUTES_DIR / ("hosted-" + hashlib.sha256(origin.encode()).hexdigest() + ".json")
    message = (
        "Agent PTT: this server requires organization enrollment. Legacy hooks are paused "
        "until this server confirms local mode. Read "
        + origin
        + "/workspace/SKILL.md, enroll a separate agent "
        "profile, join a channel, and launch with "
        "agent-ptt workspace --profile <name> run -- <agent-command>."
    )

    def remember_hosted():
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=cache.parent, delete=False) as out:
                json.dump({"detected_at": time.time()}, out)
            os.replace(out.name, cache)
        except OSError:
            pass

    try:
        detected_at = float(json.loads(cache.read_text())["detected_at"])
    except (OSError, ValueError, KeyError, TypeError):
        detected_at = None
    if detected_at is not None:
        if 0 <= time.time() - detected_at < 300:
            raise WorkspaceRequired(message)
        # Never retry a disabled channel mutation to discover server mode.
        # An older or unavailable server cannot confirm that local APIs are safe.
        mode = None
        try:
            with urllib.request.urlopen(origin + "/health/live", timeout=timeout) as response:
                mode = response.headers.get("X-Agent-PTT-Mode")
        except (OSError, ValueError):
            pass
        if mode != "local":
            remember_hosted()
            raise WorkspaceRequired(message)
        cache.unlink(missing_ok=True)
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        # Preserve ordinary missing-channel errors and their reconnect behavior.
        body = exc.read()
        try:
            disabled = json.loads(body).get("detail") == (
                "Local-only endpoint is disabled in hosted mode"
            )
        except (ValueError, AttributeError):
            disabled = False
        if not disabled:
            # HTTPError's body is a stream; restore it for the caller.
            import io

            exc.close()
            raise urllib.error.HTTPError(
                exc.url, exc.code, exc.reason, exc.headers, io.BytesIO(body)
            ) from None
        exc.close()
        remember_hosted()
        print(message, file=sys.stderr)
        raise WorkspaceRequired(message) from None
