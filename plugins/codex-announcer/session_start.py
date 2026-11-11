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
    version: str = "",
    mcp: bool = False,
) -> str:
    ptt = link.script_command("ptt.py", "--session", session_id)
    where = f"project {project['project_name']}" if project else f"channel {channel_name}"
    lines = [f"Agent PTT: you are {link.mention_text(handle)} in {where} ({link.base_url()})."]
    stale = link.stale_plugin_line(version, people)
    if stale:
        lines.append(stale)
    lines += roster_lines(people)
    shell = {"antigravity": "in the terminal", "cursor": "with the Shell tool"}.get(
        agent.lower(), "with Bash"
    )
    lines.append(f"Your ptt command (run it {shell}; `ptt` below means this): {ptt}")
    lines.append(
        '  ptt say "@Theo found X in a.py" (@Handle reaches them even if away; @role:<r>,'
        " @project too) · ptt who · ptt inbox · ptt done <item>"
    )
    if project:
        lines.append(
            '  ptt tell <Handle|role:<r>|project> "<text>" [--kind question|note|handoff]'
            " [--reply-to <item>]  (a work item)"
        )
    if mcp:
        lines.append(
            f"MCP tools agent-ptt say/tell/who/inbox/ack/done also work; pass session={session_id}."
        )
    lines.append(link.TRUST_RULES)
    lines.append(link.ETIQUETTE)
    wake = routing.setting("AGENT_PTT_WAKE", "") != "0"
    lead = link.wants_wake(member_roles)
    if agent.lower() == "codex":
        lines.append(
            codex_wake_text(codex_route)
            + " Keep ptt commands plain so the sandbox lets them through: double-quote the"
            " text with no $, backticks, backslashes or newlines; for anything else write it"
            " to a file and run `ptt say --file <path>`. If every ptt call asks for sandbox"
            " approval, tell the user `ptt codex-rules` prints a one-time allow rule."
        )
    elif agent.lower() == "antigravity":
        lines.insert(1, antigravity_wake_text(ptt, wake, lead))
    elif agent.lower() == "cursor":
        lines.insert(1, cursor_wake_text(ptt, wake, lead))
    elif wake:
        # Second line, before the roster: sessions that read on past it tend to skip it.
        lines.insert(
            1,
            ("You coordinate others. First, start" if lead else "First, start")
            + f" the Monitor tool on `{ptt} wait` (longest timeout), even if nobody else is"
            " here yet: other agents can only reach you while you're idle through it (FYIs"
            " don't wake you). Each burst of lines is one delivery; FYI lines need no reply."
            " When it ends, start it again.",
        )
    else:
        lines.append(f"Mentions reach you at your next hook (`{ptt} wait` in Monitor wakes you).")
    text = "\n".join(lines)
    return text if len(text) <= MAX_CONTEXT else text[: MAX_CONTEXT - 1] + "…"


def antigravity_wake_text(ptt: str, wake: bool, lead: bool) -> str:
    """Antigravity wakes when a background command it started exits."""
    if not wake:
        return f"Mentions reach you before your next model call (`{ptt} inbox` lists them)."
    return (
        ("You coordinate others. First, start" if lead else "First, start")
        + f" `{ptt} wait --first` as a background command (don't wait for it), even if nobody"
        " else is here yet: other agents can only reach you while you're idle through it"
        " (FYIs don't wake you). When it exits, its output is a delivery: act on it, then"
        " start it again in the background. Run each ptt command on its own (no &&, pipes"
        " or $): then it needs no approval."
    )


def cursor_wake_text(ptt: str, wake: bool, lead: bool) -> str:
    """Cursor wakes when a background shell command it started finishes."""
    approval = (
        " Run each ptt command on its own (no &&, pipes or $). If ptt calls ask for"
        " approval, tell the user once that `ptt cursor-rules` prints an allowlist entry."
    )
    if not wake:
        return f"Mentions reach you after your next tool call (`{ptt} inbox` lists them)." + (
            approval
        )
    return (
        ("You coordinate others. First, start" if lead else "First, start")
        + f" `{ptt} wait --first` as a background shell command (don't wait for it), even if"
        " nobody else is here yet: other agents can only reach you while you're idle through"
        " it (FYIs don't wake you). When it finishes, its output is a delivery: act on it,"
        " then start the same `wait --first` command again in the background." + approval
    )


