"""The Claude Code announcer hook (claude-plugin/hooks/announce.py).

The script is stdlib-only; we import it directly for unit tests and run
it as a subprocess for the never-break-coding guarantee.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
ANNOUNCE_PY = REPO_ROOT / "plugins" / "announcer" / "hooks" / "announce.py"

spec = importlib.util.spec_from_file_location("announce", ANNOUNCE_PY)
announce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(announce)


@pytest.fixture(autouse=True)
def _no_real_ollama_or_fork(monkeypatch, tmp_path):
    """Tests never reach a real local model and never fork the test runner."""

    def unavailable(instruction, text):
        raise urllib.error.URLError("ollama not running")

    monkeypatch.setattr(announce, "_ollama", unavailable)
    monkeypatch.delenv("AGENT_PTT_CHANNEL", raising=False)
    monkeypatch.setattr(announce.routing, "ROUTES_DIR", tmp_path / "routes")
    monkeypatch.setattr(announce, "STATE_FILE", tmp_path / "announcer-state.json")
    monkeypatch.setenv("AGENT_PTT_FORK", "0")


# ---------------------------------------------------------------------------
# summarize_prompt
# ---------------------------------------------------------------------------


def test_summarize_takes_first_meaningful_line():
    prompt = "\n\n  fix the login bug  \nand also update the docs"
    assert announce.summarize_prompt(prompt) == "fix the login bug"


def test_summarize_collapses_whitespace():
    assert announce.summarize_prompt("add   tests\tfor the   parser") == "add tests for the parser"


def test_summarize_truncates_long_prompts_at_word_boundary():
    prompt = "please refactor " + "the entire authentication and session layer " * 10
    result = announce.summarize_prompt(prompt)
    assert len(result) <= announce.MAX_ANNOUNCE_CHARS + 1
    assert result.endswith("…")
    assert not result[:-1].endswith(" ")


def test_summarize_empty_prompt():
    assert announce.summarize_prompt("") == ""
    assert announce.summarize_prompt("\n\n  \n") == ""


def test_summarize_ignores_codex_browser_tab_placeholder():
    assert announce.summarize_prompt("# Chrome tabs:") == ""
    assert announce.summarize_with_agent("# Chrome tabs:") == ""


def test_clean_spoken_text_removes_urls():
    assert (
        announce._clean_spoken_text("I built the dashboard: https://claude.ai/artifact/example.")
        == "I built the dashboard"
    )


def test_normalize_start_summary_removes_request_prefixes():
    assert announce._normalize_start_summary("can you configure this for me?") == (
        "configure this for me"
    )
    assert announce._normalize_start_summary("look at the dashboard….") == "look at the dashboard"


def test_normalize_completion_summary_repairs_fragments():
    assert announce._normalize_completion_summary("Caught up on the migration.") == (
        "I caught up on the migration."
    )
    assert announce._normalize_completion_summary("Done.") == "I finished the task."


def test_tool_subject_ignores_commands_and_generic_tools():
    assert (
        announce._tool_subject(
            {"tool_name": "Bash", "tool_input": {"command": "python3 - <<'E'\nprint('x')"}}
        )
        == ""
    )
    assert (
        announce._tool_subject(
            {"tool_name": "Artifact", "tool_input": {"url": "https://claude.ai/artifact/example"}}
        )
        == ""
    )


def test_tool_subject_speaks_intent_not_tool_names():
    assert (
        announce._tool_subject({"tool_name": "Read", "tool_input": {"file_path": "src/main.py"}})
        == "read main.py"
    )
    assert (
        announce._tool_subject({"tool_name": "Edit", "tool_input": {"file_path": "/a/b/audio.py"}})
        == "edit audio.py"
    )
    assert (
        announce._tool_subject({"tool_name": "Grep", "tool_input": {"pattern": "foo"}})
        == "look through the code"
    )
    assert (
        announce._tool_subject(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "uv run pytest", "description": "Run the test suite."},
            }
        )
        == "run the test suite"
    )
    assert (
        announce._tool_subject(
            {"tool_name": "Agent", "tool_input": {"prompt": "…", "description": "Review the diff"}}
        )
        == "hand off a subtask: review the diff"
    )


def test_summarize_transcript_reads_latest_assistant_sentence(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"content": "fix it"}}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": "I updated the API. All tests pass."}
                            ]
                        },
                    }
                ),
            ]
        )
    )
    assert announce.summarize_transcript(str(transcript)) == "I updated the API. All tests pass"


def test_summarize_transcript_missing_file_is_empty(tmp_path):
    assert announce.summarize_transcript(str(tmp_path / "missing.jsonl")) == ""


def test_summarize_with_agent_uses_claude_for_claude_agent(monkeypatch):
    class Result:
        returncode = 0
        stdout = "I'll inspect the project and identify its main entry point.\n"

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(announce.subprocess, "run", fake_run)
    monkeypatch.setattr(announce, "AGENT_NAME", "Claude")
    result = announce.summarize_with_agent("what is the main entry point?")
    assert result == "I'll inspect the project and identify its main entry point"
    assert calls[0][0] == ["claude", "-p", calls[0][0][2]]
    assert calls[0][1]["env"]["AGENT_PTT_ANNOUNCE"] == "0"
    assert "Do not address the listener as 'you'" in calls[0][0][2]


def test_summarize_with_agent_uses_codex_for_codex_agent(monkeypatch):
    class Result:
        returncode = 0
        stdout = "I'll inspect the project."

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(announce.subprocess, "run", fake_run)
    monkeypatch.setattr(announce, "AGENT_NAME", "Codex")
    assert announce.summarize_with_agent("inspect the app") == "I'll inspect the project"
    assert calls[0][0][:3] == ["codex", "exec", "--sandbox"]
    assert calls[0][0][3] == "read-only"


def test_summarize_with_agent_falls_back_on_failure(monkeypatch):
    def fake_run(*args, **kwargs):
        raise OSError("claude unavailable")

    monkeypatch.setattr(announce.subprocess, "run", fake_run)
    assert announce.summarize_with_agent("inspect the app") == "inspect the app"


def test_summarize_with_agent_prefers_local_ollama(monkeypatch):
    calls = []

    def fake_ollama(instruction, text):
        calls.append((instruction, text))
        return '"I\'m going to find out why the API tests fail and fix them."\n'

    def no_cli(*args, **kwargs):
        raise AssertionError("agent CLI must not run when Ollama answers")

    monkeypatch.setattr(announce, "_ollama", fake_ollama)
    monkeypatch.setattr(announce.subprocess, "run", no_cli)
    result = announce.summarize_with_agent("can you check why test_api fails and fix it")
    assert result == "I'm going to find out why the API tests fail and fix them"
    assert calls[0][1] == "Request: can you check why test_api fails and fix it"
    assert "Do not address the listener as 'you'" in calls[0][0]


def test_summarize_with_agent_rejects_chatty_model_output(monkeypatch):
    monkeypatch.setattr(announce, "_ollama", lambda i, t: "Sure! Here is a status update.")
    monkeypatch.setattr(announce, "SUMMARIZER", "ollama")
    assert announce.summarize_with_agent("inspect the app") == "inspect the app"


def test_summarize_with_agent_falls_back_to_cli_when_ollama_is_down(monkeypatch):
    class Result:
        returncode = 0
        stdout = "I'll inspect the app."

    monkeypatch.setattr(announce.subprocess, "run", lambda *a, **k: Result())
    assert announce.summarize_with_agent("inspect the app") == "I'll inspect the app"


def test_summarizer_ollama_mode_never_runs_the_cli(monkeypatch):
    def no_cli(*args, **kwargs):
        raise AssertionError("agent CLI must not run in ollama mode")

    monkeypatch.setattr(announce.subprocess, "run", no_cli)
    monkeypatch.setattr(announce, "SUMMARIZER", "ollama")
    assert announce.summarize_with_agent("inspect the app") == "inspect the app"


def test_summarizer_off_uses_the_prompt_text(monkeypatch):
    def no_llm(*args, **kwargs):
        raise AssertionError("no model call when summarizing is off")

    monkeypatch.setattr(announce, "_ollama", no_llm)
    monkeypatch.setattr(announce.subprocess, "run", no_llm)
    monkeypatch.setattr(announce, "SUMMARIZER", "off")
    assert announce.summarize_with_agent("can you inspect the app?") == "inspect the app"


def test_ollama_request_shape(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"response": "I'll do it."}).encode()

    def fake_urlopen(req, timeout):
        requests.append((req, timeout))
        return Response()

    monkeypatch.undo()  # drop the autouse fake to test the real _ollama
    monkeypatch.setattr(announce.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(announce, "OLLAMA_URL", "http://localhost:11434")
    monkeypatch.setattr(announce, "OLLAMA_MODEL", "gemma3:4b")
    assert announce._ollama("be brief", "x" * 10_000) == "I'll do it."
    req, timeout = requests[0]
    body = json.loads(req.data)
    assert req.full_url == "http://localhost:11434/api/generate"
    assert body["model"] == "gemma3:4b"
    assert body["system"] == "be brief"
    assert len(body["prompt"]) == announce.MAX_LLM_INPUT_CHARS
    assert body["stream"] is False
    assert timeout == announce.OLLAMA_TIMEOUT


def test_summarize_drops_request_prefixes():
    assert announce.summarize_prompt("can you fix the login redirect?") == "fix the login redirect"
    assert (
        announce.summarize_prompt("please update the docs and tests") == "update the docs and tests"
    )


def test_summarize_keeps_only_first_sentence():
    assert (
        announce.summarize_prompt("Fix the login redirect. Then update the docs.")
        == "Fix the login redirect"
    )


# ---------------------------------------------------------------------------
# State cache
# ---------------------------------------------------------------------------


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(announce, "STATE_FILE", tmp_path / "state.json")
    announce._save_state({"session:handle": {"channel_id": "c1", "key_id": "k1"}})
    assert announce._load_state() == {"session:handle": {"channel_id": "c1", "key_id": "k1"}}


def test_state_missing_or_corrupt_is_empty(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(announce, "STATE_FILE", state_file)
    assert announce._load_state() == {}
    state_file.write_text("{not json")
    assert announce._load_state() == {}


# ---------------------------------------------------------------------------
# Never break coding: subprocess exits 0 fast when the server is down
# ---------------------------------------------------------------------------


def _run_hook(event: dict, url: str) -> tuple[subprocess.CompletedProcess, float]:
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(ANNOUNCE_PY)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "PATH": "/usr/bin:/bin",
            "AGENT_PTT_URL": url,
            "AGENT_PTT_OLLAMA_URL": "http://localhost:19998",
            "HOME": "/tmp",
        },
    )
    return proc, time.monotonic() - start


def test_exits_zero_and_silent_when_server_down():
    event = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "do something",
        "cwd": "/tmp/my-project",
        "session_id": "s1",
    }
    proc, elapsed = _run_hook(event, "http://localhost:19999")
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert elapsed < 6, f"hook took {elapsed:.1f}s with server down"


# ---------------------------------------------------------------------------
# Codex integration
# ---------------------------------------------------------------------------

CODEX_ANNOUNCE = REPO_ROOT / "plugins" / "codex-announcer" / "announce.py"
CODEX_INSTALL = REPO_ROOT / "plugins" / "codex-announcer" / "install.sh"


def test_codex_script_is_identical_to_claude_script():
    """One script serves both tools — fix bugs in one place and copy."""
    assert CODEX_ANNOUNCE.read_bytes() == ANNOUNCE_PY.read_bytes()


def test_announce_creates_missing_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(announce, "STATE_FILE", tmp_path / "state.json")
    requests = []

    def fake_request(method, path, payload=None, timeout=None):
        requests.append((method, path))
        if method == "GET" and path == "/channels":
            return []
        if method == "POST" and path == "/channels":
            assert payload == {"name": "proj", "reuse_existing": True}
            return {"channel_id": "chan-1"}
        if path.endswith("/join"):
            assert payload == {"handle": "Claude · proj", "session_id": "s1", "agent": "Claude"}
            return {"key_id": "key-1", "session_id": "s1"}
        if path.endswith("/say"):
            return {"message_id": "m1"}
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(announce, "_request", fake_request)

    announce.announce("s1", "Claude · proj", "hello", "proj")

    assert requests == [
        ("GET", "/channels"),
        ("POST", "/channels"),
        ("POST", "/channels/chan-1/join"),
        ("POST", "/channels/chan-1/say"),
    ]


def test_announce_recreates_channel_deleted_mid_session(monkeypatch, tmp_path):
    """Channel cleared after the key was cached: the 404 retry recreates it."""
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(announce, "STATE_FILE", state_file)
    state_file.write_text(
        json.dumps(
            {
                f"identity-v2:{announce.BASE_URL}:Claude:s1": {
                    "channel_id": "chan-1",
                    "key_id": "old-key",
                }
            }
        )
    )
    live = {"channels": [{"name": "proj", "channel_id": "chan-1"}]}
    requests = []

    def fake_request(method, path, payload=None, timeout=None):
        requests.append((method, path))
        if method == "GET" and path == "/channels":
            return live["channels"]
        if method == "POST" and path == "/channels":
            live["channels"] = [{"name": payload["name"], "channel_id": "chan-2"}]
            return live["channels"][0]
        if path == "/channels/chan-1/say":
            live["channels"] = []  # cleared between lookup and say
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        if path == "/channels/chan-2/join":
            return {"key_id": "new-key", "session_id": "s1"}
        if path == "/channels/chan-2/say":
            return {"message_id": "m1"}
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(announce, "_request", fake_request)

    announce.announce("s1", "Claude · proj", "hello", "proj")

    assert requests[-1] == ("POST", "/channels/chan-2/say")
    assert json.loads(state_file.read_text())[f"identity-v2:{announce.BASE_URL}:Claude:s1"] == {
        "channel_id": "chan-2",
        "key_id": "new-key",
    }


def test_agent_name_prefixes_the_handle(monkeypatch):
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    monkeypatch.setattr(announce, "summarize_with_agent", lambda prompt: prompt)
    monkeypatch.setattr(announce, "AGENT_NAME", "Codex")
    monkeypatch.setenv("AGENT_PTT_FORK", "0")
    monkeypatch.setattr(
        announce.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "ship it",
                    "cwd": "/tmp/my-project",
                    "session_id": "s1",
                }
            )
        ),
    )
    announce.main()
    assert calls == [("s1", "Codex · my-project", "I'm going to ship it.", "my-project")]


def test_progress_is_grouped_after_three_completed_tools(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    monkeypatch.setattr(announce, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setenv("AGENT_PTT_FORK", "0")

    for event_name, path in [
        ("PreToolUse", "src/main.py"),
        ("PostToolUse", "src/main.py"),
        ("PreToolUse", "tests/test_main.py"),
        ("PostToolUse", "tests/test_main.py"),
        ("PreToolUse", "pyproject.toml"),
        ("PostToolUse", "pyproject.toml"),
    ]:
        monkeypatch.setattr(
            announce.sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": event_name,
                        "tool_name": "Read",
                        "tool_input": {"file_path": path},
                        "cwd": "/tmp/my-project",
                        "session_id": "s1",
                    }
                )
            ),
        )
        announce.main()

    assert len(calls) == 1
    assert calls[0][2] == (
        "I'm about to read main.py, then read test_main.py, then read pyproject.toml."
    )


def test_progress_speaks_new_narration_right_away(monkeypatch, tmp_path):
    """What the agent tells the user between tool calls is spoken once, at the next tool."""
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    monkeypatch.setattr(announce, "STATE_FILE", tmp_path / "state.json")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Codex is editing the same file, so I'll wait."}
                    ]
                },
            }
        )
    )
    for event_name, tool in [
        ("PreToolUse", "Bash"),
        ("PostToolUse", "Bash"),
        ("PreToolUse", "Bash"),
        ("PostToolUse", "Bash"),
    ]:
        monkeypatch.setattr(
            announce.sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": event_name,
                        "tool_name": tool,
                        "tool_input": {"command": "git diff", "description": "Show the diff"},
                        "transcript_path": str(transcript),
                        "cwd": "/tmp/my-project",
                        "session_id": "s1",
                    }
                )
            ),
        )
        announce.main()

    assert [c[2] for c in calls] == [
        "Codex is editing the same file, so I'll wait. I'm about to show the diff."
    ]


def test_narration_skips_tool_results_and_stops_at_user_prompt(tmp_path):
    transcript = tmp_path / "t.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "fix the bug"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Old turn."}]}},
        {"type": "user", "message": {"role": "user", "content": "now the tests"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "On it: tests."}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
        },
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "…"}]}},
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in records))
    assert announce._latest_narration(str(transcript)) == "On it: tests."

    codex = tmp_path / "c.jsonl"
    codex.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "Checking the mixer."}],
                    },
                },
                {"type": "response_item", "payload": {"type": "custom_tool_call", "input": "x"}},
            ]
        )
    )
    assert announce._latest_narration(str(codex)) == "Checking the mixer."


def test_intent_sentence_stays_short():
    long = "check the plugin cache layout and refresh the installed plugins from the checkout"
    assert announce._intent_sentence(["run the tests", "edit audio.py"]) == (
        "I'm about to run the tests, then edit audio.py."
    )
    assert announce._intent_sentence([long, long, long]) == f"I'm about to {long}."


def test_stop_drops_unspoken_tool_intents(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    monkeypatch.setattr(announce, "STATE_FILE", tmp_path / "state.json")
    announce._collect_progress(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "x.py"},
            "session_id": "s1",
        }
    )
    monkeypatch.setattr(
        announce.sys,
        "stdin",
        io.StringIO(json.dumps({"hook_event_name": "Stop", "cwd": "/p", "session_id": "s1"})),
    )
    announce.main()
    assert "read x.py" not in calls[0][2]
    assert "progress:s1:Claude" not in announce._load_state()


def test_system_notifications_are_not_announced_as_prompts(monkeypatch):
    """Background task events reach UserPromptSubmit but aren't the user talking."""
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    prompt = (
        "<system-reminder>\n[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
        "<task-notification>Monitor event: new messages</task-notification>"
    )
    monkeypatch.setattr(
        announce.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "session_id": "s1"}
            )
        ),
    )
    announce.main()
    assert calls == []


