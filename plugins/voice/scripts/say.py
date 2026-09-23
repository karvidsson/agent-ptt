#!/usr/bin/env python3
"""Speak a message in an Agent PTT voice channel.

Usage: say.py "message to speak"

Joins the channel as "<agent> · <project folder>" (auto-designed voice,
same identity and key cache as the announcer hooks) and posts the
message over REST. Unlike the announcer hook this is explicitly
invoked, so failures are reported loudly: error on stderr, exit 1.
Stdlib only — no dependencies.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("AGENT_PTT_URL", "http://localhost:8770").rstrip("/")
CHANNEL_NAME = os.environ.get("AGENT_PTT_CHANNEL", "Claude Code")
AGENT_NAME = os.environ.get("AGENT_PTT_AGENT", "Claude")
STATE_FILE = Path.home() / ".agent-ptt" / "announcer-state.json"
HTTP_TIMEOUT = float(os.environ.get("AGENT_PTT_TIMEOUT", "45"))
MAX_SAY_CHARS = 400


def _request(method: str, path: str, payload: dict | None = None, timeout: float | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
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


def _find_or_create_channel() -> str:
    channels = _request("GET", "/channels", timeout=2.0)
    for channel in channels:
        if channel.get("name") == CHANNEL_NAME:
            return channel["channel_id"]
    return _request("POST", "/channels", {"name": CHANNEL_NAME})["channel_id"]


def _join(channel_id: str, handle: str) -> str:
    return _request("POST", f"/channels/{channel_id}/join", {"handle": handle})["key_id"]


def say(text: str) -> None:
    channel_id = _find_or_create_channel()

    handle = f"{AGENT_NAME} · {Path.cwd().name}"
    state = _load_state()
    cache_key = f"say:{handle}"
    cached = state.get(cache_key, {})
    key_id = cached.get("key_id") if cached.get("channel_id") == channel_id else None

    if key_id is None:
        key_id = _join(channel_id, handle)
        state[cache_key] = {"channel_id": channel_id, "key_id": key_id}
        _save_state(state)

    try:
        _request("POST", f"/channels/{channel_id}/say", {"key_id": key_id, "text": text})
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        # Stale key or deleted channel — find (or recreate) the channel and rejoin once
        channel_id = _find_or_create_channel()
        key_id = _join(channel_id, handle)
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