def codex_route() -> str:
    """ "queue" when an idle Codex can be woken with `codex queue`, else "stop"."""
    if routing.setting("AGENT_PTT_WAKE", "") == "0":
        return "off"
    return "queue" if link.codex_queue_supported() else "stop"


def join_session(
    base: str,
    session_id: str,
    agent: str,
    cwd: str,
    *,
    mode: str = "hooks",
    harness: str | None = None,
    identity: dict | None = None,
    **pointer_fields,
) -> dict:
    """Join this session's channel and project and save its pointer; the shared
    part of SessionStart and ptt_mcp.py's standalone (hookless) join.

    Returns ``{"handle", "project", "channel_name", "channel_id", "people", "record",
    "version"}``; ``project`` is None on an older server without projects.
    """
    channel_name = routing.channel_name(session_id, agent, base, routing.project_name(cwd))
    connection = link.connect_channel(base, channel_name, agent, session_id)
    handle = connection.pop("handle", None) or agent
    connection["mention_policy"] = _workspace().receive_policy(connection.get("mention_policy"))
    link.update_channel_state(link.channel_state_path(base, agent, session_id), **connection)
    pointer = {
        **pointer_fields,
        "agent": agent,
        "cwd": cwd,
        "channel_id": connection["channel_id"],
        "channel_name": channel_name,  # lets ptt rejoin if the channel state file is gone
    }
    version = link.plugin_version()

    pinned = link.pinned_handle()
    identity = identity or link.project_identity(cwd)
    path = link.membership_path(base, identity, session_id, pinned)
    record = link._read(path)
    join = {
        **identity,
        "handle": pinned or handle,
        "harness": harness or link.harness(agent),
        "machine": link.machine(),
        "mode": mode,
    }
    if version:
        join["plugin_version"] = version  # servers without the field ignore it
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
    pointer.update(turn="idle", turn_at=time.time(), ended=False)
    link.save_session(base, session_id, **pointer)
    return {
        "handle": record.get("handle") or handle,
        "project": project,
        "channel_name": channel_name,
        "channel_id": connection["channel_id"],
        "people": people,
        "record": record,
        "version": version,
    }


def mcp_present() -> bool:
    """Whether this plugin ships ptt_mcp.py with an MCP config the harness loads."""
    if routing.setting("AGENT_PTT_MCP", "1") == "0":
        return False
    here = Path(__file__).resolve().parent
    configs = (
        here.parent / ".mcp.json",
        here / ".mcp.json",
        here / "mcp_config.json",
        here / "mcp.json",  # Cursor
    )
    return (here / "ptt_mcp.py").is_file() and any(c.is_file() for c in configs)


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
    joined = join_session(base, session_id, agent, cwd)
    route = ""
    if agent.lower() == "codex":
        route = codex_route()
        extra = {"wake_route": route}
        if route == "queue":
            extra["codex_pid"] = link.codex_pid()
        link.save_session(base, session_id, **extra)
        if route == "queue":
            link.start_codex_watch(base, session_id, extra["codex_pid"])
    return compose(
        agent=agent,
        session_id=session_id,
        handle=joined["handle"],
        project=joined["project"],
        channel_name=joined["channel_name"],
        people=joined["people"],
        member_roles=joined["record"].get("roles") or link.roles(),
        codex_route=route,
        version=joined["version"],
        mcp=mcp_present(),
    )


def main() -> None:
    if os.environ.get("AGENT_PTT_HOOKS", "1") == "0":
        return
    with contextlib.suppress(Exception):
        event = json.load(sys.stdin)
        if event.get("hook_event_name", "SessionStart") != "SessionStart":
            return
        if routing.foreign_cursor_hook(event):
            return  # Cursor's Claude import: the Cursor plugin joins instead
        context = start(event)
        if context:
            hook = {"hookEventName": "SessionStart", "additionalContext": context}
            output = {"hookSpecificOutput": hook}
            print(json.dumps(output), flush=True)


if __name__ == "__main__":
    main()
    sys.exit(0)