def test_progress_ignores_low_information_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(announce, "STATE_FILE", tmp_path / "state.json")
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "session_id": "s1",
    }
    assert announce._collect_progress(event) == ""


def test_install_sh_writes_hooks_json(tmp_path):
    proc = subprocess.run(
        ["bash", str(CODEX_INSTALL)],
        capture_output=True,
        text=True,
        timeout=15,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    hooks_file = tmp_path / ".codex" / "hooks.json"
    config = json.loads(hooks_file.read_text())
    command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "AGENT_PTT_AGENT=Codex" in command
    assert str(CODEX_ANNOUNCE) in command
    assert "__AGENT_PTT_PLUGIN_DIR__" not in command
    assert "Stop" in config["hooks"]


def test_install_sh_refuses_to_overwrite(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "hooks.json").write_text('{"hooks": {}}')
    proc = subprocess.run(
        ["bash", str(CODEX_INSTALL)],
        capture_output=True,
        text=True,
        timeout=15,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert proc.returncode == 1
    assert json.loads((tmp_path / ".codex" / "hooks.json").read_text()) == {"hooks": {}}
    assert "AGENT_PTT_AGENT=Codex" in proc.stdout  # snippet printed for manual merge


def test_exits_zero_on_garbage_stdin():
    proc = subprocess.run(
        [sys.executable, str(ANNOUNCE_PY)],
        input="this is not json",
        capture_output=True,
        text=True,
        timeout=15,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
    )
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_disabled_via_env_exits_immediately():
    proc = subprocess.run(
        [sys.executable, str(ANNOUNCE_PY)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "x"}),
        capture_output=True,
        text=True,
        timeout=15,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "AGENT_PTT_ANNOUNCE": "0"},
    )
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_stop_hook_active_is_skipped(monkeypatch):
    """When stop_hook_active is set, no announcement is attempted."""
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    monkeypatch.setattr(
        announce.sys,
        "stdin",
        io.StringIO(json.dumps({"hook_event_name": "Stop", "stop_hook_active": True})),
    )
    announce.main()
    assert calls == []


