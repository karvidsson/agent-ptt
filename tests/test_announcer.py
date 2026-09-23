"""The Claude Code announcer hook (claude-plugin/hooks/announce.py).

The script is stdlib-only; we import it directly for unit tests and run
it as a subprocess for the never-break-coding guarantee.
"""

import importlib.util
import io
import json
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
def _no_real_ollama_or_fork(monkeypatch):
    """Tests never reach a real local model and never fork the test runner."""

    def unavailable(instruction, text):
        raise urllib.error.URLError("ollama not running")

    monkeypatch.setattr(announce, "_ollama", unavailable)
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


def test_tool_subject_describes_file_without_tool_name():
    assert (
        announce._tool_subject({"tool_name": "Read", "tool_input": {"file_path": "src/main.py"}})
        == "the file src/main.py"
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
    assert announce.summarize_transcript(str(transcript)) == "I updated the API"


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
            assert payload == {"name": announce.CHANNEL_NAME}
            return {"channel_id": "chan-1"}
        if path.endswith("/join"):
            return {"key_id": "key-1"}
        if path.endswith("/say"):
            return {"message_id": "m1"}
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(announce, "_request", fake_request)

    announce.announce("s1", "Claude · proj", "hello")

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
        json.dumps({"s1:Claude · proj": {"channel_id": "chan-1", "key_id": "old-key"}})
    )
    live = {"channels": [{"name": announce.CHANNEL_NAME, "channel_id": "chan-1"}]}
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
            return {"key_id": "new-key"}
        if path == "/channels/chan-2/say":
            return {"message_id": "m1"}
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(announce, "_request", fake_request)

    announce.announce("s1", "Claude · proj", "hello")

    assert requests[-1] == ("POST", "/channels/chan-2/say")
    assert json.loads(state_file.read_text())["s1:Claude · proj"] == {
        "channel_id": "chan-2",
        "key_id": "new-key",
    }


def test_agent_name_prefixes_the_handle(monkeypatch):
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a))
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
    assert calls == [("s1", "Codex · my-project", "I'm going to ship it.")]


def test_progress_is_grouped_after_three_completed_tools(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a))
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
    assert calls[0][2].startswith("I'm going to inspect the file src/main.py.")
    assert "I also checked the file src/main.py." in calls[0][2]


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
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a))
    monkeypatch.setattr(
        announce.sys,
        "stdin",
        io.StringIO(json.dumps({"hook_event_name": "Stop", "stop_hook_active": True})),
    )
    announce.main()
    assert calls == []


def test_stop_uses_conversational_completion(monkeypatch):
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a))
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
    assert calls == [("s1", "Claude · my-project", "I've finished working on it.")]


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
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a))
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
    assert calls == [("s1", "Claude · my-project", "I looked into it. I fixed the login flow.")]


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
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a))
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
        ("s1", "Claude · my-project", "I made the hook fork first, and all the tests pass.")
    ]


def test_ignores_unknown_events(monkeypatch):
    calls = []
    monkeypatch.setattr(announce, "announce", lambda *a: calls.append(a))
    monkeypatch.setattr(
        announce.sys,
        "stdin",
        io.StringIO(json.dumps({"hook_event_name": "PreToolUse"})),
    )
    announce.main()
    assert calls == []
