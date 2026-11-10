#!/usr/bin/env python3
"""SessionStart hook: join the channel and the project, then tell the agent who is here.

Stdlib only, portable. Joins the session's channel for mentions (sharing
receive.py's state, so the first prompt hook doesn't re-register), joins the
project (git remote, else the main worktree's folder name) as this session or
as the pinned ``AGENT_PTT_HANDLE``, and injects a short roster, etiquette and
the exact ``ptt.py`` command, as an absolute path computed on this machine,
plus the trust and reply rules once (each later delivery has a one-line preamble).
Does nothing in workspace (hosted) mode or for subagents. Every failure exits
0 silently: this hook must never get in the way of coding.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "project_link", Path(__file__).with_name("project_link.py")
)
link = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(link)
routing = link.routing

MAX_CONTEXT = 2600
MAX_PEOPLE = 8
ROSTER_BUDGET = 700


def _workspace():
    spec = importlib.util.spec_from_file_location(
        "workspace_client", Path(__file__).with_name("workspace_client.py")
    )
    workspace = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(workspace)
    return workspace


def roster_lines(people: list[dict], budget: int = ROSTER_BUDGET) -> list[str]:
    """Present-first people lines that fit ``budget`` characters, plus an overflow note."""
    if not people:
        return ["Nobody else is here yet."]
    lines, used, shown = ["Others (present first):"], 0, 0
    for person in people[:MAX_PEOPLE]:
        line = link.person_line(person)
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if len(people) > shown:
        lines.append(f"- … and {len(people) - shown} more (`ptt who`)")
    return lines


def codex_wake_text(route: str) -> str:
    """How an idle Codex session gets woken, in the words the agent passes on."""
    if route == "queue":
        return (
            "While this Codex window is open, mentions and work items wake you even when"
            " idle: a background watcher queues a short prompt with `codex queue` (FYIs"
            " don't wake you; they come with your next delivery)."
        )
    minutes = max(1, round(receive_stop_wait() / 60)) if receive_stop_wait() else 0
    if minutes:
        return (
            f"Mentions reach you at your next hook, or within {minutes} min after a turn"
            " (the Stop hook waits). After that you're unreachable until the user prompts"
            f" you: tell the user once that other agents can only reach you for {minutes} min"
            " after each turn (this Codex has no `codex queue`)."
        )
    return (
        "Mentions reach you only at your next hook: tell the user once that other agents"
        " can't wake you while you're idle."
    )


def receive_stop_wait() -> float:
    try:
        raw = routing.setting("AGENT_PTT_STOP_WAIT_SECONDS", "")
        seconds = float(raw) if raw.strip() else 240.0
    except ValueError:
        seconds = 0.0
    if routing.setting("AGENT_PTT_WAKE", "") == "0":
        return 0.0
    return max(0.0, min(280.0, seconds))


def compose(
    *,
    agent: str,
    session_id: str,
    handle: str,
    project: dict | None,
    channel_name: str,
    people: list[dict],
    member_roles: list[str],
    codex_route: str = "",
) -> str:
    ptt = link.script_command("ptt.py", "--session", session_id)
    where = f"project {project['project_name']}" if project else f"channel {channel_name}"
    lines = [f"Agent PTT: you are {link.mention_text(handle)} in {where} ({link.base_url()})."]
    lines += roster_lines(people)
    lines.append(f"Your ptt command (run it with Bash; `ptt` below means this): {ptt}")
    lines.append(
        '  ptt say "@Theo found X in a.py" (@Handle reaches them even if away; @role:<r>,'
        " @project too) · ptt who · ptt inbox · ptt done <item>"
    )
    if project:
        lines.append(
            '  ptt tell <Handle|role:<r>|project> "<text>" [--kind question|note|handoff]'
            " [--reply-to <item>]  (a work item)"
        )
    lines.append(link.TRUST_RULES)
    lines.append(link.ETIQUETTE)
    wake = routing.setting("AGENT_PTT_WAKE", "") != "0"
    lead = link.wants_wake(member_roles)
    if agent.lower() == "codex":
        lines.append(
            codex_wake_text(codex_route) + " If every ptt call asks for sandbox"
            " approval, tell the user `ptt codex-rules` prints a one-time allow rule."
        )
    elif wake:
        lines.append(
            ("You coordinate others: start" if lead else "Start")
            + f" the Monitor tool now on `{ptt} wait` (longest timeout) so mentions wake"
            " you while idle (FYIs don't); each burst of lines is one delivery, FYI lines need"
            " no reply. When it ends, start it again."
        )
    else:
        lines.append(f"Mentions reach you at your next hook (`{ptt} wait` in Monitor wakes you).")
    text = "\n".join(lines)
    return text if len(text) <= MAX_CONTEXT else text[: MAX_CONTEXT - 1] + "…"


def codex_route() -> str:
    """ "queue" when an idle Codex can be woken with `codex queue`, else "stop"."""
    if routing.setting("AGENT_PTT_WAKE", "") == "0":
        return "off"
    return "queue" if link.codex_queue_supported() else "stop"


def start(event: dict) -> str | None:
    if event.get("agent_id"):
        return None
    workspace = _workspace()
    workspace.routing.bind_event(event)
    routing.bind_event(event)
    if workspace.enabled():
        return None
    if routing.setting("AGENT_PTT_RECEIVE", "1") == "0":
        return None
    session_id, agent = routing.session_context(event)
    if not session_id or session_id == "unknown":
        return None
    base = link.base_url()
    cwd = event.get("cwd") or str(Path.cwd())
    channel_name = routing.channel_name(session_id, agent, base, routing.project_name(cwd))

    connection = link.connect_channel(base, channel_name, agent, session_id)
    handle = connection.pop("handle", None) or agent
    connection["mention_policy"] = workspace.receive_policy(connection.get("mention_policy"))
    link.update_channel_state(link.channel_state_path(base, agent, session_id), **connection)
    pointer = {"agent": agent, "cwd": cwd, "channel_id": connection["channel_id"]}

    pinned = link.pinned_handle()
    identity = link.project_identity(cwd)
    path = link.membership_path(base, identity, session_id, pinned)
    record = link._read(path)
    join = {
        **identity,
        "handle": pinned or handle,
        "harness": link.harness(agent),
        "machine": link.machine(),
        "mode": "hooks",
    }
    if link.roles():
        join["roles"] = link.roles()
    record["join"] = join
    project = None
    try:
        link.join_project(base, record)
        link.save_membership(path, record)
        pointer["membership"] = path.name
        project = record
    except Exception:
        record = {"handle": handle}  # an older server without projects: the channel works
    people = link.fetch_people(base, record, connection["channel_id"])
    pointer["roster"] = link.snapshot(people)
    route = ""
    if agent.lower() == "codex":
        route = codex_route()
        pointer["wake_route"] = route
        if route == "queue":
            pointer["codex_pid"] = link.codex_pid()
    pointer.update(turn="idle", turn_at=time.time())
    link.save_session(base, session_id, **pointer)
    if route == "queue":
        link.start_codex_watch(base, session_id, pointer["codex_pid"])
    return compose(
        agent=agent,
        session_id=session_id,
        handle=record.get("handle") or handle,
        project=project,
        channel_name=channel_name,
        people=people,
        member_roles=record.get("roles") or link.roles(),
        codex_route=route,
    )


def main() -> None:
    if os.environ.get("AGENT_PTT_HOOKS", "1") == "0":
        return
    with contextlib.suppress(Exception):
        event = json.load(sys.stdin)
        if event.get("hook_event_name", "SessionStart") != "SessionStart":
            return
        context = start(event)
        if context:
            hook = {"hookEventName": "SessionStart", "additionalContext": context}
            output = {"hookSpecificOutput": hook}
            print(json.dumps(output), flush=True)


if __name__ == "__main__":
    main()
    sys.exit(0)
