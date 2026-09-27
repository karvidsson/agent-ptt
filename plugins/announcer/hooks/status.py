#!/usr/bin/env python3
"""Agent PTT status hook: lets people ask an agent in the channel what it's doing.

Runs next to announce.py on the same hook events and does two things:

1. Records a small per-session status file (task, elapsed time, latest
   narration, what the next tool call is about, done/working) under
   ~/.agent-ptt/status/.
2. Keeps one background responder per channel alive. The responder polls
   the channel and, when someone asks "status?" or "what are you working
   on?", replies in the agent's own voice from the status file:

   "I've been on 'fix the flaky login test' for 12 minutes. I just said:
   the cause is a shared fixture. Right now I'm about to run the tests."

Design rules match announce.py: never interfere with coding, every failure
exits 0 silently, stdlib only. Speaking as the agent reuses the participation
key announce.py cached in announcer-state.json, so the voice matches.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

STATE_FILE = Path.home() / ".agent-ptt" / "announcer-state.json"
STATUS_DIR = Path.home() / ".agent-ptt" / "status"
CONFIG_FILE = Path.home() / ".agent-ptt" / "announcer.env"
MAX_CHARS = 140
POLL_SECONDS = 2.0
STALE_AFTER = 10 * 60  # a status older than this is reported as silence, not as current work
RESPONDER_IDLE_EXIT = 60 * 60  # stop when no session in the channel has reported for an hour
PING = re.compile(r"\b(ping|marco|are you there|you there|still there|alive)\b", re.IGNORECASE)
QUESTION = re.compile(
    r"\bstatus\b|what are you (?:doing|working on|up to)"
    r"|where are you(?: at)?|how(?:'s| is) it going",
    re.IGNORECASE,
)


def _setting(name: str, default: str) -> str:
    if name in os.environ:
        return os.environ[name]
    try:
        for line in CONFIG_FILE.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip().strip("\"'")
    except (OSError, UnicodeError):
        pass
    return default


BASE_URL = _setting("AGENT_PTT_URL", "http://localhost:8770").rstrip("/")
AGENT_NAME = _setting("AGENT_PTT_AGENT", "Claude")


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


def _request(method: str, path: str, payload: dict | None = None, timeout: float = 10.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, method=method, headers=_headers())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except Exception:
        pass


def _short(text: str, limit: int = MAX_CHARS) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[`*#_]+", "", text)
    text = " ".join(text.split()).strip(" .!?:;,-")
    if len(text) > limit:
        cut = text[:limit]
        text = (cut.rsplit(" ", 1)[0] if " " in cut else cut) + "…"
    return text


# ---------------------------------------------------------------------------
# Recording status from hook events
# ---------------------------------------------------------------------------


def status_path(session_id: str) -> Path:
    return STATUS_DIR / f"{session_id}.json"


def _channel_for_session(session_id: str) -> tuple[str, str] | None:
    """(channel_id, key_id) that announce.py cached for this session, if any."""
    found = None
    for key, value in _read_json(STATE_FILE).items():  # insertion order: newest entry wins
        if not (session_id in key and isinstance(value, dict)):
            continue
        if value.get("key_id") and value.get("channel_id"):
            found = (value["channel_id"], value["key_id"])
    return found


def _latest_narration(path: str, tail_bytes: int = 1_500_000) -> str:
    """What the agent last said to the user in this turn (Claude or Codex JSONL).

    Tool results can be hundreds of kilobytes each, so the tail is generous.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - tail_bytes))
            lines = handle.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        message = payload if isinstance(payload, dict) else record.get("message", record)
        if not isinstance(message, dict):
            continue
        if message.get("type") == "task_started":
            break
        role = message.get("role") or record.get("type")
        content = message.get("content")
        if role == "user":
            if isinstance(content, list) and any(
                isinstance(i, dict) and i.get("type") == "tool_result" for i in content
            ):
                continue
            break
        if role != "assistant" or not isinstance(content, list):
            continue
        texts = [
            i.get("text", "")
            for i in content
            if isinstance(i, dict) and i.get("type") in {"text", "output_text"}
        ]
        text = " ".join(t for t in texts if isinstance(t, str)).strip()
        if text:
            return text
    return ""


