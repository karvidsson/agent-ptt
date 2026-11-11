#!/usr/bin/env python3
"""Cursor hook adapter: runs the shared Agent PTT hook scripts for Cursor.

Cursor (the ``agent`` / ``cursor-agent`` CLI and the IDE agent) sends snake_case
payloads with ``conversation_id`` and expects its own outputs
(``additional_context``, ``followup_message``, ``permission``). This script
translates one event into the Claude/Codex shape the shared scripts read, runs
them with ``AGENT_PTT_AGENT=Cursor``, and translates what they print back:

- ``sessionStart``       → session_start.py; its context becomes ``additional_context``.
- ``beforeSubmitPrompt`` → doing, announce, status. Cursor injects no context here,
  so nothing is delivered (mentions come after the next tool call). Cursor may
  fire it before ``sessionStart``; the session then joins here first.
- ``postToolUse``        → status, receive; mentions become ``additional_context``.
- ``stop``               → announce, status, receive. A ``block`` from receive becomes
  ``followup_message`` (Cursor submits it as the next user message). A stop the
  user aborted never continues.
- ``sessionEnd``         → announce (leaves the channel).
- ``beforeShellExecution`` / ``beforeMCPExecution`` → ``allow`` for a plain call of
  this plugin's own ``ptt.py`` and for this plugin's own agent-ptt MCP tools; ``{}``
  (no opinion: Cursor's normal approval flow) for anything else. The CLI still
  asks before running them unless its allowlist has them (``ptt cursor-rules``).

Only conversations that had a ``sessionStart`` or a user prompt get an identity:
subagents run in conversations of their own that have neither.

Stdlib-only; failures stay silent (Cursor fails open on a crashed hook, but an
invalid answer to a permission hook blocks the action, so those always answer).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no join lock, and the permission hooks still answer
    fcntl = None

HERE = Path(__file__).resolve().parent
AGENT = "Cursor"
MCP_SERVER = "agent-ptt"  # mcp.json server name
PLUGIN_MCP_SERVER = "plugin-Agent PTT Announcer-agent-ptt"  # plugin-<displayName>-<server>
STATE_DIR = Path.home() / ".agent-ptt" / "cursor"
PERMISSION_EVENTS = ("beforeShellExecution", "beforeMCPExecution")


def _state_path(conversation: str) -> Path:
    return STATE_DIR / (hashlib.sha256(conversation.encode()).hexdigest()[:32] + ".json")


def load_state(conversation: str) -> dict:
    try:
        return json.loads(_state_path(conversation).read_text())
    except (OSError, ValueError):
        return {}


def save_state(conversation: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(conversation)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as out:
        json.dump(state, out)
    os.replace(out.name, path)


def update_state(conversation: str, **fields) -> dict:
    state = load_state(conversation)
    state.update(fields)
    save_state(conversation, state)
    return state


@contextlib.contextmanager
def join_lock(conversation: str):
    """One join at a time per conversation (sessionStart and an early prompt may race)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with _state_path(conversation).with_suffix(".lock").open("a") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def conversation_of(raw: dict) -> str:
    return str(raw.get("conversation_id") or raw.get("session_id") or "")


def shared_event(name: str, raw: dict, **extra) -> dict:
    roots = raw.get("workspace_roots") or []
    cwd = (
        raw.get("cwd")
        or (roots[0] if roots else "")
        or os.environ.get("CURSOR_PROJECT_DIR")
        or os.getcwd()
    )
    return {
        "hook_event_name": name,
        "session_id": conversation_of(raw),
        "cwd": cwd,
        "transcript_path": raw.get("transcript_path")
        or os.environ.get("CURSOR_TRANSCRIPT_PATH")
        or "",
        "prompt": raw.get("prompt") or "",
        **extra,
    }


