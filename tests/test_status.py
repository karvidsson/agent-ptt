"""The status hook (plugins/announcer/hooks/status.py): records what a session
is doing and answers "status?" questions in the channel in the agent's voice."""

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
STATUS_PY = REPO_ROOT / "plugins" / "announcer" / "hooks" / "status.py"

spec = importlib.util.spec_from_file_location("status_hook", STATUS_PY)
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(status, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(status, "STATE_FILE", tmp_path / "announcer-state.json")
    monkeypatch.setattr(status, "AGENT_NAME", "Claude")
    (tmp_path / "announcer-state.json").write_text(
        json.dumps({"identity-v2:http://x:Claude:s1": {"channel_id": "c1", "key_id": "k1"}})
    )
    monkeypatch.setenv("AGENT_PTT_STATUS", "0")


def _transcript(tmp_path, text):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    )
    return str(path)


def test_record_follows_a_turn(tmp_path):
    t0 = 1_000.0
    status.record(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "fix the flaky login test\nplease",
            "session_id": "s1",
            "cwd": "/p/app",
        },
        now=t0,
    )
    s = status.record(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest", "description": "Run the tests"},
            "transcript_path": _transcript(tmp_path, "The cause is a shared fixture."),
            "session_id": "s1",
        },
        now=t0 + 90,
    )
    assert s["task"] == "fix the flaky login test"
    assert s["state"] == "working"
    assert s["current"] == "run the tests"
    assert s["narration"] == "The cause is a shared fixture"
    assert s["tool_calls"] == 1
    assert (s["channel_id"], s["key_id"]) == ("c1", "k1")
    assert s["project"] == "app"

    s = status.record(
        {
            "hook_event_name": "Stop",
            "transcript_path": _transcript(tmp_path, "Fixed it; 12 tests pass."),
            "session_id": "s1",
        },
        now=t0 + 200,
    )
    assert s["state"] == "idle"
    assert s["result"] == "Fixed it; 12 tests pass"
    assert s["current"] == ""


def test_system_notifications_do_not_restart_the_task():
    status.record(
        {"hook_event_name": "UserPromptSubmit", "prompt": "real task", "session_id": "s1"}, now=1.0
    )
    s = status.record(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "<system-reminder>\n[SYSTEM NOTIFICATION - NOT USER INPUT]",
            "session_id": "s1",
        },
        now=2.0,
    )
    assert s["task"] == "real task"
    assert s["task_started"] == 1.0


def test_status_reply_wording():
    working = {
        "task": "fix login",
        "task_started": 0.0,
        "state": "working",
        "narration": "Found the cause",
        "current": "run the tests",
    }
    assert status.status_reply(working, now=12 * 60 + 5) == (
        "I've been on 'fix login' for 12 minutes. I just said: Found the cause. "
        "Right now I'm about to run the tests."
    )
    assert status.status_reply(
        {"task": "fix login", "task_started": 0.0, "state": "working"}, now=30
    ) == ("I've been on 'fix login' for under a minute.")
    idle = {
        "task": "fix login",
        "state": "idle",
        "finished_at": 0.0,
        "result": "Fixed it, tests pass",
    }
    assert (
        status.status_reply(idle, now=60)
        == "I finished 'fix login' a minute ago. Fixed it, tests pass."
    )


def test_status_reply_trims_long_tasks():
    task = (
        "monitor the channel and come up with better ways to make the plugins clearer "
        "on what agents do"
    )
    reply = status.status_reply({"task": task, "task_started": 0.0, "state": "working"}, now=0)
    assert reply.startswith(
        "I've been on 'monitor the channel and come up with better ways to make the plugins"
    )
    assert "…'" in reply


def _seed(session_id, key_id, **fields):
    data = {
        "session_id": session_id,
        "channel_id": "c1",
        "key_id": key_id,
        "state": "working",
        "task": f"task {session_id}",
        "task_started": 0.0,
        "updated_at": 0.0,
    }
    data.update(fields)
    status.STATUS_DIR.mkdir(parents=True, exist_ok=True)
    (status.STATUS_DIR / f"{session_id}.json").write_text(json.dumps(data))


def test_answer_question_addresses_the_mentioned_agent(monkeypatch):
    _seed("s1", "k1")
    _seed("s2", "k2", task="task s2")
    participants = {"k1": {"handle": "Hazel · agent-ptt"}, "k2": {"handle": "Otto · agent-ptt"}}
    monkeypatch.setattr(status, "_request", lambda *a, **k: {"participants": participants})

    replies = status.answer_question(
        "c1", {"text": "So what is the status @Hazel?", "sender_key": "user"}, now=120
    )
    assert [r["key_id"] for r in replies] == ["k1"]
    assert replies[0]["text"].startswith("I've been on 'task s1' for 2 minutes.")

    everyone = status.answer_question(
        "c1", {"text": "what are you working on?", "sender_key": "user"}, now=120
    )
    assert sorted(r["key_id"] for r in everyone) == ["k1", "k2"]


