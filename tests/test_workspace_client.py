"""Hosted hook transport stays separate from the local participation-key API."""

import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest

from tests.test_workspace_delivery import tenant as tenant_fixture

tenant = tenant_fixture

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "workspace_client", ROOT / "plugins/announcer/hooks/workspace_client.py"
)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


@pytest.fixture
def transport(tenant, monkeypatch, tmp_path):
    t = tenant
    for key, value in {
        "AGENT_PTT_MODE": "workspace",
        "AGENT_PTT_URL": "https://workspace.example",
        "AGENT_PTT_WORKSPACE_ORG": t["org"],
        "AGENT_PTT_WORKSPACE_CHANNEL": t["room"],
        "AGENT_PTT_WORKSPACE_TOKEN": "test-token",
        "AGENT_PTT_RECEIVE": "1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(client.routing, "CONFIG_FILE", tmp_path / "config")
    calls = []

    def request(config, method, suffix, payload=None, *, organization=False):
        calls.append((method, suffix, payload))
        prefix = f"/api/workspace/organizations/{t['org']}" if organization else t["base"]
        path = "/api/workspace/me" if suffix == "/me" else prefix + suffix
        response = t["bot"].request(method, path, json=payload)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(path, response.status_code, "error", {}, None)
        return response.json() if response.status_code != 204 else None

    monkeypatch.setattr(client, "request", request)

    def save(path, state):
        path.write_text(json.dumps(state))

    event = {"session_id": "tenant-session", "hook_event_name": "UserPromptSubmit"}
    return t, calls, event, tmp_path / "inboxes", save


def test_hosted_receive_handoff_and_authenticated_reply(transport, capsys):
    t, calls, event, directory, save = transport
    message = (
        t["owner"]
        .post(
            t["base"] + "/messages",
            json={"text": "Check the tests", "client_id": "request", "recipient_ids": [t["a"]]},
        )
        .json()
    )
    client.deliver(event, directory, save)
    output = json.loads(capsys.readouterr().out)
    assert "Check the tests" in output["hookSpecificOutput"]["additionalContext"]
    assert "--reply-to " + str(message["id"]) in output["hookSpecificOutput"]["additionalContext"]
    assert t["bot"].get(t["base"] + "/inbox").json() == []
    result = client.send("Tests passed", client_id="answer", reply_to=message["id"])
    assert result["sender_id"] == t["a"] and result["reply_to"] == message["id"]
    assert all("/channels/" not in suffix for _, suffix, _ in calls)


def test_failed_ack_then_reconnect_does_not_reinject(transport, monkeypatch, capsys):
    t, _, event, directory, save = transport
    t["owner"].post(
        t["base"] + "/messages",
        json={"text": "Please respond", "client_id": "request", "recipient_ids": [t["a"]]},
    )
    request = client.request

    def failure(config, method, suffix, payload=None):
        if suffix == "/inbox/ack":
            raise urllib.error.URLError("offline")
        return request(config, method, suffix, payload)

    monkeypatch.setattr(client, "request", failure)
    with pytest.raises(urllib.error.URLError):
        client.deliver(event, directory, save)
    assert capsys.readouterr().out
    with pytest.raises(urllib.error.URLError):
        client.deliver(event, directory, save)
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(client, "request", request)
    client.deliver(event, directory, save)
    assert capsys.readouterr().out == ""
    assert t["bot"].get(t["base"] + "/inbox").json() == []


def test_reply_retry_keeps_same_client_id(transport, monkeypatch):
    original = client.request
    sent = []

    def lost_response(config, method, suffix, payload=None):
        if method != "POST":
            return original(config, method, suffix, payload)
        sent.append(payload.copy())
        result = original(config, method, suffix, payload)
        if len(sent) == 1:
            raise urllib.error.URLError("response lost")
        return result

    monkeypatch.setattr(client, "request", lost_response)
    assert client.send("Reply")["sender_kind"] == "agent"
    assert len(sent) == 2 and sent[0] == sent[1]


def test_revocation_prevents_new_handoff(transport, capsys):
    t, _, event, directory, save = transport
    t["owner"].post(
        t["base"] + "/messages",
        json={"text": "Request", "client_id": "request", "recipient_ids": [t["a"]]},
    )
    t["owner"].delete(f"/api/workspace/organizations/{t['org']}/agents/{t['a']}")
    with pytest.raises(urllib.error.HTTPError) as error:
        client.deliver(event, directory, save)
    assert error.value.code == 401
    assert capsys.readouterr().out == ""


def test_misconfigured_workspace_fails_closed(transport, monkeypatch):
    _, calls, event, directory, save = transport
    monkeypatch.setenv("AGENT_PTT_WORKSPACE_TOKEN", "")
    with pytest.raises(ValueError):
        client.deliver(event, directory, save)
    assert calls == []
    monkeypatch.setenv("AGENT_PTT_WORKSPACE_TOKEN", "secret")
    monkeypatch.setenv("AGENT_PTT_URL", "http://public.example")
    with pytest.raises(ValueError):
        client.configuration()
    assert client.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other") is None
    monkeypatch.setenv("AGENT_PTT_MODE", "workspce")
    with pytest.raises(ValueError):
        client.enabled()


def test_human_credential_cannot_be_used_as_an_agent(transport, monkeypatch):
    t, calls, _, _, _ = transport
    original = client.request

    def human_identity(config, method, suffix, payload=None):
        if suffix == "/me":
            return t["owner"].get("/api/workspace/me").json()
        return original(config, method, suffix, payload)

    monkeypatch.setattr(client, "request", human_identity)
    with pytest.raises(ValueError, match="agent credential"):
        client.send("No impersonation")
    assert calls == []


def test_workspace_client_copies_match():
    assert (ROOT / "plugins/announcer/hooks/workspace_client.py").read_bytes() == (
        ROOT / "plugins/codex-announcer/workspace_client.py"
    ).read_bytes()


def test_hosted_presence_reports_only_observed_hooks_and_throttles_heartbeats(transport):
    t, calls, event, directory, _ = transport
    client.report_presence({**event, "prompt": "Run the test suite"}, directory)
    first_calls = len(calls)
    client.report_presence({**event, "hook_event_name": "PostToolUse"}, directory)
    assert len(calls) == first_calls
    base = f"/api/workspace/organizations/{t['org']}/sessions"
    row = t["owner"].get(base).json()[0]
    assert row["principal_id"] == t["a"]
    assert row["state"] == "working" and row["task"] == "Run the test suite"
    client.report_presence({**event, "hook_event_name": "Stop"}, directory)
    assert t["owner"].get(base).json()[0]["state"] == "idle"


def test_connection_policy_notice_once_and_never_blocks_empty_stop(transport, capsys):
    t, _, event, directory, save = transport
    assert "mention_policy" not in t["owner"].get("/api/workspace/me").json()
    policy = t["bot"].get("/api/workspace/me").json()["mention_policy"]
    assert policy["idle_wakeup"] is False
    client.deliver({**event, "hook_event_name": "Stop"}, directory, save)
    assert capsys.readouterr().out == ""
    client.deliver(event, directory, save)
    output = json.loads(capsys.readouterr().out)
    assert "No manual polling loop" in output["hookSpecificOutput"]["additionalContext"]
    client.deliver(event, directory, save)
    assert capsys.readouterr().out == ""


def test_server_policy_controls_tool_throttle_but_not_prompt_or_stop(
    transport, monkeypatch, capsys
):
    _, calls, event, directory, save = transport
    original = client.request

    def request(config, method, suffix, payload=None):
        result = original(config, method, suffix, payload)
        if suffix == "/me":
            result["mention_policy"]["tool_poll_interval_seconds"] = 10
        return result

    monkeypatch.setattr(client, "request", request)
    monkeypatch.setattr(client.time, "time", lambda: 100)
    client.deliver(event, directory, save)
    assert "every 10 seconds" in capsys.readouterr().out
    count = len(calls)
    monkeypatch.setattr(client.time, "time", lambda: 105)
    client.deliver({**event, "hook_event_name": "PostToolUse"}, directory, save)
    assert len(calls) == count
    client.deliver({**event, "hook_event_name": "Stop"}, directory, save)
    assert len(calls) > count
    count = len(calls)
    monkeypatch.setattr(client.time, "time", lambda: 116)
    client.deliver({**event, "hook_event_name": "PostToolUse"}, directory, save)
    assert len(calls) > count
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"transport": "shell"},
        {"idle_wakeup": True},
        {"tool_poll_interval_seconds": 0},
        {"tool_poll_interval_seconds": 61},
        {"tool_poll_interval_seconds": float("nan")},
        {"tool_poll_interval_seconds": "2"},
        {"check_on": ["Stop"]},
    ],
)
def test_unsupported_policy_uses_existing_hook_defaults(change):
    from agent_ptt.mention_policy import mention_policy

    assert client.receive_policy({**mention_policy(), **change}) is None
    assert client.receive_policy(None) is None


def test_old_server_without_policy_still_delivers_mentions(transport, monkeypatch, capsys):
    t, _, event, directory, save = transport
    original = client.request

    def request(config, method, suffix, payload=None):
        result = original(config, method, suffix, payload)
        if suffix == "/me":
            result.pop("mention_policy", None)
        return result

    monkeypatch.setattr(client, "request", request)
    client.deliver(event, directory, save)
    assert capsys.readouterr().out == ""
    t["owner"].post(
        t["base"] + "/messages",
        json={
            "text": "Still works",
            "client_id": "legacy",
            "recipient_ids": [t["a"]],
        },
    )
    client.deliver(event, directory, save)
    assert "Still works" in capsys.readouterr().out