def test_stop_uses_conversational_completion(monkeypatch):
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    monkeypatch.setattr(
        announce.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "Stop",
                    "cwd": "/tmp/my-project",
                    "session_id": "s1",
                }
            )
        ),
    )
    monkeypatch.setenv("AGENT_PTT_FORK", "0")
    announce.main()
    assert calls == [
        (
            "s1",
            "Claude · my-project",
            "The task ended, but I couldn't read its result.",
            "my-project",
        )
    ]


def test_stop_announces_transcript_summary(monkeypatch, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "I fixed the login flow."}]},
            }
        )
    )
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    monkeypatch.setenv("AGENT_PTT_FORK", "0")
    monkeypatch.setattr(
        announce.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "Stop",
                    "cwd": "/tmp/my-project",
                    "session_id": "s1",
                    "transcript_path": str(transcript),
                }
            )
        ),
    )
    announce.main()
    assert calls == [("s1", "Claude · my-project", "I fixed the login flow.", "my-project")]


def test_stop_speaks_ollama_summary_of_the_report(monkeypatch, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    report = "## Summary\n\n- Fixed `announce.py` so the hook forks first.\n- 64 tests pass."
    transcript.write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": report}]}}
        )
    )
    seen = []

    def fake_ollama(instruction, text):
        seen.append(text)
        return "I made the `hook` fork first, and **all** the tests pass."

    calls = []
    monkeypatch.setattr(announce, "_ollama", fake_ollama)
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    monkeypatch.setattr(
        announce.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "Stop",
                    "cwd": "/tmp/my-project",
                    "session_id": "s1",
                    "transcript_path": str(transcript),
                }
            )
        ),
    )
    announce.main()
    assert seen == [f"Report: {report}"]
    assert calls == [
        (
            "s1",
            "Claude · my-project",
            "I made the hook fork first, and all the tests pass.",
            "my-project",
        )
    ]