def _intent(event: dict) -> str:
    data = event.get("tool_input") or event.get("input") or {}
    if not isinstance(data, dict):
        return ""
    description = data.get("description")
    if isinstance(description, str) and description.strip():
        phrase = _short(description, 90)
        return phrase[:1].lower() + phrase[1:]
    path = data.get("file_path") or data.get("path") or ""
    tool = str(event.get("tool_name") or "")
    if isinstance(path, str) and path.strip():
        verb = "edit" if tool in {"Edit", "MultiEdit", "Write", "NotebookEdit"} else "read"
        return f"{verb} {Path(path).name}"
    return ""


def record(event: dict, now: float | None = None) -> dict:
    """Update this session's status file from a hook event and return it."""
    if now is None:
        now = time.time()
    session_id = str(event.get("session_id") or event.get("thread_id") or "unknown")
    path = status_path(session_id)
    status = _read_json(path)
    status.update(
        {
            "session_id": session_id,
            "agent": AGENT_NAME,
            "updated_at": now,
        }
    )
    if event.get("cwd"):
        status["project"] = Path(str(event["cwd"])).name
    name = event.get("hook_event_name")
    if name == "UserPromptSubmit":
        prompt = str(event.get("prompt") or "")
        head = prompt.lstrip()[:400]
        if not (head.startswith("<system-reminder>") or "[SYSTEM NOTIFICATION" in head):
            status.update(
                {
                    "task": _short(prompt.strip().splitlines()[0] if prompt.strip() else ""),
                    "task_started": now,
                    "state": "working",
                    "narration": "",
                    "current": "",
                    "tool_calls": 0,
                    "result": "",
                }
            )
    elif name == "PreToolUse":
        status["state"] = "working"
        status["current"] = _intent(event) or status.get("current", "")
        status["tool_calls"] = int(status.get("tool_calls", 0)) + 1
        narration = _latest_narration(str(event.get("transcript_path") or ""))
        if narration:
            status["narration"] = _short(narration)
    elif name == "Stop":
        status["state"] = "idle"
        status["finished_at"] = now
        status["current"] = ""
        result = _latest_narration(str(event.get("transcript_path") or ""))
        if result:
            status["result"] = _short(result)
    channel = _channel_for_session(session_id)
    if channel:
        status["channel_id"], status["key_id"] = channel
    _write_json(path, status)
    return status


# ---------------------------------------------------------------------------
# Answering status questions in the channel
# ---------------------------------------------------------------------------