def run(script: str, event: dict, *args: str, timeout: float = 300) -> list[dict]:
    """Run one shared hook script; return the JSON objects it printed."""
    command = [sys.executable, str(HERE / script), *args]
    env = {**os.environ, "AGENT_PTT_AGENT": AGENT}
    try:
        done = subprocess.run(
            command,
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=event.get("cwd") or None,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in done.stdout.splitlines():
        with contextlib.suppress(ValueError):
            value = json.loads(line)
            if isinstance(value, dict):
                out.append(value)
    return out


def dispatch(target: str, event: dict) -> list[dict]:
    return run("workspace_dispatch.py", event, str(HERE / target))


def contexts(outputs: list[dict]) -> list[str]:
    texts = []
    for output in outputs:
        hook = output.get("hookSpecificOutput") or {}
        if hook.get("additionalContext"):
            texts.append(hook["additionalContext"])
        elif output.get("decision") == "block" and output.get("reason"):
            texts.append(output["reason"])
    return texts


def context_output(texts: list[str]) -> dict:
    return {"additional_context": "\n\n".join(texts)} if texts else {}


def join(raw: dict) -> list[dict]:
    """Run session_start.py once per conversation; returns what it printed."""
    conversation = conversation_of(raw)
    with join_lock(conversation):
        outputs = run("session_start.py", shared_event("SessionStart", raw), timeout=20)
        update_state(conversation, main=True, joined_at=time.time())
    return outputs


def session_start(raw: dict) -> dict:
    return context_output(contexts(join(raw)))


def before_submit_prompt(raw: dict) -> dict:
    conversation = conversation_of(raw)
    if not load_state(conversation).get("joined_at"):
        join(raw)  # the prompt came first: join now, sessionStart repeats it with context
    event = shared_event("UserPromptSubmit", raw)
    run("doing.py", event, timeout=10)
    dispatch("announce.py", event)
    dispatch("status.py", event)
    return {"continue": True}


def post_tool_use(raw: dict) -> dict:
    event = shared_event("PostToolUse", raw, tool_name=raw.get("tool_name") or "")
    event["tool_input"] = raw.get("tool_input") or {}
    dispatch("status.py", event)
    return context_output(contexts(run("receive.py", event)))


def stop(raw: dict) -> dict:
    if raw.get("status") == "aborted":
        return {}  # Esc means stop: never hold or re-enter the turn
    # loop_count > 0: this turn is itself a follow-up; receive.py's loop guard applies.
    looped = bool(raw.get("loop_count"))
    event = shared_event("Stop", raw, stop_hook_active=looped)
    dispatch("announce.py", event)
    dispatch("status.py", event)
    reasons = [
        o["reason"]
        for o in run("receive.py", event)
        if o.get("decision") == "block" and o.get("reason")
    ]
    return {"followup_message": "\n\n".join(reasons)} if reasons else {}


def session_end(raw: dict) -> dict:
    dispatch("announce.py", shared_event("SessionEnd", raw))
    return {}


def plain_shell(command: str) -> bool:
    """True when ``sh -c command`` is one simple command with no expansion.

    Outside quotes: no operators, redirections, subshells, comments, newlines or
    escapes. Inside double quotes: no ``$``, backticks or backslashes. Single
    quotes may hold anything (sh expands nothing there), newlines included, so
    a multi-line ``ptt say '...'`` still counts as plain.
    """
    quote = ""
    for ch in command:
        if quote == "'":
            if ch == "'":
                quote = ""
        elif quote == '"':
            if ch in "$`\\":
                return False
            if ch == '"':
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in ";&|<>()`$\\#\n\r{}*?[~!":
            return False
    return not quote


def ptt_command_ok(command: str) -> bool:
    """True for a plain call of this plugin's own ptt.py (see ``plain_shell``)."""
    if not command or not plain_shell(command):
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    if len(tokens) < 3:
        return False
    python, script = tokens[0], tokens[1]
    if Path(script).name != "ptt.py":
        return False
    try:
        if Path(script).resolve() != (HERE / "ptt.py").resolve():
            return False
    except OSError:
        return False
    return Path(python).name.startswith("python")


def own_mcp_call(raw: dict) -> bool:
    """True for a call to this plugin's own agent-ptt MCP server.

    Cursor names a plugin's server ``plugin-<displayName>-<server>`` and sends the
    server's command along: it must run this plugin's ptt_mcp.py.
    """
    server = str(raw.get("mcp_server_name") or "")
    command = str(raw.get("command") or "")
    if command:
        return server.endswith(MCP_SERVER) and str(HERE / "ptt_mcp.py") in command
    return server == PLUGIN_MCP_SERVER


def permission(raw: dict) -> dict:
    """``allow`` for our own ptt and MCP calls, else ``{}`` (Cursor's normal flow)."""
    if os.environ.get("AGENT_PTT_CURSOR_ALLOW", "1") == "0":
        return {}
    if raw.get("hook_event_name") == "beforeMCPExecution":
        ok = own_mcp_call(raw)
    else:
        ok = ptt_command_ok(str(raw.get("command") or "").strip())
    return {"permission": "allow"} if ok else {}


def is_main(name: str, raw: dict) -> bool:
    """False for subagents: they run in conversations that never had a sessionStart
    or a user prompt. sessionStart and beforeSubmitPrompt mark a conversation main."""
    if name in ("sessionStart", "beforeSubmitPrompt"):
        return True
    return bool(load_state(conversation_of(raw)).get("main"))


HANDLERS = {
    "sessionStart": session_start,
    "beforeSubmitPrompt": before_submit_prompt,
    "postToolUse": post_tool_use,
    "stop": stop,
    "sessionEnd": session_end,
    "beforeShellExecution": permission,
    "beforeMCPExecution": permission,
}


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    output: dict = {"continue": True} if name == "beforeSubmitPrompt" else {}
    if os.environ.get("AGENT_PTT_HOOKS", "1") != "0":
        raw: dict = {}
        error = ""
        try:
            raw = json.load(sys.stdin)
            name = name or raw.get("hook_event_name", "")
            handler = HANDLERS.get(name)
            if handler is permission:
                output = permission(raw)  # answers for subagents too: it's only our ptt
            elif handler and conversation_of(raw) and is_main(name, raw):
                output = handler(raw)
        except Exception as exc:  # never break the session
            error = repr(exc)
        with contextlib.suppress(Exception):
            debug(name, raw, output, error)
    print(json.dumps(output), flush=True)


def debug(name: str, raw: dict, output: dict, error: str = "") -> None:
    """AGENT_PTT_CURSOR_DEBUG=1: append each event and answer to ~/.agent-ptt/cursor/."""
    if os.environ.get("AGENT_PTT_CURSOR_DEBUG", "") != "1":
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"at": time.time(), "event": name, "in": raw, "out": output}
    if error:
        entry["error"] = error
    with (STATE_DIR / "debug.jsonl").open("a") as log:
        log.write(json.dumps(entry) + "\n")


if __name__ == "__main__":
    main()
    sys.exit(0)