def test_ignores_unknown_events(monkeypatch):
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    monkeypatch.setattr(
        announce.sys,
        "stdin",
        io.StringIO(json.dumps({"hook_event_name": "PreToolUse"})),
    )
    announce.main()
    assert calls == []


def test_project_name_groups_subfolders_and_worktrees(tmp_path):
    repo = tmp_path / "sales-portal"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "Initial",
        ],
        check=True,
        capture_output=True,
    )
    nested = repo / "src" / "app"
    nested.mkdir(parents=True)
    worktree = tmp_path / "worker-123"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "worker", str(worktree)],
        check=True,
        capture_output=True,
    )
    assert announce.project_name(str(repo)) == "sales-portal"
    assert announce.project_name(str(nested)) == "sales-portal"
    assert announce.project_name(str(worktree)) == "sales-portal"


def test_project_name_falls_back_when_git_unavailable(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(announce.subprocess, "run", unavailable)
    assert announce.project_name(str(tmp_path / "notes")) == "notes"
    monkeypatch.chdir(tmp_path)
    assert announce.project_name() == tmp_path.name


@pytest.mark.parametrize("override", ["", "Shared task"])
def test_events_route_by_current_project(monkeypatch, tmp_path, override):
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *args: calls.append(args))
    monkeypatch.setattr(announce, "summarize_with_agent", lambda prompt: prompt)
    monkeypatch.setenv("AGENT_PTT_CHANNEL", override)
    for project in ("storefront", "warehouse"):
        directory = tmp_path / project
        directory.mkdir()
        monkeypatch.setattr(
            announce.sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "fix it",
                        "cwd": str(directory),
                        "session_id": "same-session",
                    }
                )
            ),
        )
        announce.main()
    assert [call[3] for call in calls] == (
        [override, override] if override else ["storefront", "warehouse"]
    )


