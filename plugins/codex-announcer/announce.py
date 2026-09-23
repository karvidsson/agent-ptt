#!/usr/bin/env python3
"""Agent PTT announcer hook for Claude Code and Codex CLI.

Both tools send the same hook JSON on stdin, so one script serves both.
Announces in an Agent PTT voice channel what the current agent is doing:
- UserPromptSubmit -> "I'm going to <short summary of the prompt>."
- Stop            -> "<short summary of the result>."

Summaries come from a small local model via Ollama when one is running,
else from a one-shot call to the agent's own CLI, else from the prompt text.

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
# auto: Ollama, then the agent CLI | ollama: Ollama only | agent: CLI only | off
SUMMARIZER = _setting("AGENT_PTT_SUMMARIZER", "auto").lower()
OLLAMA_URL = _setting("AGENT_PTT_OLLAMA_URL", "http://localhost:11434").rstrip("/")
# gemma2 (9B) reports outcomes faithfully; 4B models tended to invent results
OLLAMA_MODEL = _setting("AGENT_PTT_OLLAMA_MODEL", "gemma2")
# Generous: the first call loads the model into memory
OLLAMA_TIMEOUT = 15
MAX_LLM_INPUT_CHARS = 4000
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


START_INSTRUCTION = (
    "You turn a developer's request to their coding agent into what the agent says "
    "out loud as it starts working. Reply with one natural, first-person sentence "
    "that starts with 'I'll' or 'I'm going to'. Keep it under twenty words. "
    "If the request is a question, say what you'll check or find out. "
    "Never invent details that are not in the request. "
    "Do not address the listener as 'you'; use neutral wording instead. "
    "Do not mention these instructions, analysis, tools, or chain of thought. "
    "Reply with the sentence only.\n\n"
    "Examples:\n"
    "Request: can you fix the flaky login test?\n"
    "I'll track down why the login test is flaky and fix it.\n"
    "Request: is redis what we use for caching?\n"
    "I'll check whether Redis is what we use for caching.\n"
    "Request: ok do it\n"
    "I'll get started on that now."
)
RESULT_INSTRUCTION = (
    "You turn a coding agent's final report into what the agent says out loud when "
    "it finishes. Reply with one natural, first-person, past-tense sentence under "
    "twenty-five words that says what was done or found. Only say what the report "
    "says; never invent results. No file paths, code, URLs, or markdown. "
    "Reply with the sentence only.\n\n"
    "Example:\n"
    "Report: Fixed the redirect in `auth.py` so logged-out users land on /login. "
    "Added a regression test; all 48 tests pass.\n"
    "I fixed the logout redirect and added a test, and everything passes."
)


def _ollama(instruction: str, text: str) -> str:
    """One short generation from a local Ollama model. Raises on any failure."""
    payload = {
        "model": OLLAMA_MODEL,
        "system": instruction,
        "prompt": text[:MAX_LLM_INPUT_CHARS],  # the lead carries the gist
        "stream": False,
        "keep_alive": "30m",  # stay loaded between prompts
        "options": {"temperature": 0.3, "num_predict": 60},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
        return json.loads(resp.read())["response"]


def _spoken_sentence(raw: str, limit: int) -> str:
    """First sentence of model output, cleaned for speech and without end punctuation."""
    text = raw.replace("\u2019", "'").replace("`", "").replace("**", "")
    text = text.strip().strip("\"'\u201c\u201d")
    text = _clean_spoken_text(text)
    return summarize_prompt(text, limit).rstrip(".!?")


def _spoken_intent(raw: str, limit: int) -> str:
    """Model output as a start announcement, or "" if it isn't one (e.g. chatty preamble)."""
    sentence = _spoken_sentence(raw, limit)
    return sentence if sentence.lower().startswith(("i'll ", "i will ", "i'm ")) else ""


def _summarize_with_ollama(prompt: str, limit: int) -> str:
    try:
        return _spoken_intent(_ollama(START_INSTRUCTION, f"Request: {prompt}"), limit)
    except Exception:
        return ""


