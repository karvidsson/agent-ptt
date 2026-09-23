#!/usr/bin/env python3
"""Manage the shared Agent PTT channel selection."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("AGENT_PTT_URL", "http://localhost:8770").rstrip("/")
CONFIG_FILE = Path.home() / ".agent-ptt" / "announcer.env"


def _request(method: str, path: str, payload: dict | None = None) -> object:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _save_channel(name: str) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = CONFIG_FILE.read_text().splitlines() if CONFIG_FILE.exists() else []
    replaced = False
    output = []
    for line in lines:
        if line.startswith("AGENT_PTT_CHANNEL="):
            output.append(f"AGENT_PTT_CHANNEL={name}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"AGENT_PTT_CHANNEL={name}")
    CONFIG_FILE.write_text("\n".join(output).rstrip() + "\n")


def main(args: list[str]) -> int:
    action = args[0] if args else "list"
    if action == "list":
        channels = _request("GET", "/channels")
        for channel in channels:
            print(f"{channel['name']}\t{channel['channel_id']}")
        return 0
    if action == "create":
        name = " ".join(args[1:]).strip()
        if not name:
            print("usage: channel.py create <channel name>", file=sys.stderr)
            return 2
        channel = _request("POST", "/channels", {"name": name})
        _save_channel(channel["name"])
        print(f"Created and selected: {channel['name']} ({channel['channel_id']})")
        return 0
    if action == "use":
        name = " ".join(args[1:]).strip()
        if not name:
            print("usage: channel.py use <channel name>", file=sys.stderr)
            return 2
        channels = _request("GET", "/channels")
        channel = next((item for item in channels if item["name"] == name), None)
        if channel is None:
            print(f"Channel not found: {name}", file=sys.stderr)
            return 1
        _save_channel(channel["name"])
        print(f"Selected: {channel['name']} ({channel['channel_id']})")
        return 0
    print("usage: channel.py [list|create|use] [channel name]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as error:
        print(f"agent-ptt channel failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