@pytest.mark.parametrize(
    "record",
    [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "I fixed login. Tests pass."}],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": "I fixed login. Tests pass.",
            },
        },
    ],
)
def test_codex_final_answer_formats(tmp_path, record):
    transcript = tmp_path / "codex.jsonl"
    transcript.write_text(json.dumps(record) + '\n{"partial":')
    assert announce.summarize_transcript(str(transcript)) == "I fixed login. Tests pass"


def test_codex_does_not_reuse_previous_turn_or_speak_commentary(tmp_path):
    transcript = tmp_path / "codex.jsonl"
    records = [
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": "Old result.",
            },
        },
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {
            "type": "response_item",
            "payload": {
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "I'll investigate."}],
            },
        },
    ]
    transcript.write_text("\n".join(map(json.dumps, records)))
    assert announce.summarize_transcript(str(transcript)) == ""


def test_transcript_ignores_reasoning_and_tool_input(tmp_path):
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "Private reasoning"},
                        {"type": "tool_use", "name": "bash", "input": {"command": "echo secret"}},
                    ]
                },
            }
        )
    )
    assert announce.summarize_transcript(str(transcript)) == ""


def test_fallback_preserves_late_caveat_and_skips_headings():
    report = "## Summary\n\nFixed login.\nAdded tests.\nUpdated docs.\nDeployment is still pending."
    assert announce._fallback_result(report) == "Fixed login. Deployment is still pending"


