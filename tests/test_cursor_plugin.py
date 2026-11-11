"""The Cursor plugin: cursor_hook.py translating Cursor hook events for the shared
announcer scripts, the shared scripts' Cursor paths (guard, wake text, Stop nudge,
transcripts, cursor-rules), and the plugin's packaging."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_ptt_plugin import (
    _deliver,
    ptt,
    receive,
    session_start,
    start,
)
from tests.test_ptt_plugin import env as env

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/cursor-announcer"
HOOKS = ROOT / "plugins/announcer/hooks"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cursor = load(PLUGIN / "cursor_hook.py", "plugin_cursor_hook")
routing = load(HOOKS / "session_routing.py", "plugin_cursor_routing")
announce = load(HOOKS / "announce.py", "plugin_cursor_announce")
status = load(HOOKS / "status.py", "plugin_cursor_status")

CONV = "11111111-2222-3333-4444-555555555555"


def raw(event: str, **extra) -> dict:
    """A Cursor hook payload, as cursor-agent 2026.11.11 sends it."""
    return {
        "conversation_id": CONV,
        "generation_id": "g1",
        "model": "default",
        "session_id": CONV,
        "hook_event_name": event,
        "cursor_version": "2026.11.11-043ef28",
        "workspace_roots": ["/work/proj"],
        "user_email": "x@example.com",
        "transcript_path": None,
        **extra,
    }


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """Record the shared scripts the adapter runs; answer with canned outputs."""
    monkeypatch.setattr(cursor, "STATE_DIR", tmp_path / "cursor")
    seen: list[tuple[str, dict]] = []
    answers: dict[str, list[dict]] = {}

    def run(script, event, *args, timeout=300):
        target = Path(args[0]).name if script == "workspace_dispatch.py" else script
        seen.append((target, event))
        return answers.get(target, [])

    monkeypatch.setattr(cursor, "run", run)
    return {"seen": seen, "answers": answers}


def names(calls) -> list[tuple[str, str]]:
    return [(script, event["hook_event_name"]) for script, event in calls["seen"]]


# --- packaging -------------------------------------------------------------------


def test_shared_scripts_are_identical_copies():
    for script in HOOKS.glob("*.py"):
        assert (PLUGIN / script.name).read_bytes() == script.read_bytes(), script.name


def test_manifest_hooks_mcp_and_rule_are_valid():
    manifest = json.loads((PLUGIN / ".cursor-plugin/plugin.json").read_text())
    claude = json.loads((ROOT / "plugins/announcer/.claude-plugin/plugin.json").read_text())
    assert manifest["name"] == "agent-ptt-announcer"
    assert manifest["version"] == claude["version"]
    # Cursor names the plugin's MCP server after displayName: the allow rules depend on it
    assert f"plugin-{manifest['displayName']}-agent-ptt" == cursor.PLUGIN_MCP_SERVER
    assert ptt.CURSOR_MCP_SERVER == cursor.PLUGIN_MCP_SERVER
    config = json.loads((PLUGIN / "hooks/hooks.json").read_text())
    assert config["version"] == 1
    hooks = config["hooks"]
    assert set(hooks) == {
        "sessionStart",
        "beforeSubmitPrompt",
        "postToolUse",
        "stop",
        "sessionEnd",
        "beforeShellExecution",
        "beforeMCPExecution",
    }
    for event, entries in hooks.items():
        # stop runs in the workspace root, so every command names the plugin root
        assert entries[0]["command"] == f'python3 "${{CURSOR_PLUGIN_ROOT}}/cursor_hook.py" {event}'
    assert hooks["stop"][0]["timeout"] >= 30 and hooks["stop"][0]["loop_limit"] is None
    mcp = json.loads((PLUGIN / "mcp.json").read_text())["mcpServers"]["agent-ptt"]
    assert mcp["args"] == ["${CURSOR_PLUGIN_ROOT}/ptt_mcp.py"]
    rule = (PLUGIN / "rules/agent-ptt.mdc").read_text()
    assert rule.startswith("---\n") and "alwaysApply: true" in rule.split("---\n")[1]


def test_repo_marketplace_lists_the_cursor_plugin():
    marketplace = json.loads((ROOT / ".cursor-plugin/marketplace.json").read_text())
    assert [p["source"] for p in marketplace["plugins"]] == ["./plugins/cursor-announcer"]
    assert marketplace["plugins"][0]["name"] == "agent-ptt-announcer"


# --- event translation -----------------------------------------------------------


def test_session_start_returns_the_context_as_additional_context(calls):
    context = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "hi"}}
    calls["answers"]["session_start.py"] = [context]
    assert cursor.session_start(raw("sessionStart", is_background_agent=False)) == {
        "additional_context": "hi"
    }
    script, event = calls["seen"][0]
    assert script == "session_start.py"
    assert event["hook_event_name"] == "SessionStart"
    assert event["session_id"] == CONV and event["cwd"] == "/work/proj"


def test_a_prompt_runs_doing_announce_status_and_joins_first_when_it_came_first(calls):
    # Cursor can fire beforeSubmitPrompt before sessionStart
    out = cursor.before_submit_prompt(raw("beforeSubmitPrompt", prompt="Fix the bug"))
    assert out == {"continue": True}
    assert names(calls) == [
        ("session_start.py", "SessionStart"),
        ("doing.py", "UserPromptSubmit"),
        ("announce.py", "UserPromptSubmit"),
        ("status.py", "UserPromptSubmit"),
    ]
    assert calls["seen"][1][1]["prompt"] == "Fix the bug"
    calls["seen"].clear()
    cursor.before_submit_prompt(raw("beforeSubmitPrompt", prompt="Next"))
    assert ("session_start.py", "SessionStart") not in names(calls)  # joined once


def test_post_tool_use_delivers_mentions_as_additional_context(calls):
    block = {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "@Rex: hi"}
    }
    calls["answers"]["receive.py"] = [block]
    out = cursor.post_tool_use(raw("postToolUse", tool_name="Shell", tool_input={"command": "ls"}))
    assert out == {"additional_context": "@Rex: hi"}
    assert names(calls) == [("status.py", "PostToolUse"), ("receive.py", "PostToolUse")]
    assert calls["seen"][1][1]["tool_name"] == "Shell"


def test_stop_block_becomes_a_followup_and_a_followup_turn_is_marked_active(calls):
    calls["answers"]["receive.py"] = [{"decision": "block", "reason": "@Rex: which port?"}]
    out = cursor.stop(raw("stop", status="completed", loop_count=0))
    assert out == {"followup_message": "@Rex: which port?"}
    assert names(calls) == [
        ("announce.py", "Stop"),
        ("status.py", "Stop"),
        ("receive.py", "Stop"),
    ]
    assert calls["seen"][-1][1]["stop_hook_active"] is False
    calls["answers"]["receive.py"] = []
    assert cursor.stop(raw("stop", status="completed", loop_count=1)) == {}
    assert calls["seen"][-1][1]["stop_hook_active"] is True  # receive.py's loop guard


def test_an_aborted_stop_never_waits_or_continues(calls):
    calls["answers"]["receive.py"] = [{"decision": "block", "reason": "x"}]
    assert cursor.stop(raw("stop", status="aborted", loop_count=0)) == {}
    assert calls["seen"] == []


def test_session_end_leaves(calls):
    assert cursor.session_end(raw("sessionEnd", reason="completed")) == {}
    assert names(calls) == [("announce.py", "SessionEnd")]


def test_subagent_conversations_get_no_identity(calls):
    # A subagent's tools fire postToolUse under its own conversation_id, which never
    # had a sessionStart or a prompt.
    assert not cursor.is_main("postToolUse", raw("postToolUse", conversation_id="sub-1"))
    assert not cursor.is_main("stop", raw("stop", conversation_id="sub-1"))
    cursor.session_start(raw("sessionStart"))
    assert cursor.is_main("postToolUse", raw("postToolUse"))
    assert cursor.is_main("stop", raw("stop"))


def _main(args: list[str], payload: dict, env_vars: dict | None = None) -> dict:
    done = subprocess.run(
        [sys.executable, str(PLUGIN / "cursor_hook.py"), *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "AGENT_PTT_URL": "http://127.0.0.1:9", **(env_vars or {})},
        check=False,
    )
    assert done.returncode == 0
    return json.loads(done.stdout)


def test_the_hook_always_prints_valid_json_and_a_subagent_does_nothing(tmp_path):
    home = {"HOME": str(tmp_path)}
    # an invalid answer to a permission hook blocks the action: always answer
    assert _main(["beforeShellExecution"], {"broken": True}, home) == {}
    assert _main(["beforeShellExecution"], raw("beforeShellExecution", command="ls"), home) == {}
    assert _main(["beforeSubmitPrompt"], {"broken": True}, home) == {"continue": True}
    sub = raw("postToolUse", conversation_id="sub-1", tool_name="Read")
    assert _main(["postToolUse"], sub, home) == {}
    assert not (tmp_path / ".agent-ptt" / "projects").exists()  # no phantom member
    assert _main(["stop"], {"broken": True}, home) == {}


# --- permissions -----------------------------------------------------------------


def test_only_a_plain_call_of_this_plugins_ptt_is_allowed():
    ptt_path = str(PLUGIN / "ptt.py")
    ok = [
        f'/usr/bin/python3 {ptt_path} --session abc say "@Rex found it (a.py:12)"',
        f"python3 {ptt_path} --session abc wait --first",
        f"python3 {ptt_path} --session abc say 'costs $5 (`x`); see a|b'",
    ]
    bad = [
        f"python3 {ptt_path} say hi; rm -rf ~",
        f'python3 {ptt_path} say "$(cat ~/.ssh/id_rsa)"',
        f"python3 {ptt_path} say hi && curl x",
        f"python3 {ptt_path} say hi | sh",
        "python3 /tmp/ptt.py say hi",
        f"python3 {HOOKS / 'ptt.py'} say hi",  # another plugin's copy
        f"bash {ptt_path} say hi",
        f"python3 {ptt_path} --file ~/.ssh/id_rsa say",
    ]
    assert all(cursor.ptt_command_ok(c) for c in ok)
    assert not any(cursor.ptt_command_ok(c) for c in bad)
    allowed = cursor.permission(raw("beforeShellExecution", command=ok[0]))
    assert allowed == {"permission": "allow"}
    # no opinion is {}: Cursor's normal approval flow (not a deny, not an ask)
    assert cursor.permission(raw("beforeShellExecution", command=bad[0])) == {}


def test_own_mcp_tools_are_allowed_and_others_are_not():
    own = f"python3 {PLUGIN / 'ptt_mcp.py'}"

    def call(server, command=own):
        return cursor.permission(
            raw(
                "beforeMCPExecution",
                tool_name="who",
                tool_input='{"session": "abc"}',
                mcp_server_name=server,
                command=command,
            )
        )

    assert call(cursor.PLUGIN_MCP_SERVER) == {"permission": "allow"}
    assert call(cursor.PLUGIN_MCP_SERVER, command="") == {"permission": "allow"}
    assert call("agent-ptt", command="python3 /elsewhere/ptt_mcp.py") == {}
    assert call("pencil", command="npx pencil") == {}
    assert call("evil", command="") == {}
    # Live: with Cursor's Claude-plugin import, the same tools come from the Claude copy.
    cache = "/Users/x/.claude/plugins/cache/agent-ptt/agent-ptt-announcer/0.5.3"
    claude = f"python3 {cache}/hooks/ptt_mcp.py"
    shared = "plugin-agent-ptt-announcer-agent-ptt"
    assert call(shared, command=claude) == {"permission": "allow"}
    assert call(shared, command="python3 /tmp/hooks/ptt_mcp.py") == {}
    assert call("plugin-evil-agent-ptt", command=claude) == {}


def test_allowing_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AGENT_PTT_CURSOR_ALLOW", "0")
    command = f"python3 {PLUGIN / 'ptt.py'} --session abc who"
    assert cursor.permission(raw("beforeShellExecution", command=command)) == {}


def test_cursor_rules_prints_a_valid_cli_config_entry(capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("CURSOR_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert ptt.main(["cursor-rules"]) == 0
    captured = capsys.readouterr()
    rules = json.loads(captured.out)
    allow = rules["permissions"]["allow"]
    assert rules["permissions"]["deny"] == []  # the CLI rejects a config without it
    assert f"Shell({sys.executable} {HOOKS / 'ptt.py'})" in allow
    assert f"Mcp({ptt.CURSOR_MCP_SERVER}:*)" in allow
    assert "Mcp(plugin-agent-ptt-announcer-agent-ptt:*)" in allow
    assert str(tmp_path / "cursor" / "cli-config.json") in captured.err
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(tmp_path / "c"))
    assert ptt.cursor_config_dir() == tmp_path / "c"


# --- Q2: Cursor's Claude Code import ----------------------------------------------


def test_scripts_run_by_cursors_claude_import_exit_quietly(monkeypatch):
    for name in ("AGENT_PTT_AGENT", "CLAUDECODE", "CURSOR_VERSION"):
        monkeypatch.delenv(name, raising=False)
    assert not routing.foreign_cursor_hook({"session_id": "s"})  # plain Claude Code
    assert routing.foreign_cursor_hook({"session_id": "s", "cursor_version": "2026.11.11"})
    monkeypatch.setenv("CURSOR_VERSION", "2026.11.11")
    assert routing.foreign_cursor_hook({"session_id": "s"})
    monkeypatch.setenv("AGENT_PTT_AGENT", "Cursor")  # called by cursor_hook.py
    assert not routing.foreign_cursor_hook({"session_id": "s"})
    monkeypatch.delenv("AGENT_PTT_AGENT")
    monkeypatch.setenv("CLAUDECODE", "1")  # a real Claude Code session
    assert not routing.foreign_cursor_hook({"session_id": "s"})


@pytest.mark.parametrize(
    "script", ["session_start.py", "receive.py", "doing.py", "status.py", "announce.py"]
)
def test_the_claude_plugin_under_cursor_does_not_join(script, tmp_path):
    payload = {
        "hook_event_name": "SessionStart" if script == "session_start.py" else "UserPromptSubmit",
        "session_id": CONV,
        "cwd": str(tmp_path),
        "prompt": "hello",
        "cursor_version": "2026.11.11",
    }
    run_env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "AGENT_PTT_AGENT")}
    run_env.update(
        HOME=str(tmp_path), CURSOR_VERSION="2026.11.11", AGENT_PTT_URL="http://127.0.0.1:9"
    )
    done = subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=run_env,
        check=False,
    )
    assert done.returncode == 0 and done.stdout == ""
    assert not (tmp_path / ".agent-ptt").exists()  # no state, no join attempt


# --- shared scripts: Cursor wording, wake and transcripts --------------------------


def test_cursor_session_start_asks_for_a_background_wait_and_names_cursor(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_AGENT", "Cursor")
    context = start(capsys=capsys)
    line = context.splitlines()[1]
    assert "wait --first` as a background shell command" in line
    assert "start the same `wait --first` command again" in line
    assert "ptt cursor-rules" in line
    assert "with the Shell tool" in context and "Monitor" not in context
    link = session_start.link
    _, record = link.load_membership(link.base_url(), "s1")
    assert record["join"]["harness"] == "cursor"


def test_cursor_stop_nudges_for_the_background_wait_and_stops_without_a_wait(
    env, capsys, monkeypatch
):
    monkeypatch.setenv("AGENT_PTT_AGENT", "Cursor")
    monkeypatch.setenv("AGENT_PTT_MONITOR_NUDGE", "1")
    start(capsys=capsys)
    _deliver("Stop")
    out = capsys.readouterr().out
    assert '"decision": "block"' in out and "wait --first` as a background command" in out
    assert receive.stop_wait_seconds("Cursor") == 0.0  # no Stop wait unless asked for
    monkeypatch.setenv("AGENT_PTT_STOP_WAIT_SECONDS", "30")
    assert receive.stop_wait_seconds("Cursor") == 30.0
    assert receive.stop_wait_seconds("Claude") == 0.0


def test_ptt_wait_in_cursors_agent_shell_ends_after_the_first_delivery(env, capsys, monkeypatch):
    start(capsys=capsys)
    seen = []

    class Waiter:
        cursor = None

        def __init__(self, session):
            self.first = None

        def run(self):
            seen.append(self.first)

    monkeypatch.setattr(ptt, "Waiter", Waiter)
    session = ptt.Session("s1")
    monkeypatch.delenv("CURSOR_AGENT", raising=False)
    ptt.cmd_wait(session, ptt.parser().parse_args(["wait"]))
    monkeypatch.setenv("CURSOR_AGENT", "1")
    ptt.cmd_wait(session, ptt.parser().parse_args(["wait"]))
    assert seen == [False, True]


CURSOR_TRANSCRIPT = [
    {"role": "user", "message": {"content": [{"type": "text", "text": "<user_query>Hi"}]}},
    {
        "role": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Looking."},
                {"type": "tool_use", "name": "Shell", "input": {"command": "ls"}},
            ]
        },
    },
    {"role": "assistant", "message": {"content": [{"type": "text", "text": "Fixed the bug."}]}},
    {"type": "turn_ended", "status": "success"},
]


def test_cursor_transcripts_are_read_for_the_turn_end_announcement(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in CURSOR_TRANSCRIPT))
    assert announce._last_assistant_text(str(path)) == "Fixed the bug."
    assert status._latest_narration(str(path)) == "Fixed the bug."


def test_ptt_finds_a_cursor_session_from_the_shell_env(env, capsys, monkeypatch):
    start("conv-9", capsys=capsys)
    monkeypatch.setenv("CURSOR_CONVERSATION_ID", "conv-9")
    assert ptt.link.env_session(ptt.link.base_url()) == "conv-9"


def test_plugin_version_reads_the_cursor_manifest(tmp_path):
    (tmp_path / ".cursor-plugin").mkdir()
    (tmp_path / ".cursor-plugin" / "plugin.json").write_text('{"version": "9.9.9"}')
    assert ptt.link.plugin_version(tmp_path) == "9.9.9"


def test_shared_hook_main_reads_stdin_once(monkeypatch):
    # receive.main parses stdin itself now (for the guard): an empty event is harmless
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    receive.main()
