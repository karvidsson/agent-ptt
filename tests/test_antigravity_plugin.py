"""The Antigravity plugin: agy_hook.py translating Antigravity hook events for the
shared announcer scripts, and the plugin's packaging."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/antigravity-announcer"
HOOKS = ROOT / "plugins/announcer/hooks"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agy = load(PLUGIN / "agy_hook.py", "plugin_agy_hook")

CONV = "11111111-2222-3333-4444-555555555555"


def raw(event: str, **extra) -> dict:
    return {
        "conversationId": CONV,
        "workspacePaths": ["/work/proj"],
        "cwd": "/work/proj",
        "transcriptPath": "/x/transcript_full.jsonl",
        "lastUserInput": "Review the docs",
        "hookEventName": event,
        **extra,
    }


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """Record the shared scripts the adapter runs; answer with canned outputs."""
    monkeypatch.setattr(agy, "STATE_DIR", tmp_path / "agy")
    monkeypatch.setenv("AGENT_PTT_AGY_SETTINGS", str(tmp_path / "agy-settings.json"))
    monkeypatch.setenv("AGENT_PTT_AGY_GRANTS", "0")  # only the grants test writes them
    seen: list[tuple[str, dict]] = []
    answers: dict[str, list[dict]] = {}

    def run(script, event, *args, timeout=300):
        target = Path(args[0]).name if script == "workspace_dispatch.py" else script
        seen.append((target, event))
        return answers.get(target, [])

    monkeypatch.setattr(agy, "run", run)
    return {"seen": seen, "answers": answers}


# --- packaging -------------------------------------------------------------------


def test_shared_scripts_are_identical_copies():
    for script in HOOKS.glob("*.py"):
        assert (PLUGIN / script.name).read_bytes() == script.read_bytes(), script.name


def test_manifest_and_hooks_are_valid_and_versioned_with_the_other_plugins():
    manifest = json.loads((PLUGIN / "plugin.json").read_text())
    claude = json.loads((ROOT / "plugins/announcer/.claude-plugin/plugin.json").read_text())
    assert manifest["name"] == "agent-ptt-announcer"
    assert manifest["version"] == claude["version"]
    hooks = json.loads((PLUGIN / "hooks.json").read_text())["agent-ptt"]
    assert set(hooks) == {"SessionStart", "PreInvocation", "PreToolUse", "Stop"}
    assert hooks["PreToolUse"][0]["matcher"] == "run_command"
    # Antigravity runs hooks from the plugin folder: relative paths, no ${PLUGIN_ROOT}
    commands = [h["command"] for h in hooks["SessionStart"] + hooks["PreInvocation"]]
    commands += [h["command"] for h in hooks["Stop"] + hooks["PreToolUse"][0]["hooks"]]
    assert all(c.startswith("python3 ./agy_hook.py ") for c in commands)
    assert hooks["Stop"][0]["timeout"] >= 30


# --- event translation -----------------------------------------------------------


def test_session_start_injects_the_shared_context_as_a_system_message(calls):
    context = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "hi"}}
    calls["answers"]["session_start.py"] = [context]
    out = agy.session_start(raw("SessionStart"))
    assert out == {"injectSteps": [{"systemMessage": {"systemMessage": "hi"}}]}
    script, event = calls["seen"][0]
    assert script == "session_start.py"
    assert event["hook_event_name"] == "SessionStart"
    assert event["session_id"] == CONV and event["cwd"] == "/work/proj"


def test_session_start_writes_agy_grants_once_and_says_to_restart(calls, tmp_path, monkeypatch):
    # Task 23: agy asked before ptt and MCP calls on the Mac; grants skip that,
    # and agy reads them only at start.
    monkeypatch.setenv("AGENT_PTT_AGY_GRANTS", "1")
    settings = tmp_path / "agy-settings.json"
    settings.write_text(json.dumps({"trustedWorkspaces": ["/w"]}))
    context = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "hi"}}
    calls["answers"]["session_start.py"] = [context]
    first = agy.session_start(raw("SessionStart"))["injectSteps"][0]["systemMessage"]
    assert "restart agy" in first["systemMessage"]
    allow = json.loads(settings.read_text())["permissions"]["allow"]
    assert f"command({sys.executable} {PLUGIN / 'ptt.py'})" in allow
    for tool in ("say", "tell", "who", "inbox", "ack", "done"):
        assert f"mcp(agent-ptt-announcer_agent-ptt/{tool})" in allow
    assert json.loads(settings.read_text())["trustedWorkspaces"] == ["/w"]
    again = agy.session_start(raw("SessionStart"))
    assert again == {"injectSteps": [{"systemMessage": {"systemMessage": "hi"}}]}  # no repeat
    monkeypatch.setenv("AGENT_PTT_AGY_GRANTS", "0")
    settings.unlink()
    agy.session_start(raw("SessionStart"))
    assert not settings.exists()


def test_first_model_call_of_a_prompt_plays_user_prompt_submit_then_post_tool_use(calls):
    agy.pre_invocation(raw("PreInvocation", invocationNum=0, initialNumSteps=1))
    first = [(s, e["hook_event_name"]) for s, e in calls["seen"]]
    assert first == [
        ("doing.py", "UserPromptSubmit"),
        ("announce.py", "UserPromptSubmit"),
        ("status.py", "UserPromptSubmit"),
        ("receive.py", "UserPromptSubmit"),
    ]
    assert calls["seen"][0][1]["prompt"] == "Review the docs"
    calls["seen"].clear()
    agy.pre_invocation(raw("PreInvocation", invocationNum=1, initialNumSteps=1))
    later = [(s, e["hook_event_name"]) for s, e in calls["seen"]]
    assert later == [("status.py", "PostToolUse"), ("receive.py", "PostToolUse")]


def test_pre_invocation_delivers_mentions_as_a_system_message(calls):
    block = {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "@Rex: hi"}
    }
    calls["answers"]["receive.py"] = [block]
    out = agy.pre_invocation(raw("PreInvocation", invocationNum=0, initialNumSteps=1))
    assert out["injectSteps"][0]["systemMessage"]["systemMessage"] == "@Rex: hi"


def test_stop_block_becomes_continue_and_the_next_stop_is_marked_active(calls):
    calls["answers"]["receive.py"] = [{"decision": "block", "reason": "@Rex: which port?"}]
    out = agy.stop(raw("Stop", terminationReason="NO_TOOL_CALL", fullyIdle=True))
    assert out == {"decision": "continue", "reason": "@Rex: which port?"}
    assert calls["seen"][-1][1]["stop_hook_active"] is False
    calls["answers"]["receive.py"] = []
    assert agy.stop(raw("Stop", terminationReason="NO_TOOL_CALL", fullyIdle=True)) == {}
    assert calls["seen"][-1][1]["stop_hook_active"] is True  # loop guard for receive.py
    # a new execution (new initialNumSteps) starts the guard over
    agy.pre_invocation(raw("PreInvocation", invocationNum=0, initialNumSteps=9))
    agy.stop(raw("Stop", terminationReason="NO_TOOL_CALL", fullyIdle=True))
    assert calls["seen"][-1][1]["stop_hook_active"] is False


def test_a_stop_the_user_cancelled_never_waits_or_continues(calls):
    calls["answers"]["receive.py"] = [{"decision": "block", "reason": "x"}]
    assert agy.stop(raw("Stop", terminationReason="USER_CANCELED")) == {}
    assert calls["seen"] == []


def _main(args: list[str], payload: dict, env: dict | None = None) -> dict:
    import os

    done = subprocess.run(
        [sys.executable, str(PLUGIN / "agy_hook.py"), *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "AGENT_PTT_URL": "http://127.0.0.1:9", **(env or {})},
        check=False,
    )
    assert done.returncode == 0
    return json.loads(done.stdout)


def test_subagent_conversations_get_no_identity(calls, monkeypatch):
    sub = raw("SessionStart", lastUserInput="")
    monkeypatch.setattr(sys, "argv", ["agy_hook.py", "SessionStart"])
    assert not agy.is_main(sub)
    assert agy.is_main(raw("SessionStart"))
    # once a conversation had user input it stays main (Stop events after a wake)
    assert agy.is_main(raw("Stop", lastUserInput=""))


def test_pre_tool_use_always_answers_even_when_the_hook_fails(tmp_path):
    # Antigravity denies a tool call whose PreToolUse hook prints nothing useful.
    home = {"HOME": str(tmp_path)}
    assert _main(["PreToolUse"], {"toolCall": {}}, home) == {"decision": "allow"}
    out = _main(["PreToolUse"], raw("PreToolUse", toolCall={"args": {"CommandLine": "ls"}}), home)
    assert out == {"decision": "allow"}
    assert _main(["Stop"], {"broken": True}, home) == {}


# --- ptt auto-approval ------------------------------------------------------------


def test_only_a_plain_call_of_this_plugins_ptt_is_auto_approved():
    ptt = str(PLUGIN / "ptt.py")
    ok = [
        f'/usr/bin/python3 {ptt} --session abc say "@Rex found it (a.py:12)"',
        f"python3 {ptt} --session abc who",
        f"python3 '{ptt}' --session abc wait --first",
        f'python3 {ptt} --session abc say "@Rex summary:\n- a.md: fine\n- b.md: typo"',
        f"python3 {ptt} --session abc say 'costs $5 (`x`); see a|b'",
    ]
    bad = [
        f"python3 {ptt} say hi; rm -rf ~",
        f'python3 {ptt} say "$(cat ~/.ssh/id_rsa)"',
        f"python3 {ptt} say `id`",
        f"python3 {ptt} say hi | sh",
        f"python3 {ptt} say hi > /etc/x",
        f"python3 {ptt} say hi && curl x",
        "python3 /tmp/ptt.py say hi",
        f"bash {ptt} say hi",
        f"python3 {ptt}",
        f"python3 {ptt} say 'unterminated",
        f"python3 {ptt} say hi\nrm x",
        f'python3 {ptt} say "a\\"; rm x"',
        f"python3 {ptt} --file ~/.ssh/id_rsa say",
        f"python3 {ptt} say hi # comment",
    ]
    assert all(agy.ptt_command_ok(c) for c in ok)
    assert not any(agy.ptt_command_ok(c) for c in bad)
    approved = agy.pre_tool_use(raw("PreToolUse", toolCall={"args": {"CommandLine": ok[0]}}))
    assert approved["decision"] == "auto_approve"
    other = agy.pre_tool_use(raw("PreToolUse", toolCall={"args": {"CommandLine": bad[0]}}))
    assert other == {"decision": "allow"}


def test_own_mcp_tools_are_auto_approved_and_others_are_not():
    def call(server, tool="who"):
        args = {"ServerName": server, "ToolName": tool, "Arguments": '{"session": "abc"}'}
        return agy.pre_tool_use(raw("PreToolUse", toolCall={"name": "call_mcp_tool", "args": args}))

    # agy names a plugin's server <plugin>_<server>; seen JSON-quoted in its transcript
    assert call("agent-ptt-announcer_agent-ptt")["decision"] == "auto_approve"
    assert call('"agent-ptt-announcer_agent-ptt"', '"say"')["decision"] == "auto_approve"
    assert call("agent-ptt") == {"decision": "allow"}  # a user-added server of that name
    assert call("evil_agent-ptt-announcer_agent-ptt") == {"decision": "allow"}
    assert call("pencil") == {"decision": "allow"}
    hooks = json.loads((PLUGIN / "hooks.json").read_text())["agent-ptt"]["PreToolUse"]
    assert {entry["matcher"] for entry in hooks} == {"run_command", "call_mcp_tool"}


def test_auto_approval_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AGENT_PTT_AGY_ALLOW", "0")
    command = f"python3 {PLUGIN / 'ptt.py'} --session abc who"
    out = agy.pre_tool_use(raw("PreToolUse", toolCall={"args": {"CommandLine": command}}))
    assert out == {"decision": "allow"}
