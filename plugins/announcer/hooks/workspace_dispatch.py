#!/usr/bin/env python3
"""Route installed hooks explicitly; leave existing local hook implementations intact."""

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    script = Path(sys.argv[1]).resolve()
    if script.parent != Path(__file__).resolve().parent or script.name not in {
        "announce.py",
        "status.py",
    }:
        raise ValueError("Unsupported hook target")
    event = json.load(sys.stdin)
    if event.get("agent_id"):
        return
    workspace = load("workspace_client")
    workspace.routing.bind_event(event)
    if not workspace.enabled():
        subprocess.run(
            [sys.executable, str(script)], input=json.dumps(event), text=True, check=False
        )
        return
    # No legacy participation keys, status responders, or implicit local fallback
    # are used when a session explicitly selects hosted mode.
    if script.name == "status.py":
        workspace.report_presence(event)
        return
    if os.environ.get("AGENT_PTT_ANNOUNCE", "1") == "0":
        return
    if event.get("stop_hook_active"):
        return
    if event.get("hook_event_name") not in {"UserPromptSubmit", "Stop"}:
        return
    if workspace.polling_turn(event):
        return  # Replies use explicit say; polling bookkeeping never becomes chat.
    helper = load("announce")
    name = event.get("hook_event_name")
    if name == "UserPromptSubmit":
        prompt = event.get("prompt", "")
        if helper._is_system_notification(prompt):
            return
        summary = helper.summarize_prompt(prompt)
        text = f"Starting: {summary}" if summary else ""
    elif name == "Stop":
        text = helper.summarize_transcript(event.get("transcript_path", ""))
    else:
        return
    if text:
        workspace.send(text)


if __name__ == "__main__":
    with contextlib.suppress(Exception):
        main()
