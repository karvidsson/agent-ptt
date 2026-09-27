"""The /say skill script (plugins/voice/scripts/say.py)."""

import importlib.util
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SAY_PY = REPO_ROOT / "plugins" / "voice" / "scripts" / "say.py"

spec = importlib.util.spec_from_file_location("say", SAY_PY)
say = importlib.util.module_from_spec(spec)
spec.loader.exec_module(say)


@pytest.fixture(autouse=True)
def isolated_routing(monkeypatch, tmp_path):
    monkeypatch.setattr(say.routing, "ROUTES_DIR", tmp_path / "routes")
    monkeypatch.setenv("AGENT_PTT_SESSION_ID", "s1")
    monkeypatch.setenv("AGENT_PTT_CHANNEL", "test-room")


def test_clean_message_collapses_whitespace():
    assert say.clean_message("deploy\n  finished,\tall good") == "deploy finished, all good"


def test_clean_message_truncates_at_word_boundary():
    message = "status update " * 60
    result = say.clean_message(message)
    assert len(result) <= say.MAX_SAY_CHARS + 1
    assert result.endswith("…")


def test_clean_message_empty():
    assert say.clean_message("") == ""
    assert say.clean_message("   \n ") == ""


def _run_say(args: list[str], url: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SAY_PY), *args],
        capture_output=True,
        text=True,
        timeout=15,
        env={"PATH": "/usr/bin:/bin", "AGENT_PTT_URL": url, "HOME": "/tmp"},
    )


def test_fails_loud_when_server_down():
    """Explicitly invoked — must report the failure, not swallow it."""
    proc = _run_say(["announce the deploy"], "http://localhost:19999")
    assert proc.returncode == 1
    assert "agent-ptt say failed" in proc.stderr
    assert "uv run agent-ptt server start" in proc.stderr


def test_usage_error_without_message():
    proc = _run_say([], "http://localhost:19999")
    assert proc.returncode == 1
    assert "usage" in proc.stderr


def test_say_reuses_cached_key(monkeypatch, tmp_path):
    """Second call must not join again — the cached key is reused."""
    monkeypatch.setattr(say, "STATE_FILE", tmp_path / "state.json")
    requests = []

    def fake_request(method, path, payload=None, timeout=None):
        requests.append((method, path))
        if path == "/channels":
            return [{"name": "test-room", "channel_id": "chan-1"}]
        if path.endswith("/join"):
            return {"key_id": "key-1"}
        if path.endswith("/say"):
            return {"message_id": "m1"}
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(say, "_request", fake_request)

    say.say("first")
    say.say("second")

    joins = [r for r in requests if r[1].endswith("/join")]
    says = [r for r in requests if r[1].endswith("/say")]
    assert len(joins) == 1
    assert len(says) == 2
    cached = json.loads((tmp_path / "state.json").read_text())
    assert next(iter(cached.values())) == {"channel_id": "chan-1", "key_id": "key-1"}


def test_say_recreates_deleted_channel(monkeypatch, tmp_path):
    """A 404 on say (channel cleared) re-resolves the channel instead of failing."""
    monkeypatch.setattr(say, "STATE_FILE", tmp_path / "state.json")
    live = {"channels": [{"name": "test-room", "channel_id": "chan-1"}]}
    requests = []

    def fake_request(method, path, payload=None, timeout=None):
        requests.append((method, path))
        if method == "GET" and path == "/channels":
            return live["channels"]
        if method == "POST" and path == "/channels":
            live["channels"] = [{"name": payload["name"], "channel_id": "chan-2"}]
            return live["channels"][0]
        if path.endswith("/join"):
            return {"key_id": f"key-for-{path.split('/')[2]}"}
        if path == "/channels/chan-1/say":
            live["channels"] = []  # cleared between lookup and say
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        if path == "/channels/chan-2/say":
            return {"message_id": "m1"}
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(say, "_request", fake_request)

    say.say("hello")

    assert ("POST", "/channels") in requests
    assert requests[-1] == ("POST", "/channels/chan-2/say")
    cached = json.loads((tmp_path / "state.json").read_text())
    assert next(iter(cached.values())) == {"channel_id": "chan-2", "key_id": "key-for-chan-2"}