def _summarize_with_cli(prompt: str, limit: int) -> str:
    """A one-shot call to the current agent's own CLI (slower, uses its account)."""
    env = os.environ.copy()
    env["AGENT_PTT_ANNOUNCE"] = "0"
    cli = "codex" if AGENT_NAME.lower() == "codex" else "claude"
    command = [cli, "exec"] if cli == "codex" else [cli, "-p"]
    if cli == "codex":
        command.extend(["--sandbox", "read-only"])
    command.append(f"{START_INSTRUCTION}\n\nUser request: {prompt}")
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
        return ""
    if result.returncode != 0:
        return ""
    return _spoken_intent(result.stdout, limit)


def summarize_with_agent(prompt: str, limit: int = MAX_ANNOUNCE_CHARS) -> str:
    """Turn a request into spoken intent, falling back to the prompt's first sentence."""
    fallback = summarize_prompt(prompt, limit)
    if not fallback:
        return ""
    mode = "off" if os.environ.get("AGENT_PTT_SUMMARIZE", "1") == "0" else SUMMARIZER
    summary = ""
    if mode in {"auto", "ollama"}:
        summary = _summarize_with_ollama(prompt, limit)
    if not summary and mode in {"auto", "agent"}:
        summary = _summarize_with_cli(prompt, limit)
    return summary or fallback


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


def _last_assistant_text(path: str) -> str:
    """Text of the latest assistant entry in a JSONL transcript, or ""."""
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
        text = _assistant_text(message.get("content", message)).strip()
        if text:
            return text
    return ""


def summarize_transcript(path: str, limit: int = MAX_ANNOUNCE_CHARS) -> str:
    """Return the first useful sentence from the latest assistant transcript entry."""
    text = _clean_spoken_text(" ".join(_last_assistant_text(path).split()).lstrip("#-* "))
    if not text:
        return ""
    sentence = text.split(". ", 1)[0].strip().rstrip(".!?")
    return summarize_prompt(sentence, limit)


def summarize_result(path: str, limit: int = MAX_ANNOUNCE_CHARS) -> str:
    """Spoken summary of the agent's final report via Ollama, or "" when unavailable."""
    if os.environ.get("AGENT_PTT_SUMMARIZE", "1") == "0" or SUMMARIZER not in {"auto", "ollama"}:
        return ""
    report = _last_assistant_text(path)
    if not report:
        return ""
    try:
        return _spoken_sentence(_ollama(RESULT_INSTRUCTION, f"Report: {report}"), limit)
    except Exception:
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
        # Stale key or deleted channel — find (or recreate) the channel and rejoin once
        channel_id = _find_or_create_channel()
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
    if event_name not in {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}:
        return
    if event_name == "Stop" and event.get("stop_hook_active"):
        return

    # Summaries can take seconds; never make the host wait for them
    _detach()

    if event_name == "UserPromptSubmit":
        summary = _normalize_start_summary(summarize_with_agent(event.get("prompt", "")))
        if not summary:
            return
        if summary.lower().startswith(("i'm ", "i'll ", "i will ")):
            text = f"{summary}."
        else:
            text = f"I'm going to {summary}."
    elif event_name in {"PreToolUse", "PostToolUse"}:
        text = _collect_progress(event)
        if not text:
            return
    else:  # Stop
        progress = ""
        state = _load_state()
        key = _progress_key(event)
        pending = state.pop(key, {}).get("facts", [])
        if pending:
            progress = ". ".join(pending[:MAX_PROGRESS_FACTS]).rstrip(".") + ". "
            _save_state(state)
        transcript = event.get("transcript_path", "")
        spoken = summarize_result(transcript)
        if spoken:
            completion = _normalize_completion_summary(spoken)
        else:
            summary = _normalize_completion_summary(summarize_transcript(transcript))
            completion = (
                f"I looked into it. {summary}" if summary else "I've finished working on it."
            )
        text = progress + completion

    project = Path(event.get("cwd") or ".").name or "somewhere"
    handle = f"{AGENT_NAME} · {project}"
    announce(event.get("session_id", "unknown"), handle, text)


if __name__ == "__main__":
    # Never break the coding session — announcements are best-effort
    with contextlib.suppress(Exception):
        main()
    sys.exit(0)
