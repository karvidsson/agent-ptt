#!/usr/bin/env python3
"""Opt-in Claude Code statusLine wrapper: saves the subscription windows, then shows
the status line the user had before.

Claude Code gives a configured ``statusLine`` command a JSON object on stdin, after
every assistant message. Since 2.1.x it carries ``rate_limits`` (probed with 2.1.316:
``{"seven_day": {"used_percentage": 15, "resets_at": 1795557600}}``, plus
``five_hour`` when that window applies) and ``cost`` (``total_cost_usd`` for the
session). This script:

1. Writes them to ``~/.agent-ptt/cc-ratelimits.json`` (the latest ``rate_limits`` for
   the account, plus each recent session's ``cost``). usage.py reports the windows
   with the session's token counts while the file is under 30 minutes old.
2. Runs the user's previous statusLine command (saved by
   ``scripts/install_plugins.py --statusline`` in ``~/.agent-ptt/statusline-previous.json``)
   with the same stdin and prints what it prints, or prints a minimal line
   (model · folder · 5h/7d percentages) when there was none.

Nothing here reads message text or calls a model. Stdlib only; never fails loudly.
Install with ``python3 scripts/install_plugins.py --statusline``; undo with
``--statusline-restore``.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STATE_DIR = Path.home() / ".agent-ptt"
LIMITS_FILE = STATE_DIR / "cc-ratelimits.json"
PREVIOUS_FILE = STATE_DIR / "statusline-previous.json"
MAX_SESSIONS = 50
PREVIOUS_TIMEOUT = 5.0
WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d"}


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as out:
        json.dump(data, out)
    os.replace(out.name, path)


def _windows(rate_limits) -> dict:
    """Only the numbers: {window: {used_percentage, resets_at}}."""
    out = {}
    if not isinstance(rate_limits, dict):
        return out
    for name, window in rate_limits.items():
        if not isinstance(window, dict):
            continue
        used = window.get("used_percentage")
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            out[str(name)[:40]] = {
                "used_percentage": used,
                "resets_at": window.get("resets_at"),
            }
    return out


def save(data: dict, now: float | None = None) -> dict:
    """Merge this statusline input into the limits file; return what was written."""
    now = time.time() if now is None else now
    saved = _read_json(LIMITS_FILE)
    windows = _windows(data.get("rate_limits"))
    if windows:  # the first call of a session has none yet: keep the last known
        saved["rate_limits"] = windows
        saved["rate_limits_at"] = now
    cost = data.get("cost")
    session = str(data.get("session_id") or "")[:100]
    if session and isinstance(cost, dict):
        total = cost.get("total_cost_usd")
        sessions = saved.get("sessions") if isinstance(saved.get("sessions"), dict) else {}
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            sessions[session] = {"total_cost_usd": total, "at": now}
        recent = sorted(sessions.items(), key=lambda kv: kv[1].get("at", 0), reverse=True)
        saved["sessions"] = dict(recent[:MAX_SESSIONS])
    saved["updated_at"] = now
    _write_json(LIMITS_FILE, saved)
    return saved


def previous_command() -> str:
    previous = _read_json(PREVIOUS_FILE).get("statusLine")
    if isinstance(previous, dict) and previous.get("type", "command") == "command":
        command = previous.get("command")
        if isinstance(command, str) and command.strip():
            return command
    return ""


def minimal_line(data: dict, saved: dict) -> str:
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    parts = [str(model.get("display_name") or model.get("id") or "Claude")]
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    folder = workspace.get("current_dir") or data.get("cwd") or ""
    if folder:
        parts.append(Path(str(folder)).name)
    for name, window in (saved.get("rate_limits") or {}).items():
        used = window.get("used_percentage")
        if isinstance(used, (int, float)):
            parts.append(f"{WINDOW_LABELS.get(name, name)} {used:.0f}%")
    return " · ".join(parts)


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    saved: dict = {}
    with contextlib.suppress(Exception):
        saved = save(data)
    command = previous_command()
    if command:
        try:
            done = subprocess.run(
                command,
                shell=True,
                input=raw,
                capture_output=True,
                timeout=PREVIOUS_TIMEOUT,
                check=False,
            )
            sys.stdout.buffer.write(done.stdout)
            sys.stdout.flush()
            return 0
        except (OSError, subprocess.SubprocessError):
            pass
    print(minimal_line(data, saved))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        code = 0
    sys.exit(code)
