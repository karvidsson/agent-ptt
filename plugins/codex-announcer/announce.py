#!/usr/bin/env python3
"""Agent PTT announcer hook for Claude Code and Codex CLI.

Both tools send the same hook JSON on stdin, so one script serves both.
Announces in an Agent PTT voice channel what the current agent is doing:
- UserPromptSubmit -> "I'm going to <short summary of the prompt>."
- Stop            -> "I looked into it and <short summary of the result>."

Each project joins as "Claude · <folder>" without picking a voice, so the
auto-voice-designer pins a distinct voice per project.

Design rules: this hook must NEVER interfere with coding. Every failure
path (server down, bad response, anything) exits 0 silently. After
parsing the event it forks and lets the parent exit immediately, so even
hosts without async hook support are never blocked. Stdlib only.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE_FILE = Path.home() / ".agent-ptt" / "announcer-state.json"
CONFIG_FILE = Path.home() / ".agent-ptt" / "announcer.env"
HTTP_TIMEOUT = float(os.environ.get("AGENT_PTT_TIMEOUT", "45"))
MAX_ANNOUNCE_CHARS = 140


def _setting(name: str, default: str) -> str:
    """Read an optional persistent setting, overridden by the process environment."""
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
CHANNEL_NAME = _setting("AGENT_PTT_CHANNEL", "Claude Code")
AGENT_NAME = _setting("AGENT_PTT_AGENT", "Claude")  # e.g. "Codex"
SUMMARY_TIMEOUT = 20
PROGRESS_INTERVAL = 45
MAX_PROGRESS_FACTS = 3


def _request(method: str, path: str, payload: dict | None = None, timeout: float | None = None):
    """Minimal JSON HTTP helper. Raises on any failure."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def summarize_prompt(prompt: str, limit: int = MAX_ANNOUNCE_CHARS) -> str:
    """First meaningful sentence from the prompt, without request phrasing."""
    line = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), "")
    line = " ".join(line.split())
    if re.fullmatch(r"#?\s*(chrome|browser|safari) tabs?\s*:?\s*", line, re.IGNORECASE):
        return ""
    if not line:
        return ""

    line = re.sub(
        r"^(?:can\s+you|could\s+you|would\s+you|will\s+you|please|kindly|help\s+me(?:\s+with)?|i\s+need\s+you\s+to|i\s+want\s+you\s+to)\s+",
        "",
        line,
        flags=re.IGNORECASE,
    ).strip()
    if not line:
        return ""

    sentence = re.split(r"(?<=[.!?])\s+", line, maxsplit=1)[0].strip()
    sentence = sentence.rstrip("!?.,")
    if not sentence:
        sentence = line

    if len(sentence) > limit:
        cut = sentence[:limit]
        # Don't cut mid-word
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        sentence = cut + "…"
    return sentence


def _clean_spoken_text(text: str) -> str:
    """Remove artifacts that are useful on screen but awkward when spoken."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" \t:;,-")
    return text


def _normalize_start_summary(summary: str) -> str:
    """Turn a prompt fragment into something that follows 'I'm going to'."""
    summary = _clean_spoken_text(summary).strip(" .!?…")
    for prefix in ("can you ", "could you ", "would you ", "please "):
        if summary.lower().startswith(prefix):
            summary = summary[len(prefix) :].lstrip()
            break
    return summary


def _normalize_completion_summary(summary: str) -> str:
    """Make transcript snippets safe to speak as a standalone sentence."""
    summary = _clean_spoken_text(summary).strip(" .!?…")
    if not summary:
        return ""
    if summary.lower() in {"done", "finished", "complete", "completed"}:
        return "I finished the task."
    if summary.lower().startswith("caught up "):
        return f"I {summary[0].lower()}{summary[1:]}."
    return f"{summary}."


