#!/usr/bin/env python3
"""Antigravity hook adapter: runs the shared Agent PTT hook scripts for Antigravity.

Antigravity (agy CLI, App, IDE) sends camelCase payloads (``conversationId``,
``workspacePaths``, ``lastUserInput``) and expects its own outputs
(``injectSteps``, Stop ``decision: continue``). This script translates one event
into the Claude/Codex shape the shared scripts read, runs them, and translates
what they print back:

- ``SessionStart``  → session_start.py; its context becomes a system message.
- ``PreInvocation`` → before every model call. The first one after a new user
  prompt plays UserPromptSubmit (doing, announce, status, receive); later ones
  play PostToolUse (status, receive), so mentions arrive between tool calls.
- ``Stop``          → announce, status, receive. A ``block`` from receive becomes
  ``decision: continue`` with the delivery as the reason. A Stop the user
  cancelled (Esc) never waits or continues.
- ``PreToolUse`` (run_command) → ``auto_approve`` for the plugin's own ``ptt``
  command; ``allow`` (the normal permission flow) for anything else. An empty
  answer would deny the command.

Stdlib-only; failures stay silent (Antigravity fails open on a bad hook).
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

HERE = Path(__file__).resolve().parent
AGENT = "Antigravity"
STATE_DIR = Path.home() / ".agent-ptt" / "antigravity"


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


def shared_event(name: str, raw: dict, **extra) -> dict:
    paths = raw.get("workspacePaths") or []
    return {
        "hook_event_name": name,
        "session_id": raw.get("conversationId") or "",
        "cwd": raw.get("cwd") or (paths[0] if paths else os.getcwd()),
        "transcript_path": raw.get("transcriptPath") or "",
        "prompt": raw.get("lastUserInput") or "",
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


def inject(texts: list[str]) -> dict:
    if not texts:
        return {}
    return {"injectSteps": [{"systemMessage": {"systemMessage": "\n\n".join(texts)}}]}


def session_start(raw: dict) -> dict:
    return inject(contexts(run("session_start.py", shared_event("SessionStart", raw))))


def pre_invocation(raw: dict) -> dict:
    conversation = raw.get("conversationId") or ""
    state = load_state(conversation)
    prompt = raw.get("lastUserInput") or ""
    new_prompt = bool(prompt) and prompt != state.get("last_input")
    if raw.get("initialNumSteps") != state.get("initial_steps"):
        state["continued"] = False  # a new execution: the Stop loop guard starts over
    state.update(last_input=prompt, initial_steps=raw.get("initialNumSteps"), at=time.time())
    save_state(conversation, state)
    if new_prompt:
        event = shared_event("UserPromptSubmit", raw)
        outputs = run("doing.py", event)
        dispatch("announce.py", event)
        dispatch("status.py", event)
    else:
        event = shared_event("PostToolUse", raw, tool_name="model_call")
        outputs = []
        dispatch("status.py", event)
    outputs += run("receive.py", event)
    return inject(contexts(outputs))


def stop(raw: dict) -> dict:
    conversation = raw.get("conversationId") or ""
    state = load_state(conversation)
    if raw.get("terminationReason") == "USER_CANCELED":
        state["continued"] = False
        save_state(conversation, state)
        return {}  # Esc means stop: never hold or re-enter the turn
    event = shared_event("Stop", raw, stop_hook_active=bool(state.get("continued")))
    dispatch("announce.py", event)
    dispatch("status.py", event)
    reasons = [
        o["reason"]
        for o in run("receive.py", event)
        if o.get("decision") == "block" and o.get("reason")
    ]
    state["continued"] = bool(reasons)
    save_state(conversation, state)
    if not reasons:
        return {}
    return {"decision": "continue", "reason": "\n\n".join(reasons)}


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


def pre_tool_use(raw: dict) -> dict:
    call = raw.get("toolCall") or {}
    args = call.get("args") or {}
    command = str(args.get("CommandLine") or args.get("command") or "").strip()
    if os.environ.get("AGENT_PTT_AGY_ALLOW", "1") != "0" and ptt_command_ok(command):
        return {"decision": "auto_approve", "reason": "Agent PTT: the plugin's own ptt command"}
    return {"decision": "allow"}  # no opinion: the normal permission flow (an empty answer denies)


def is_main(raw: dict) -> bool:
    """False for subagents: their conversations never carry user input.

    A main conversation's SessionStart fires before the first model call, with the
    user's prompt (or the -p prompt) already in ``lastUserInput``; a subagent's
    conversation starts with it empty and keeps it empty. Subagents get no identity.
    """
    conversation = raw.get("conversationId") or ""
    state = load_state(conversation)
    if state.get("main"):
        return True
    if not raw.get("lastUserInput"):
        return False
    state["main"] = True
    save_state(conversation, state)
    return True


HANDLERS = {
    "SessionStart": session_start,
    "PreInvocation": pre_invocation,
    "Stop": stop,
    "PreToolUse": pre_tool_use,
}


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    # PreToolUse must answer even on failure: an empty answer denies the tool call.
    output: dict = {"decision": "allow"} if name == "PreToolUse" else {}
    if os.environ.get("AGENT_PTT_HOOKS", "1") != "0":
        with contextlib.suppress(Exception):
            raw = json.load(sys.stdin)
            name = name or raw.get("hookEventName", "")
            handler = HANDLERS.get(name)
            if handler and raw.get("conversationId") and is_main(raw):
                output = handler(raw)
            debug(name, raw, output)
    print(json.dumps(output), flush=True)


def debug(name: str, raw: dict, output: dict) -> None:
    """AGENT_PTT_AGY_DEBUG=1: append each event and answer to ~/.agent-ptt/antigravity/."""
    if os.environ.get("AGENT_PTT_AGY_DEBUG", "") != "1":
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"at": time.time(), "event": name, "in": raw, "out": output}
    with (STATE_DIR / "debug.jsonl").open("a") as log:
        log.write(json.dumps(entry) + "\n")


if __name__ == "__main__":
    main()
    sys.exit(0)
