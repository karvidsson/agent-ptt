#!/usr/bin/env python3
"""Route installed hooks explicitly; leave existing local hook implementations intact."""

import contextlib
import importlib.util
import json
import os
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
    workspace = load("workspace_client")
    if not workspace.enabled():
        os.execv(sys.executable, [sys.executable, str(script)])
        return
    # No legacy participation keys, status responders, or implicit local fallback
    # are used when a session explicitly selects hosted mode.
    if script.name == "status.py":
        workspace.report_presence(json.load(sys.stdin))
        return
    if os.environ.get("AGENT_PTT_ANNOUNCE", "1") == "0":
        return
    event = json.load(sys.stdin)
    if event.get("agent_id") or event.get("stop_hook_active"):
        return
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