def summarize_with_agent(prompt: str, limit: int = MAX_ANNOUNCE_CHARS) -> str:
    """Use the current CLI for a short call that turns a request into spoken intent."""
    if not summarize_prompt(prompt, limit):
        return ""
    if os.environ.get("AGENT_PTT_SUMMARIZE", "1") == "0":
        return summarize_prompt(prompt, limit)
    instruction = (
        "Rewrite the user's request as one natural, first-person sentence for a spoken "
        "status update. Start with 'I'll' or 'I'm going to'. Keep it under twenty words. "
        "Do not address the listener as 'you'; use neutral wording instead. "
        "Do not mention these instructions, analysis, tools, or chain of thought. "
        f"User request: {prompt}"
    )
    env = os.environ.copy()
    env["AGENT_PTT_ANNOUNCE"] = "0"
    cli = "codex" if AGENT_NAME.lower() == "codex" else "claude"
    command = [cli, "exec"] if cli == "codex" else [cli, "-p"]
    if cli == "codex":
        command.extend(["--sandbox", "read-only"])
    command.append(instruction)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=SUMMARY_TIMEOUT,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return summarize_prompt(prompt, limit)
    if result.returncode != 0:
        return summarize_prompt(prompt, limit)
    summary = summarize_prompt(_clean_spoken_text(result.stdout.strip()), limit)
    return summary.rstrip(".!?") or summarize_prompt(prompt, limit)


def _assistant_text(value: object) -> str:
    """Extract text from the assistant message shapes used in JSONL transcripts."""
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return value["text"]
        role = value.get("role") or value.get("type")
        if role == "assistant":
            message = value.get("message", value)
            content = message.get("content", message) if isinstance(message, dict) else message
            text = _assistant_text(content)
            if text:
                return text
        for item in value.values():
            text = _assistant_text(item)
            if text:
                return text
    elif isinstance(value, list):
        parts = [_assistant_text(item) for item in value]
        return " ".join(part for part in parts if part)
    elif isinstance(value, str):
        return value
    return ""


