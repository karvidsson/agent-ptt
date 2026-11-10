#!/usr/bin/env python3
"""UserPromptSubmit (async): publish what this session is doing to its project.

Sends the prompt's first line (at most 200 characters) as ``doing`` with
``PATCH /projects/{p}/members/me``, only when it changed. Stdlib only, portable,
silent on every failure, and a no-op in workspace mode, for subagents, for
background notifications and when the session has no project membership.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "project_link", Path(__file__).with_name("project_link.py")
)
link = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(link)
routing = link.routing

NOTIFICATION_MARKERS = ("<system-reminder>", "[SYSTEM NOTIFICATION", "<task-notification>", "[ptt]")


def doing_text(prompt: str) -> str:
    head = prompt.lstrip()[:400]
    if not head or any(marker in head for marker in NOTIFICATION_MARKERS):
        return ""
    first = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
    return link.one_line(first, link.DOING_MAX)


def update(event: dict) -> bool:
    if event.get("hook_event_name") != "UserPromptSubmit" or event.get("agent_id"):
        return False
    spec = importlib.util.spec_from_file_location(
        "workspace_client", Path(__file__).with_name("workspace_client.py")
    )
    workspace = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workspace)
    workspace.routing.bind_event(event)
    routing.bind_event(event)
    if workspace.enabled():
        return False
    text = doing_text(event.get("prompt") or "")
    session_id, _ = routing.session_context(event)
    if not text or not session_id or session_id == "unknown":
        return False
    base = link.base_url()
    path, record = link.load_membership(base, session_id)
    if not path or not record.get("member_key") or record.get("doing") == text:
        return False
    link.request(
        base,
        "PATCH",
        f"/projects/{record['project_id']}/members/me",
        {"doing": text},
        member_key=record["member_key"],
        timeout=3,
    )
    record = link._read(path) or record  # don't clobber a concurrent rejoin
    record["doing"] = text
    link.save_membership(path, record)
    return True


def main() -> None:
    if os.environ.get("AGENT_PTT_HOOKS", "1") == "0":
        return
    with contextlib.suppress(Exception):
        update(json.load(sys.stdin))


if __name__ == "__main__":
    main()
    sys.exit(0)