def test_answer_question_ignores_agents_and_small_talk(monkeypatch):
    _seed("s1", "k1")
    monkeypatch.setattr(status, "_request", lambda *a, **k: {"participants": {}})
    # Another announcer session (no status file) narrating about "status" is not a question
    status.STATE_FILE.write_text(
        json.dumps({"identity-v2:x:Claude:s9": {"channel_id": "c1", "key_id": "k9"}})
    )
    narration = "I'll look into how each agent's status could be tracked and surfaced."
    assert status.answer_question("c1", {"text": narration, "sender_key": "k9"}) == []
    assert status.answer_question("c1", {"text": narration, "sender_key": "user"}) == []
    long = "The status hook already tracks per-session state, " * 5
    assert status.answer_question("c1", {"text": long, "sender_key": "user"}) == []
    assert status.answer_question("c1", {"text": "status", "sender_key": "user"}) != []
    assert (
        status.answer_question(
            "c1", {"text": "Record slice status in the plan", "sender_key": "k1"}
        )
        == []
    )
    assert status.answer_question("c1", {"text": "cool", "sender_key": "user"}) == []
    assert status.answer_question("c9", {"text": "status?", "sender_key": "user"}) == []


def test_ensure_responder_reuses_a_live_process(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PTT_STATUS")
    started = []
    monkeypatch.setattr(
        status.subprocess,
        "Popen",
        lambda *a, **k: started.append(a) or type("P", (), {"pid": 4242})(),
    )
    status.ensure_responder("c1")
    assert len(started) == 1
    assert (status.STATUS_DIR / "responder-c1.pid").read_text() == "4242"
    monkeypatch.setattr(status.os, "kill", lambda pid, sig: None)  # pretend 4242 is alive
    status.ensure_responder("c1")
    assert len(started) == 1


def test_script_exits_zero_on_garbage_and_when_server_down(tmp_path):
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "AGENT_PTT_URL": "http://127.0.0.1:1",
        "AGENT_PTT_STATUS": "0",
    }
    for stdin in ["not json", json.dumps({"hook_event_name": "Stop", "session_id": "s1"})]:
        result = subprocess.run(
            [sys.executable, str(STATUS_PY)],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        assert result.returncode == 0
        assert result.stdout == "" and result.stderr == ""


def test_codex_copy_is_identical():
    assert (
        REPO_ROOT / "plugins" / "codex-announcer" / "status.py"
    ).read_text() == STATUS_PY.read_text()


def test_hooks_run_status_next_to_announce():
    for path in [
        REPO_ROOT / "plugins" / "announcer" / "hooks" / "hooks.json",
        REPO_ROOT / "plugins" / "codex-announcer" / "hooks.json.template",
    ]:
        config = json.loads(path.read_text())
        for event in ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]:
            commands = [h["command"] for entry in config["hooks"][event] for h in entry["hooks"]]
            assert any("status.py" in c for c in commands), (path, event)
            assert any("announce.py" in c for c in commands), (path, event)


def test_requests_send_bearer_key_when_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PTT_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert "Authorization" not in status._headers()
    (tmp_path / ".agent-ptt").mkdir()
    (tmp_path / ".agent-ptt" / "announcer.env").write_text("AGENT_PTT_API_KEY='from-file'\n")
    assert status._headers()["Authorization"] == "Bearer from-file"
    monkeypatch.setenv("AGENT_PTT_API_KEY", "from-env")
    assert status._headers()["Authorization"] == "Bearer from-env"


def test_status_reply_admits_silence_when_stale():
    stale = {
        "task": "fix login",
        "task_started": 0.0,
        "updated_at": 0.0,
        "state": "working",
        "current": "run the tests",
    }
    assert status.status_reply(stale, now=25 * 60) == (
        "I haven't reported anything for 25 minutes. Last I was on 'fix login'. "
        "I was about to run the tests."
    )


def test_subagent_events_do_not_touch_status(monkeypatch, tmp_path):
    status.record(
        {"hook_event_name": "UserPromptSubmit", "prompt": "real task", "session_id": "s1"}, now=1.0
    )
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"description": "Worker step"},
        "transcript_path": str(tmp_path / "s1" / "subagents" / "agent-abc.jsonl"),
        "session_id": "s1",
    }
    monkeypatch.setattr(status.sys, "stdin", io.StringIO(json.dumps(event)))
    status.main()
    assert status._read_json(status.status_path("s1")).get("current", "") == ""


def test_ping_gets_a_short_presence_reply(monkeypatch):
    _seed("s1", "k1", task="fix login", updated_at=100.0)
    monkeypatch.setattr(
        status, "_request", lambda *a, **k: {"participants": {"k1": {"handle": "Ezra"}}}
    )
    replies = status.answer_question("c1", {"text": "@Ezra Marco?", "sender_key": "user"}, now=120)
    assert [r["text"] for r in replies] == ["Polo. I'm here, on 'fix login'."]