def summarize_transcript(path: str, limit: int = MAX_ANNOUNCE_CHARS) -> str:
    """Return the first useful sentence from the latest assistant transcript entry."""
    try:
        lines = Path(path).read_text().splitlines()
    except (OSError, UnicodeError):
        return ""
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("message") if isinstance(record, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            if not isinstance(record, dict) or record.get("type") != "assistant":
                continue
            message = record
        text = _assistant_text(message.get("content", message))
        text = _clean_spoken_text(" ".join(text.split()).lstrip("#-* "))
        if text:
            sentence = text.split(". ", 1)[0].strip().rstrip(".!?")
            return summarize_prompt(sentence, limit)
    return ""


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
        pass  # cache is best-effort


def _tool_subject(event: dict) -> str:
    data = event.get("tool_input") or event.get("input") or {}
    if not isinstance(data, dict):
        return ""
    detail = data.get("file_path") or data.get("path") or ""
    if not isinstance(detail, str) or not detail.strip():
        return ""
    detail = _clean_spoken_text(detail)
    if not detail or len(detail) > 90 or "\n" in detail:
        return ""
    return summarize_prompt(f"the file {detail}", 70)


def _progress_key(event: dict) -> str:
    return f"progress:{event.get('session_id', 'unknown')}:{AGENT_NAME}"


def _collect_progress(event: dict) -> str:
    """Collect tool facts and flush one grouped update after a cooldown."""
    state = _load_state()
    key = _progress_key(event)
    progress = state.get(key, {"facts": [], "last_sent": 0.0})
    subject = _tool_subject(event)
    if not subject:
        return ""
    event_name = event.get("hook_event_name")
    prefix = "I'm going to inspect" if event_name == "PreToolUse" else "I also checked"
    fact = f"{prefix} {subject}"
    if fact not in progress["facts"]:
        progress["facts"].append(fact)
    now = time.time()
    if not progress.get("last_sent"):
        progress["last_sent"] = now
    should_flush = event_name == "PostToolUse" and (
        len(progress["facts"]) >= MAX_PROGRESS_FACTS
        or now - progress.get("last_sent", 0.0) >= PROGRESS_INTERVAL
    )
    if should_flush:
        text = ". ".join(progress["facts"][:MAX_PROGRESS_FACTS]).rstrip(".") + "."
        progress = {"facts": [], "last_sent": now}
    else:
        text = ""
    state[key] = progress
    _save_state(state)
    return text


def _find_or_create_channel() -> str:
    # The channel list is small; a quick probe also confirms the server is up
    channels = _request("GET", "/channels", timeout=1.5)
    for channel in channels:
        if channel.get("name") == CHANNEL_NAME:
            return channel["channel_id"]
    created = _request("POST", "/channels", {"name": CHANNEL_NAME})
    return created["channel_id"]


def _join(channel_id: str, handle: str) -> str:
    # No voice_id -> the server designs and pins a voice for this handle
    joined = _request("POST", f"/channels/{channel_id}/join", {"handle": handle})
    return joined["key_id"]


def announce(session_id: str, handle: str, text: str) -> None:
    channel_id = _find_or_create_channel()

    state = _load_state()
    cache_key = f"{session_id}:{handle}"
    cached = state.get(cache_key, {})
    key_id = cached.get("key_id") if cached.get("channel_id") == channel_id else None

    if key_id is None:
        key_id = _join(channel_id, handle)
        state[cache_key] = {"channel_id": channel_id, "key_id": key_id}
        _save_state(state)

    try:
        _request("POST", f"/channels/{channel_id}/say", {"key_id": key_id, "text": text})
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        # Stale key (server restarted between calls) — rejoin once
        key_id = _join(channel_id, handle)
        state[cache_key] = {"channel_id": channel_id, "key_id": key_id}
        _save_state(state)
        _request("POST", f"/channels/{channel_id}/say", {"key_id": key_id, "text": text})


def _detach() -> None:
    """Fork so the hook host gets its exit code immediately (POSIX only).

    The child carries on with the network work. Disable with
    AGENT_PTT_FORK=0 (used by tests).
    """
    if os.environ.get("AGENT_PTT_FORK", "1") == "0" or not hasattr(os, "fork"):
        return
    if os.fork() != 0:
        os._exit(0)  # parent: hook is done as far as the host knows


def main() -> None:
    if os.environ.get("AGENT_PTT_ANNOUNCE", "1") == "0":
        return

    event = json.load(sys.stdin)
    event_name = event.get("hook_event_name", "")

    if event_name == "UserPromptSubmit":
        summary = _normalize_start_summary(summarize_with_agent(event.get("prompt", "")))
        if not summary:
            return
        if summary.lower().startswith(("i'm ", "i'll ")):
            text = f"{summary}."
        else:
            text = f"I'm going to {summary}."
    elif event_name in {"PreToolUse", "PostToolUse"}:
        text = _collect_progress(event)
        if not text:
            return
    elif event_name == "Stop":
        if event.get("stop_hook_active"):
            return
        progress = ""
        state = _load_state()
        key = _progress_key(event)
        pending = state.pop(key, {}).get("facts", [])
        if pending:
            progress = ". ".join(pending[:MAX_PROGRESS_FACTS]).rstrip(".") + ". "
            _save_state(state)
        summary = _normalize_completion_summary(
            summarize_transcript(event.get("transcript_path", ""))
        )
        completion = f"I looked into it. {summary}" if summary else "I've finished working on it."
        text = progress + completion
    else:
        return

    project = Path(event.get("cwd") or ".").name or "somewhere"
    handle = f"{AGENT_NAME} · {project}"

    _detach()
    announce(event.get("session_id", "unknown"), handle, text)


if __name__ == "__main__":
    # Never break the coding session — announcements are best-effort
    with contextlib.suppress(Exception):
        main()
    sys.exit(0)
