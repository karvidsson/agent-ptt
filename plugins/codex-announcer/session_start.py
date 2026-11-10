#!/usr/bin/env python3
"""SessionStart hook: join the channel and the project, then tell the agent who is here.

Stdlib only, portable. Joins the session's channel for mentions (sharing
receive.py's state, so the first prompt hook doesn't re-register), joins the
project (git remote, else the main worktree's folder name) as this session or
as the pinned ``AGENT_PTT_HANDLE``, and injects a short roster, etiquette and
the exact ``ptt.py`` command, as an absolute path computed on this machine.
Does nothing in workspace (hosted) mode or for subagents. Every failure exits
0 silently: this hook must never get in the way of coding.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "project_link", Path(__file__).with_name("project_link.py")
)
link = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(link)
routing = link.routing

MAX_CONTEXT = 1800
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


def compose(
    *,
    agent: str,
    session_id: str,
    handle: str,
    project: dict | None,
    channel_name: str,
    people: list[dict],
    member_roles: list[str],
) -> str:
    ptt = link.script_command("ptt.py", "--session", session_id)
    where = f"project {project['project_name']}" if project else f"channel {channel_name}"
    lines = [f"Agent PTT: you are {link.mention_text(handle)} in {where} ({link.base_url()})."]
    lines += roster_lines(people)
    lines.append(f"Your ptt command (run it with Bash; `ptt` below means this): {ptt}")
    lines.append(
        '  ptt say "@Theo found X in a.py" (@Handle reaches them even if away; @role:<r>,'
        " @project too) · ptt who · ptt inbox · ptt ack|done <id>"
    )
    if project:
        lines.append(
            '  ptt tell <Handle|role:<r>|project> "<text>" [--kind question|note|handoff]'
            " [--reply-to <item>]  (a work item)"
        )
    lines.append(link.ETIQUETTE)
    wake = routing.setting("AGENT_PTT_WAKE", "") != "0"
    lead = link.wants_wake(member_roles)
    if agent.lower() == "codex":
        lines.append(
            "Mentions and work items reach you at your next hook; when you finish a turn"
            " while others are present, the Stop hook waits a few minutes for one."
        )
    elif wake:
        lines.append(
            ("You coordinate others: start" if lead else "Start")
            + f" the Monitor tool now (persistent: true) on `{ptt} wait` so mentions wake"
            " you while idle; each printed line is one to act on. If it stops, start it again."
        )
    else:
        lines.append(f"Mentions reach you at your next hook (`{ptt} wait` in Monitor wakes you).")
    text = "\n".join(lines)
    return text if len(text) <= MAX_CONTEXT else text[: MAX_CONTEXT - 1] + "…"


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
    link.save_session(base, session_id, **pointer)
    return compose(
        agent=agent,
        session_id=session_id,
        handle=record.get("handle") or handle,
        project=project,
        channel_name=channel_name,
        people=people,
        member_roles=record.get("roles") or link.roles(),
    )


def main() -> None:
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
