#!/usr/bin/env python3
"""Agent PTT crew hook — real Claude Code sessions reporting into one channel.

Wire this into any number of Claude Code sessions working on the same repo and
they become a crew that works out loud:

  UserPromptSubmit  ->  "Starting: <first line of the prompt>"
  Stop              ->  says what the session just did, then hands it anything
                        the room said that concerns it, so it keeps working

The Stop hook is where the interesting part lives. When a session finishes a
turn it speaks one sentence, and if someone spoke while it was working it is
continued with those words as new input instead of going idle.

Not everything gets relayed, or the crew would spend the day answering each
other. A human's message always is — they are at the table and their word is a
decision. A teammate's message only when it names this agent, i.e. when they
actually need something to keep going.

Configuration, all via environment (set them before launching `claude`):

  AGENT_PTT_URL        server base URL          (default http://localhost:8770)
  AGENT_PTT_CHANNEL    channel name to find or create  (default "Crew")
  AGENT_PTT_CHANNEL_ID exact channel id, wins over the name
  AGENT_PTT_HANDLE     what this session is called     (default "Claude · <dir>")
  AGENT_PTT_VOICE      pin a specific voice id, e.g. fantine
  AGENT_PTT_CREW       comma-separated teammate handles — everyone else is human
  AGENT_PTT_ANNOUNCE=0 turn the whole thing off

Stdlib only, and it never breaks the coding session: every failure path exits 0.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("AGENT_PTT_URL", "http://localhost:8770").rstrip("/")
API_KEY = os.environ.get("AGENT_PTT_API_KEY", "").strip()  # server auth, optional
CHANNEL_NAME = os.environ.get("AGENT_PTT_CHANNEL", "Crew")
CHANNEL_ID = os.environ.get("AGENT_PTT_CHANNEL_ID", "")
VOICE_ID = os.environ.get("AGENT_PTT_VOICE") or None
CREW = [h.strip() for h in os.environ.get("AGENT_PTT_CREW", "").split(",") if h.strip()]
STATE_FILE = Path.home() / ".agent-ptt" / "crew-state.json"
HTTP_TIMEOUT = float(os.environ.get("AGENT_PTT_TIMEOUT", "20"))
MAX_SPOKEN_CHARS = 220
MAX_RELAYED = 6
SETTLE_SECONDS = 4.0  # Stop fires before the final message lands in the transcript
SETTLE_POLL = 0.2


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def _request(method: str, path: str, payload: dict | None = None, timeout: float | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def _channel() -> str:
    if CHANNEL_ID:
        return CHANNEL_ID
    for channel in _request("GET", "/channels", timeout=2.0):
        if channel.get("name") == CHANNEL_NAME:
            return channel["channel_id"]
    return _request("POST", "/channels", {"name": CHANNEL_NAME})["channel_id"]


def _join(channel_id: str, handle: str) -> str:
    payload = {"handle": handle}
    if VOICE_ID:
        payload["voice_id"] = VOICE_ID
    return _request("POST", f"/channels/{channel_id}/join", payload)["key_id"]


def _say(channel_id: str, key_id: str, text: str) -> None:
    _request("POST", f"/channels/{channel_id}/say", {"key_id": key_id, "text": text})


# ---------------------------------------------------------------------------
# State — one participation key per session, and what it has already heard
# ---------------------------------------------------------------------------


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
        pass  # best effort; a lost key just means a rejoin


def _session_key(state: dict, session_id: str, channel_id: str, handle: str) -> str:
    entry = state.setdefault(session_id, {})
    if entry.get("channel_id") != channel_id or not entry.get("key_id"):
        entry.update({"channel_id": channel_id, "key_id": _join(channel_id, handle)})
        _save_state(state)
    return entry["key_id"]


# ---------------------------------------------------------------------------
# What this session just did
# ---------------------------------------------------------------------------


def _last_assistant_text(transcript_path: str) -> str:
    """The final assistant message in the session transcript, as plain text."""
    text = ""
    with open(transcript_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            message = entry.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = [b.get("text", "") for b in content if b.get("type") == "text"]
                if any(p.strip() for p in parts):
                    text = "\n".join(parts)
    return text


def _spoken_line(text: str) -> str:
    """Prefer an explicit `SAY: ...` line; otherwise the first real sentence."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("SAY:"):
            return _clean(stripped[4:])

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "```", "|", "-", "*", ">")):
            continue
        sentence = re.split(r"(?<=[.!?])\s", stripped)[0]
        return _clean(sentence)
    return ""


