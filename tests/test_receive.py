"""Exercise the real receiving hook against the API with stdout handoff checks."""

import fcntl
import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugins/announcer/hooks/receive.py"
spec = importlib.util.spec_from_file_location("receive", SOURCE)
receive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receive)


@pytest.fixture
def hook(client, monkeypatch, tmp_path):
    monkeypatch.setattr(receive, "STATE_DIR", tmp_path / "inboxes")
    monkeypatch.setattr(receive.routing, "CONFIG_FILE", tmp_path / "config")
    monkeypatch.setattr(receive.routing, "ROUTES_DIR", tmp_path / "routes")
    monkeypatch.setattr(receive.routing, "project_name", lambda cwd: "project")
    monkeypatch.setenv("AGENT_PTT_AGENT", "Claude")
    monkeypatch.delenv("AGENT_PTT_CHANNEL", raising=False)
    monkeypatch.delenv("AGENT_PTT_RECEIVE", raising=False)
    monkeypatch.delenv("AGENT_PTT_URL", raising=False)
    calls = []

    def request(base, method, path, payload=None, key=None):
        calls.append((method, path, payload))
        headers = {"X-Participant-Key": key} if key else {}
        response = client.request(method, path, json=payload, headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(base + path, response.status_code, "error", {}, None)
        return response.json()

    monkeypatch.setattr(receive, "request", request)
    event = {"session_id": "session", "hook_event_name": "UserPromptSubmit", "cwd": "/project"}
    receive.deliver(event)
    cid = client.get("/channels").json()[0]["channel_id"]
    participants = client.get(f"/channels/{cid}").json()["participants"]
    agent = next(iter(participants.values()))
    human = client.post(f"/channels/{cid}/join", json={"handle": "User", "voice_id": "fake"}).json()

    def send(text="run the tests"):
        return client.post(
            f"/channels/{cid}/say",
            json={"key_id": human["key_id"], "text": f'@"{agent["handle"]}" {text}'},
        ).json()

    return event, send, calls, cid, agent


@pytest.mark.parametrize("name", ["UserPromptSubmit", "PostToolUse", "Stop"])
def test_hook_hands_off_targeted_content_then_acknowledges(hook, monkeypatch, capsys, name):
    event, send, calls, _, _ = hook
    message = send()
    event["hook_event_name"] = name
    now = receive.time.time()
    monkeypatch.setattr(receive.time, "time", lambda: now + 3)
    original = receive.request
    handoffs = []

    def request(base, method, path, payload=None, key=None):
        if path.endswith("/ack"):
            # The acknowledgement must happen only after model-visible stdout.
            output = capsys.readouterr().out
            assert message["message_id"] in output
            handoffs.append(json.loads(output))
        return original(base, method, path, payload, key)

    monkeypatch.setattr(receive, "request", request)
    receive.deliver(event)
    assert len(handoffs) == 1
    result = handoffs[0]
    if name == "Stop":
        assert result["decision"] == "block"
        assert "run the tests" in result["reason"]
    else:
        assert result["hookSpecificOutput"]["hookEventName"] == name
        assert "run the tests" in result["hookSpecificOutput"]["additionalContext"]
    monkeypatch.setattr(receive, "request", original)
    receive.deliver(event)
    assert capsys.readouterr().out == ""
    assert not any("/history" in path for _, path, _ in calls)


def test_failed_ack_retries_without_reinjecting(hook, monkeypatch, capsys):
    event, send, _, _, _ = hook
    message = send()
    original = receive.request

    def request(base, method, path, payload=None, key=None):
        if path.endswith("/ack"):
            raise OSError("offline")
        return original(base, method, path, payload, key)

    monkeypatch.setattr(receive, "request", request)
    with pytest.raises(OSError):
        receive.deliver(event)
    assert message["message_id"] in capsys.readouterr().out
    receive.deliver(event)
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(receive, "request", original)
    receive.deliver(event)
    assert capsys.readouterr().out == ""


def test_stdout_failure_keeps_message_pending(hook, monkeypatch, capsys):
    event, send, _, _, _ = hook
    message = send()

    def broken(*args, **kwargs):
        raise BrokenPipeError("host closed stdout")

    with monkeypatch.context() as patch:
        patch.setattr("builtins.print", broken)
        with pytest.raises(BrokenPipeError):
            receive.deliver(event)
    receive.deliver(event)
    assert message["message_id"] in capsys.readouterr().out


def test_throttle_loop_guard_and_concurrent_hook_leave_pending(hook, monkeypatch, capsys):
    event, send, calls, _, _ = hook
    message = send()
    count = len(calls)
    receive.deliver({**event, "hook_event_name": "PostToolUse"})
    receive.deliver({**event, "hook_event_name": "Stop", "stop_hook_active": True})
    receive.deliver({**event, "agent_id": "subagent"})
    with next(receive.STATE_DIR.glob("*.lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receive.deliver(event)
    assert len(calls) == count
    assert capsys.readouterr().out == ""
    receive.deliver(event)
    assert message["message_id"] in capsys.readouterr().out


def test_channel_switch_does_not_consume_old_channel(hook, capsys):
    event, send, _, _, _ = hook
    message = send()
    receive.routing.save_channel("session", "Claude", "http://localhost:8770", "other")
    receive.deliver(event)
    notice = json.loads(capsys.readouterr().out)
    assert "mention checks are enabled" in notice["hookSpecificOutput"]["additionalContext"]
    assert message["message_id"] not in str(notice)
    receive.routing.save_channel("session", "Claude", "http://localhost:8770", None)
    receive.deliver(event)
    assert message["message_id"] in capsys.readouterr().out


def test_disabled_receiver_is_noop(hook, monkeypatch, capsys):
    event, send, calls, _, _ = hook
    send()
    count = len(calls)
    monkeypatch.setenv("AGENT_PTT_RECEIVE", "0")
    receive.deliver(event)
    assert len(calls) == count
    assert capsys.readouterr().out == ""


def test_plugin_copies_and_synchronous_config():
    assert SOURCE.read_bytes() == (ROOT / "plugins/codex-announcer/receive.py").read_bytes()
    for path in (
        "plugins/announcer/hooks/hooks.json",
        "plugins/codex-announcer/hooks.json.template",
    ):
        hooks = json.loads((ROOT / path).read_text())["hooks"]
        for event in ("UserPromptSubmit", "PostToolUse", "Stop"):
            handlers = [
                h for group in hooks[event] for h in group["hooks"] if "receive.py" in h["command"]
            ]
            assert len(handlers) == 1
            assert not handlers[0].get("async", False)


def test_real_http_helper_includes_auth_and_participant_key(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b"[]"

    seen = []
    monkeypatch.setenv("AGENT_PTT_API_KEY", "secret")
    monkeypatch.setattr(
        receive.urllib.request, "urlopen", lambda req, **kw: seen.append(req) or Response()
    )
    receive.request("http://example", "GET", "/inbox", key="participant")
    assert seen[0].get_header("Authorization") == "Bearer secret"
    assert seen[0].get_header("X-participant-key") == "participant"


def test_local_policy_notice_is_deferred_at_empty_stop(hook, capsys):
    event, _, _, _, _ = hook
    path = next(receive.STATE_DIR.glob("*.json"))
    state = json.loads(path.read_text())
    state.pop("policy_notified", None)
    receive.save(path, state)
    receive.deliver({**event, "hook_event_name": "Stop"})
    assert capsys.readouterr().out == ""
    receive.deliver(event)
    notice = json.loads(capsys.readouterr().out)
    assert "No manual polling loop" in notice["hookSpecificOutput"]["additionalContext"]
    receive.deliver(event)
    assert capsys.readouterr().out == ""