def _minutes(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "under a minute"
    if minutes == 1:
        return "a minute"
    if minutes < 120:
        return f"{minutes} minutes"
    return f"{minutes // 60} hours"


def _timestamp(value: object, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def ping_reply(status: dict, now: float | None = None) -> str:
    """The short answer to "are you there?": presence plus what's in hand."""
    if now is None:
        now = time.time()
    task = _short(status.get("task") or "", 60)
    if status.get("state") == "idle":
        return (
            f"Polo. I'm here and idle; I finished '{task}'." if task else "Polo. I'm here and idle."
        )
    quiet = now - _timestamp(status.get("updated_at"), now)
    if quiet > STALE_AFTER:
        return f"Polo. I'm here, but I haven't reported anything for {_minutes(quiet)}."
    return f"Polo. I'm here, on '{task}'." if task else "Polo. I'm here."


def status_reply(status: dict, now: float | None = None) -> str:
    """First-person spoken status for one session."""
    if now is None:
        now = time.time()
    task = _short(status.get("task") or "the current task", 80)
    if status.get("state") == "idle":
        ago = _minutes(now - _timestamp(status.get("finished_at"), now))
        result = status.get("result") or ""
        text = f"I finished '{task}' {ago} ago."
        return f"{text} {result}." if result else text
    quiet = now - _timestamp(status.get("updated_at"), now)
    if quiet > STALE_AFTER:
        parts = [f"I haven't reported anything for {_minutes(quiet)}. Last I was on '{task}'."]
        if status.get("current"):
            parts.append(f"I was about to {status['current']}.")
        return " ".join(parts)
    elapsed = _minutes(now - _timestamp(status.get("task_started"), now))
    parts = [f"I've been on '{task}' for {elapsed}."]
    if status.get("narration"):
        parts.append(f"I just said: {status['narration']}.")
    if status.get("current"):
        parts.append(f"Right now I'm about to {status['current']}.")
    return " ".join(parts)


def channel_statuses(channel_id: str) -> list[dict]:
    statuses = []
    try:
        files = sorted(STATUS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return []
    for path in files:
        status = _read_json(path)
        if status.get("channel_id") == channel_id and status.get("key_id"):
            statuses.append(status)
    return statuses


def _mentioned(text: str, handle: str) -> bool:
    words = [w for w in re.split(r"[^\w]+", handle) if len(w) > 2]
    return any(re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE) for w in words)


def _announcer_keys() -> set[str]:
    """Participation keys of every announcer session on this machine."""
    keys = set()
    for value in _read_json(STATE_FILE).values():
        if isinstance(value, dict) and value.get("key_id"):
            keys.add(value["key_id"])
    return keys


def _looks_like_question(text: str) -> bool:
    """A status question is short and asks, unlike narration that mentions status."""
    text = text.strip()
    if len(text) > 200 or text.lower().startswith(("i'm ", "i'll ", "i am ", "i will ", "i've ")):
        return False
    asks = re.match(r"(?i)(what|where|how|status)\b", text) is not None
    return "?" in text or "@" in text or asks


def answer_question(channel_id: str, message: dict, now: float | None = None) -> list[dict]:
    """Spoken replies (one per matching session) for a status question; [] if none."""
    if message.get("deliveries"):
        return []  # Explicit mentions are handled by the receiving agent hook.
    text = str(message.get("text") or "")
    ping = bool(PING.search(text))
    if not ping and not QUESTION.search(text):
        return []
    statuses = channel_statuses(channel_id)
    if not statuses:
        return []
    if message.get("sender_key") in _announcer_keys() | {s["key_id"] for s in statuses}:
        return []  # an agent's own announcement mentioning "status" is not a question
    if not _looks_like_question(text):
        return []
    try:
        participants = _request("GET", f"/channels/{channel_id}", timeout=3).get("participants", {})
    except Exception:
        participants = {}
    handles = {s["key_id"]: participants.get(s["key_id"], {}).get("handle", "") for s in statuses}
    addressed = [
        s for s in statuses if handles[s["key_id"]] and _mentioned(text, handles[s["key_id"]])
    ]
    if addressed:
        statuses = addressed
    elif len(statuses) > 3:
        statuses = statuses[-3:]  # nobody asked for a roll call of every session ever
    if ping:
        return [{"key_id": s["key_id"], "text": ping_reply(s, now)} for s in statuses]
    return [{"key_id": s["key_id"], "text": status_reply(s, now)} for s in statuses]


def responder(channel_id: str) -> None:
    """Poll the channel and answer status questions until the channel goes quiet."""
    seen = None
    failures = 0
    while True:
        try:
            channel = _request("GET", f"/channels/{channel_id}", timeout=5)
            failures = 0
        except Exception:
            failures += 1
            if failures >= 10:
                return
            time.sleep(POLL_SECONDS * 3)
            continue
        messages = channel.get("messages") or []
        if seen is None or len(messages) < seen:
            seen = len(messages)  # first pass or server restart: don't answer old questions
        for message in messages[seen:]:
            for reply in answer_question(channel_id, message):
                with contextlib.suppress(Exception):
                    _request("POST", f"/channels/{channel_id}/say", reply)
        seen = len(messages)
        statuses = channel_statuses(channel_id)
        latest = max((float(s.get("updated_at") or 0) for s in statuses), default=0)
        if time.time() - latest > RESPONDER_IDLE_EXIT:
            return
        time.sleep(POLL_SECONDS)


def ensure_responder(channel_id: str) -> None:
    """Start the channel's responder if none is alive. Disable with AGENT_PTT_STATUS=0."""
    if os.environ.get("AGENT_PTT_STATUS", "1") == "0":
        return
    pid_file = STATUS_DIR / f"responder-{channel_id}.pid"
    try:
        pid = int(pid_file.read_text())
        os.kill(pid, 0)
        return  # alive
    except (OSError, ValueError):
        pass
    process = subprocess.Popen(
        [sys.executable, __file__, "--responder", channel_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(process.pid))


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--responder":
        responder(sys.argv[2])
        return
    if os.environ.get("AGENT_PTT_ANNOUNCE", "1") == "0":
        return
    event = json.load(sys.stdin)
    if event.get("hook_event_name") not in {
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
    }:
        return
    if "/subagents/agent-" in str(event.get("transcript_path") or ""):
        return  # a worker's events must not overwrite the session's own status
    status = record(event)
    if status.get("channel_id"):
        ensure_responder(status["channel_id"])


if __name__ == "__main__":
    with contextlib.suppress(Exception):
        main()
    sys.exit(0)
