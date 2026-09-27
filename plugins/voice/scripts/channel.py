#!/usr/bin/env python3
"""Manage the current CLI session's Agent PTT channel selection."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import urllib.request
from pathlib import Path

# Load the bundled helper by path so scripts also work outside a Python package.
_routing_spec = importlib.util.spec_from_file_location(
    "session_routing", Path(__file__).with_name("session_routing.py")
)
routing = importlib.util.module_from_spec(_routing_spec)
_routing_spec.loader.exec_module(routing)

BASE_URL = routing.setting("AGENT_PTT_URL", "http://localhost:8770").rstrip("/")


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


def _request(method: str, path: str, payload: dict | None = None) -> object:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers=_headers(),
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _find_or_create(name: str) -> tuple[dict, bool]:
    """Return the channel with this name, creating it if missing, and whether it was created."""
    channels = _request("GET", "/channels")
    channel = next((item for item in channels if item["name"] == name), None)
    if channel is not None:
        return channel, False
    return _request("POST", "/channels", {"name": name, "reuse_existing": True}), True


def main(args: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["list", "create", "use", "auto"], nargs="?", default="list"
    )
    parser.add_argument("name", nargs="*")
    parser.add_argument("--session-id")
    parser.add_argument("--agent")
    options = parser.parse_args(args)
    if options.action == "list":
        for channel in _request("GET", "/channels"):
            print(f"{channel['name']}\t{channel['channel_id']}")
        return 0
    session_id, agent = routing.session_context()
    session_id = options.session_id or session_id
    agent = options.agent or agent
    if not session_id or session_id == "unknown":
        print(
            "Pass --session-id (and --agent) or set AGENT_PTT_SESSION_ID; "
            "channel selection only applies to a specific session.",
            file=sys.stderr,
        )
        return 2
    if options.action == "auto":
        routing.save_channel(session_id, agent, BASE_URL, None)
        print("This session will use its Git repo or folder channel.")
        return 0
    name = " ".join(options.name).strip()
    if not name:
        print("A channel name is required.", file=sys.stderr)
        return 2
    channel, created = _find_or_create(name)
    routing.save_channel(session_id, agent, BASE_URL, channel["name"])
    verb = "Created and selected" if created else "Selected"
    print(f"{verb}: {channel['name']} ({channel['channel_id']}) for this session")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"agent-ptt channel failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