def test_result_keeps_second_sentence(monkeypatch, tmp_path):
    transcript = tmp_path / "report.jsonl"
    transcript.write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": "Fixed login. Tests could not run."}}
        )
    )
    monkeypatch.setattr(announce, "SUMMARIZER", "ollama")
    monkeypatch.setattr(announce, "_ollama", lambda *a: "I fixed login. Tests could not run.")
    assert announce.summarize_result(str(transcript)) == "I fixed login. Tests could not run"


def test_long_report_uses_fallback_instead_of_truncating_caveat(monkeypatch, tmp_path):
    transcript = tmp_path / "report.jsonl"
    report = "Fixed login. " + "Updated documentation. " * 300 + "Tests failed."
    transcript.write_text(json.dumps({"type": "assistant", "message": {"content": report}}))
    monkeypatch.setattr(announce, "_ollama", lambda *a: pytest.fail("truncated report sent"))
    assert announce.summarize_result(str(transcript)) == ""
    assert announce.summarize_transcript(str(transcript)) == "Fixed login. Tests failed"


def test_spoken_report_removes_markdown_and_directory_names():
    summary = announce._fallback_result(
        "## Summary\nSaved the plan in `docs/plans/org-auth.md`.\nSee [the plan](https://example.com)."
    )
    assert "docs/" not in summary
    assert "`" not in summary
    assert "https://" not in summary
    assert "Saved the plan" in summary


def test_recommendation_is_not_mistaken_for_work_status():
    report = (
        "I reviewed the channel. No code changed. "
        "Announce findings and blockers—not every file inspected."
    )
    assert announce._fallback_result(report) == "I reviewed the channel. No code changed"


def test_session_channel_selection_wins_over_repo_and_other_sessions(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *args: calls.append(args))
    monkeypatch.setattr(announce, "summarize_with_agent", lambda prompt: prompt)
    announce.routing.save_channel("s1", announce.AGENT_NAME, announce.BASE_URL, "Release")
    for session_id in ("s1", "s2"):
        monkeypatch.setattr(
            announce.sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "fix it",
                        "cwd": str(tmp_path / "project"),
                        "session_id": session_id,
                    }
                )
            ),
        )
        announce.main()
    assert [call[3] for call in calls] == ["Release", "project"]


def test_stop_summary_survives_a_trailing_tool_call(tmp_path):
    """The final words followed by a tool call and its result are still the report."""
    transcript = tmp_path / "t.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "All done."}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Sched"}]}},
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
        },
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in records))
    assert announce._last_assistant_text(str(transcript)) == "All done."


