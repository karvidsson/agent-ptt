#!/usr/bin/env python3
"""Speak a message in an Agent PTT voice channel.

Usage: say.py "message to speak"

Uses the session channel selection or the Git repo/folder channel, with the
same session identity and key cache as the announcer hooks and posts the
message over REST. Unlike the announcer hook this is explicitly
invoked, so failures are reported loudly: error on stderr, exit 1.
Stdlib only — no dependencies.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Load the bundled helper by path so scripts also work outside a Python package.
_routing_spec = importlib.util.spec_from_file_location(
    "session_routing", Path(__file__).with_name("session_routing.py")
)
routing = importlib.util.module_from_spec(_routing_spec)
_routing_spec.loader.exec_module(routing)

BASE_URL = routing.setting("AGENT_PTT_URL", "http://localhost:8770").rstrip("/")
AGENT_NAME = routing.session_context()[1]
STATE_FILE = Path.home() / ".agent-ptt" / "announcer-state.json"
HTTP_TIMEOUT = float(os.environ.get("AGENT_PTT_TIMEOUT", "45"))
MAX_SAY_CHARS = 400


def _api_key() -> str:
    """AGENT_PTT_API_KEY from the environment or ~/.agent-ptt/announcer.env, or ""."""
    import os
    from pathlib import Path

    key = os.environ.get("AGENT_PTT_API_KEY", "").strip()
    if key:
        return key
    try:
        for line in (Path.home() / ".agent-ptt" / "announcer.env").read_text().splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "AGENT_PTT_API_KEY":
                return value.strip().strip("\"'")
    except (OSError, UnicodeError):
        pass
    return ""


def _headers() -> dict:
    """JSON headers plus the server's bearer key when one is configured."""
    headers = {"Content-Type": "application/json"}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _request(method: str, path: str, payload: dict | None = None, timeout: float | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers=_headers(),
    )
    with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def clean_message(raw: str, limit: int = MAX_SAY_CHARS) -> str:
    """Collapse whitespace and cap length so clips stay listenable."""
    message = " ".join(raw.split())
    if len(message) > limit:
        cut = message[:limit]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        message = cut + "…"
    return message


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def _find_or_create_channel(channel_name: str) -> str:
    channels = _request("GET", "/channels", timeout=2.0)
    for channel in channels:
        if channel.get("name") == channel_name:
            return channel["channel_id"]
    return _request("POST", "/channels", {"name": channel_name, "reuse_existing": True})[
        "channel_id"
    ]


def _join(channel_id: str, handle: str, session_id: str | None) -> str:
    payload = {"handle": handle}
    if session_id and session_id != "unknown":
        payload.update(session_id=session_id, agent=AGENT_NAME)
    return _request("POST", f"/channels/{channel_id}/join", payload)["key_id"]


def say(text: str) -> None:
    session_id, _ = routing.session_context()
    project = routing.project_name()
    channel_name = routing.channel_name(session_id, AGENT_NAME, BASE_URL, project)
    channel_id = _find_or_create_channel(channel_name)

    handle = f"{AGENT_NAME} · {project}"
    state = _load_state()
    cache_key = (
        f"identity-v2:{BASE_URL}:{AGENT_NAME}:{session_id}"
        if session_id
        else f"say:{BASE_URL}:{handle}"
    )
    cached = state.get(cache_key, {})
    key_id = cached.get("key_id") if cached.get("channel_id") == channel_id else None

    if key_id is None:
        key_id = _join(channel_id, handle, session_id)
        state[cache_key] = {"channel_id": channel_id, "key_id": key_id}
        _save_state(state)

    try:
        _request("POST", f"/channels/{channel_id}/say", {"key_id": key_id, "text": text})
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        # Stale key or deleted channel — find (or recreate) the channel and rejoin once
        channel_id = _find_or_create_channel(channel_name)
        key_id = _join(channel_id, handle, session_id)
        state[cache_key] = {"channel_id": channel_id, "key_id": key_id}
        _save_state(state)
        _request("POST", f"/channels/{channel_id}/say", {"key_id": key_id, "text": text})


def main() -> int:
    message = clean_message(" ".join(sys.argv[1:]))
    if not message:
        print("usage: say.py <message>", file=sys.stderr)
        return 1

    try:
        say(message)
    except Exception as e:
        print(
            f"agent-ptt say failed: {e}\n"
            f"Is the server running? Start it with: uv run agent-ptt server start "
            f"(server: {BASE_URL})",
            file=sys.stderr,
        )
        return 1

    print(f"🔊 said: {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
