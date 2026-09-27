"""Shared session/channel routing, bundled with each standalone plugin."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

CONFIG_FILE = Path.home() / ".agent-ptt" / "announcer.env"
ROUTES_DIR = Path.home() / ".agent-ptt" / "session-channels"


def setting(name: str, default: str) -> str:
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