def test_announcer_sends_bearer_key(monkeypatch):
    monkeypatch.setenv("AGENT_PTT_API_KEY", "secret")
    assert announce._headers()["Authorization"] == "Bearer secret"


def test_repeated_prompt_is_a_short_check_in(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    monkeypatch.setattr(announce, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(announce, "summarize_with_agent", lambda prompt, limit=140: "watch it")
    for _ in range(2):
        monkeypatch.setattr(
            announce.sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "watch the channel",
                        "session_id": "s1",
                        "cwd": "/p",
                    }
                )
            ),
        )
        announce.main()
    assert [c[2] for c in calls] == ["I'm going to watch it.", "Back on it."]


def test_stop_does_not_repeat_narration_already_spoken(monkeypatch, tmp_path):
    """A turn that ends right after narration (e.g. scheduling a wake-up) isn't read twice."""
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixing it."}]}}
        )
    )
    for event in [
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "x", "description": "Run it"},
            "transcript_path": str(transcript),
        },
        {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {}},
        {"hook_event_name": "Stop", "transcript_path": str(transcript)},
    ]:
        event.update({"session_id": "s1", "cwd": "/p"})
        monkeypatch.setattr(announce.sys, "stdin", io.StringIO(json.dumps(event)))
        announce.main()
    assert [c[2] for c in calls] == ["Fixing it. I'm about to run it.", "That's all for this turn."]


def test_prompt_and_stop_update_presence(monkeypatch, tmp_path):
    """The announcer marks the session back at a prompt and away with the result at Stop."""
    calls = []
    commands = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    monkeypatch.setattr(announce, "summarize_with_agent", lambda prompt, limit=140: "fix it")
    announce.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    announce.STATE_FILE.write_text(
        json.dumps(
            {f"identity-v2:{announce.BASE_URL}:Claude:s1": {"channel_id": "c1", "key_id": "k1"}}
        )
    )

    def fake_request(method, path, payload=None, timeout=None):
        commands.append((method, path, payload))
        return {"ok": True}

    monkeypatch.setattr(announce, "_request", fake_request)
    for event in [
        {"hook_event_name": "UserPromptSubmit", "prompt": "fix it"},
        {"hook_event_name": "Stop"},
    ]:
        event.update({"session_id": "s1", "cwd": "/p"})
        monkeypatch.setattr(announce.sys, "stdin", io.StringIO(json.dumps(event)))
        announce.main()
    assert [c[2] for c in calls] == [
        "I'm going to fix it.",
        "The task ended, but I couldn't read its result.",
    ]
    assert commands == [
        ("POST", "/channels/c1/command", {"key_id": "k1", "name": "back", "args": ""}),
        (
            "POST",
            "/channels/c1/command",
            {
                "key_id": "k1",
                "name": "away",
                "args": "The task ended, but I couldn't read its result.",
            },
        ),
    ]


def test_subagent_events_are_silent(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a[:4]))
    monkeypatch.delenv("AGENT_PTT_WORKERS", raising=False)
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "x", "description": "Run it"},
        "transcript_path": str(tmp_path / "s1" / "subagents" / "agent-abc.jsonl"),
        "session_id": "s1",
        "cwd": "/p",
    }
    for name in ["PreToolUse", "PostToolUse"]:
        monkeypatch.setattr(
            announce.sys, "stdin", io.StringIO(json.dumps({**event, "hook_event_name": name}))
        )
        announce.main()
    assert calls == []