def _clean(text: str) -> str:
    text = re.sub(r"[`*_#]+", "", text).strip()
    text = " ".join(text.split())
    if len(text) > MAX_SPOKEN_CHARS:
        text = text[:MAX_SPOKEN_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def _settled_line(transcript_path: str, already_said: str) -> str:
    """The line for the turn that just ended — waiting for it if we have to.

    Stop fires the moment the turn is over, which is a little before the final
    assistant message is flushed to the transcript. Read too early and you
    announce a mid-turn aside instead of the report, which is how Roy came to
    say "Looking at the current channel list implementation first."

    So we poll until we see a message carrying the `SAY:` line the rules ask
    for, and settle for the best sentence we saw if it never arrives.
    """
    deadline = time.monotonic() + SETTLE_SECONDS
    fallback = ""
    while True:
        text = _last_assistant_text(transcript_path)
        line = _spoken_line(text)
        if line and "SAY:" in text.upper():
            return line
        if line and line != already_said:
            fallback = line
        if time.monotonic() >= deadline:
            return fallback
        time.sleep(SETTLE_POLL)


# ---------------------------------------------------------------------------
# What the room said
# ---------------------------------------------------------------------------


def _is_teammate(handle: str) -> bool:
    if CREW:
        return handle in CREW
    return " · " in handle  # the announcer's "Claude · project" shape


def _mentions(handle: str, text: str) -> bool:
    """Did they actually ask *us* something, or just talk?"""
    tokens = {handle, handle.split(" · ")[-1], handle.split()[0]}
    lowered = text.lower()
    return any(token and token.lower() in lowered for token in tokens)


def _relevant(messages: list[dict], handle: str) -> list[dict]:
    picked = []
    for msg in messages:
        sender = msg.get("handle", "")
        if sender == handle:
            continue
        if _is_teammate(sender) and not _mentions(handle, msg.get("text", "")):
            continue  # teammates only interrupt when they need something
        picked.append(msg)
    return picked[-MAX_RELAYED:]


def _since(messages: list[dict], last_seen: str) -> list[dict]:
    if not last_seen:
        return []
    ids = [m.get("message_id") for m in messages]
    if last_seen not in ids:
        return messages
    return messages[ids.index(last_seen) + 1 :]


def _continue_reason(messages: list[dict], handle: str) -> str:
    lines = "\n".join(f"- {m.get('handle')}: {m.get('text')}" for m in messages)
    humans = [m.get("handle") for m in messages if not _is_teammate(m.get("handle", ""))]
    who = humans[0] if humans else None
    note = (
        f"{who} is the human at the table — that is a decision, not a suggestion. "
        "Do it now if it touches your work."
        if who
        else "A teammate pinged you by name, which means they need something to keep going."
    )
    return (
        f"The room spoke while you were working:\n\n{lines}\n\n{note} "
        "If none of it concerns your task, carry on with what you were doing. "
        "Finish your reply with a single line `SAY: <one sentence, max 25 words>` "
        "describing what you did — it is read aloud to everyone."
    )


# ---------------------------------------------------------------------------
# Hook entry points
# ---------------------------------------------------------------------------


def _detach() -> None:
    """Fire-and-forget for announcements that can't affect control flow."""
    if os.environ.get("AGENT_PTT_FORK", "1") == "0" or not hasattr(os, "fork"):
        return
    if os.fork() != 0:
        os._exit(0)


def handle_for(event: dict) -> str:
    explicit = os.environ.get("AGENT_PTT_HANDLE")
    if explicit:
        return explicit
    project = Path(event.get("cwd") or ".").name or "somewhere"
    return f"Claude · {project}"


def on_prompt(event: dict, handle: str) -> None:
    prompt = event.get("prompt", "")
    first = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), "")
    if not first:
        return
    _detach()
    channel_id = _channel()
    state = _load_state()
    key_id = _session_key(state, event.get("session_id", "?"), channel_id, handle)
    _say(channel_id, key_id, f"Starting: {_clean(first)}")


def on_stop(event: dict, handle: str) -> None:
    """Report the turn, then hand back anything the room said that concerns us."""
    if event.get("stop_hook_active"):
        return  # already continued once for this turn; don't loop

    channel_id = _channel()
    state = _load_state()
    session_id = event.get("session_id", "?")
    key_id = _session_key(state, session_id, channel_id, handle)
    entry = state[session_id]

    history = _request("GET", f"/channels/{channel_id}/history")
    heard = _relevant(_since(history, entry.get("last_seen", "")), handle)

    spoken = ""
    with contextlib.suppress(Exception):
        spoken = _settled_line(event.get("transcript_path", ""), entry.get("last_spoken", ""))
    if spoken and spoken != entry.get("last_spoken"):
        entry["last_spoken"] = spoken
        with contextlib.suppress(urllib.error.HTTPError, urllib.error.URLError):
            _say(channel_id, key_id, spoken)
        history = _request("GET", f"/channels/{channel_id}/history")

    entry["last_seen"] = history[-1]["message_id"] if history else ""
    _save_state(state)

    if heard:
        print(json.dumps({"decision": "block", "reason": _continue_reason(heard, handle)}))


def main() -> None:
    if os.environ.get("AGENT_PTT_ANNOUNCE", "1") == "0":
        return

    event = json.load(sys.stdin)
    handle = handle_for(event)
    name = event.get("hook_event_name", "")

    if name == "UserPromptSubmit":
        on_prompt(event, handle)
    elif name == "Stop":
        on_stop(event, handle)


if __name__ == "__main__":
    with contextlib.suppress(Exception):
        main()
    sys.exit(0)