def test_message_context_carries_files_tools_and_task(monkeypatch, tmp_path):
    said = []

    def fake_request(method, path, payload=None, timeout=None):
        if path.endswith("/say"):
            said.append(payload)
            return {}
        if path == "/channels":
            return [] if method == "GET" else {"channel_id": "c1"}
        return {"key_id": "k1", "session_id": "s1"}

    monkeypatch.setattr(announce, "_request", fake_request)
    monkeypatch.setattr(announce, "summarize_with_agent", lambda prompt, limit=140: "fix it")
    monkeypatch.setattr(announce, "_git_branch", lambda cwd: "main")
    events = [
        {"hook_event_name": "UserPromptSubmit", "prompt": "fix the login bug"},
        {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": "a.py"}},
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest", "description": "Run tests"},
        },
        {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {}},
        {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": "b.py"}},
        {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {}},
        {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {}},
    ]
    for event in events:
        event.update({"session_id": "s1", "cwd": str(tmp_path)})
        monkeypatch.setattr(announce.sys, "stdin", io.StringIO(json.dumps(event)))
        announce.main()
    contexts = [p["context"] for p in said if "context" in p]
    assert contexts[0]["task"] == "fix the login bug"
    assert contexts[0]["branch"] == "main" and contexts[0]["agent"] == "Claude"
    progress = [c for c in contexts if c.get("files")]
    assert progress, said
    assert progress[0]["files"] == [{"path": "a.py", "op": "edit"}, {"path": "b.py", "op": "edit"}]
    assert progress[0]["tools"] == {"Edit": 2, "Bash": 1}


def test_context_is_dropped_when_server_rejects_it(monkeypatch):
    calls = []

    def fake_request(method, path, payload=None, timeout=None):
        calls.append(payload)
        if "context" in payload:
            raise urllib.error.HTTPError(path, 422, "bad context", {}, None)
        return {}

    monkeypatch.setattr(announce, "_request", fake_request)
    announce._say("c1", "k1", "hello", {"agent": "Claude"})
    assert [("context" in c) for c in calls] == [True, False]


def test_repeat_prompt_survives_other_prompts_in_between(monkeypatch, tmp_path):
    event = {"session_id": "s1"}
    assert announce._is_repeat_prompt(event, "watch the channel") is False
    assert announce._is_repeat_prompt(event, "what is status?") is False
    assert announce._is_repeat_prompt(event, "watch the channel") is True


def test_report_sentences_skip_markdown_tables():
    report = (
        "Yes.\n\n| # | Item | Notes |\n|---|---|---|\n"
        "| 5 | Finish hosting | Recovery |\n\nMigrations remain."
    )
    assert announce._report_sentences(report) == ["Yes", "Migrations remain"]


def _git_repo(path, monkeypatch=None):
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "a.py"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"],
        check=True,
        env={**os.environ, **env},
    )
    return path


def test_display_path_is_repo_relative_or_home_collapsed(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    assert (
        announce.display_path(str(repo / "agent_ptt" / "server.py"), None, str(repo))
        == "agent_ptt/server.py"
    )
    assert announce.display_path("a.py", str(repo), str(repo)) == "a.py"
    inside_home = Path.home() / ".claude" / "projects" / "x.jsonl"
    assert announce.display_path(str(inside_home)) == "~/.claude/projects/x.jsonl"
    assert (
        announce.display_path("notes.md", str(tmp_path)) == "notes.md"
    )  # outside repo and home: untouched


def test_git_facts_report_branch_dirty_and_worktree(tmp_path):
    import subprocess

    repo = _git_repo(tmp_path / "repo")
    facts = announce.git_facts(str(repo))
    assert facts["branch"] == "main" and facts["dirty"] == 0 and "worktree" not in facts
    (repo / "b.py").write_text("y = 2\n")
    assert announce.git_facts(str(repo))["dirty"] == 1
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feature", str(wt)], check=True
    )
    facts = announce.git_facts(str(wt))
    assert facts["branch"] == "feature" and facts["worktree"].endswith("wt")


def test_git_facts_outside_a_repo_only_have_branch_none(tmp_path):
    assert announce.git_facts(str(tmp_path)) == {"branch": None}


def test_bash_paths_are_tracked_as_run(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "deploy.sh").write_text("")
    (tmp_path / "pyproject.toml").write_text("")
    progress = {}
    event = {
        "tool_name": "Bash",
        "cwd": str(tmp_path),
        "tool_input": {"command": "bash scripts/deploy.sh && cat pyproject.toml && ls missing.txt"},
    }
    announce._track_context(event, progress)
    assert progress["files"] == [
        {"path": "scripts/deploy.sh", "op": "run"},
        {"path": "pyproject.toml", "op": "run"},
    ]
    assert progress["tools"] == {"Bash": 1}


def test_trace_id_is_per_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(announce, "STATE_FILE", tmp_path / "state.json")
    event = {"session_id": "s9", "cwd": str(tmp_path)}
    announce._remember_task(event, "first task")
    first = announce.message_context(event)["trace_id"]
    assert len(first) == 32 and announce.message_context(event)["trace_id"] == first
    announce._remember_task(event, "second task")
    second = announce.message_context(event)["trace_id"]
    assert second != first


def test_message_context_has_host_and_collapsed_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(announce, "STATE_FILE", tmp_path / "state.json")
    repo = _git_repo(tmp_path / "repo")
    state = {
        announce._progress_key({"session_id": "s1"}): {
            "files": [{"path": str(repo / "a.py"), "op": "edit"}],
            "tools": {"Edit": 1},
        }
    }
    event = {"session_id": "s1", "cwd": str(repo), "transcript_path": str(Path.home() / "t.jsonl")}
    context = announce.message_context(event, state)
    assert context["files"] == [{"path": "a.py", "op": "edit"}]
    assert context["host"] and context["branch"] == "main" and context["dirty"] == 0
    assert context["transcript"] == "~/t.jsonl"
