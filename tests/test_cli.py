"""IRC-style channel commands in the CLI: names, whois, me, notice, topic, away, back.

httpx is faked — no server needed. Each command posts
`{"key_id", "name", "args"}` to `POST /channels/{id}/command` and prints a
short human-readable result (or the raw result JSON with `--json`).
"""

from __future__ import annotations

import json
import json as json_mod
import os
import platform
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import agent_ptt.cli as cli

runner = CliRunner()

CHANNEL = "chan-123"
KEY = "key-abc"


def _flat(output: str) -> str:
    return " ".join(output.split())


@pytest.fixture
def session(monkeypatch, tmp_path):
    """Point the CLI at a temp session file with a joined channel."""
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({"key_id": KEY, "channel_id": CHANNEL, "handle": "Claude"}))
    monkeypatch.setattr(cli, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(cli, "SESSION_FILE", session_file)
    return session_file


@pytest.fixture
def fake_post(monkeypatch):
    """Capture httpx.post calls; reply with whatever `respond` was set to."""
    calls: list[dict] = []
    state = {"status": 200, "body": {"ok": True, "name": "", "result": {}}}

    def post(url, json=None, headers=None, **kwargs):
        calls.append({"url": url, "json": json, "headers": headers, "params": kwargs.get("params")})
        body = state["body"]
        return SimpleNamespace(
            status_code=state["status"], json=lambda: body, text=json_mod.dumps(body)
        )

    monkeypatch.setattr(cli.httpx, "post", post)

    def respond(result: dict, name: str = "", status: int = 200, body: dict | None = None):
        state["status"] = status
        state["body"] = body if body is not None else {"ok": True, "name": name, "result": result}

    calls_ns = SimpleNamespace(calls=calls, respond=respond)
    return calls_ns


ACTIVE = {
    "key_id": KEY,
    "handle": "Claude",
    "voice_id": "marius",
    "state": "active",
    "away_reason": None,
    "since": "2026-09-27T10:00:00+00:00",
    "last_active": "2026-09-27T10:05:30+00:00",
    "doing": "reviews the diff",
    "created_at": "2026-09-27T09:58:00+00:00",
}
AWAY = {
    **ACTIVE,
    "key_id": "key-2",
    "handle": "Codex",
    "state": "away",
    "away_reason": "lunch",
    "doing": None,
}


def _assert_request(fake_post, name: str, args: str):
    assert len(fake_post.calls) == 1
    call = fake_post.calls[0]
    assert call["url"].endswith(f"/channels/{CHANNEL}/command")
    assert call["json"] == {"key_id": KEY, "name": name, "args": args}


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------


def test_names_table(session, fake_post):
    fake_post.respond({"participants": [ACTIVE, AWAY]}, name="names")
    result = runner.invoke(cli.app, ["names"])
    assert result.exit_code == 0, result.output
    _assert_request(fake_post, "names", "")
    out = _flat(result.output)
    assert "Claude" in out and "active" in out
    assert "reviews the diff" in out
    assert "Codex" in out and "away (lunch)" in out
    assert "10:05:30" in out  # last_active trimmed to HH:MM:SS


def test_names_empty(session, fake_post):
    fake_post.respond({"participants": []}, name="names")
    result = runner.invoke(cli.app, ["names"])
    assert result.exit_code == 0
    assert "Nobody in the channel" in result.output


def test_names_json(session, fake_post):
    fake_post.respond({"participants": [ACTIVE]}, name="names")
    result = runner.invoke(cli.app, ["names", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"participants": [ACTIVE]}


# ---------------------------------------------------------------------------
# whois
# ---------------------------------------------------------------------------


def test_whois_lines(session, fake_post):
    last = {"handle": "Claude", "text": "hello there", "timestamp": "2026-09-27T10:05:30+00:00"}
    fake_post.respond({"participant": ACTIVE, "last_message": last}, name="whois")
    result = runner.invoke(cli.app, ["whois", "cla"])
    assert result.exit_code == 0, result.output
    _assert_request(fake_post, "whois", "cla")
    out = result.output
    assert "Handle: Claude" in out
    assert "State: active" in out
    assert "Doing: reviews the diff" in out
    assert "Voice: marius" in out
    assert "Last message: 10:05:30 hello there" in out


def test_whois_no_last_message(session, fake_post):
    fake_post.respond({"participant": AWAY, "last_message": None}, name="whois")
    result = runner.invoke(cli.app, ["whois", "codex"])
    assert result.exit_code == 0
    assert "State: away (lunch)" in result.output
    assert "Last message: none" in result.output


def test_whois_unknown_handle_exits_1(session, fake_post):
    fake_post.respond({}, status=404, body={"error": "no participant matching zed"})
    result = runner.invoke(cli.app, ["whois", "zed"])
    assert result.exit_code == 1
    assert "no participant matching zed" in result.output


# ---------------------------------------------------------------------------
# me / notice
# ---------------------------------------------------------------------------


def test_me_prints_action(session, fake_post):
    msg = {"handle": "Claude", "text": "waves", "kind": "action"}
    fake_post.respond({"message": msg}, name="me")
    result = runner.invoke(cli.app, ["me", "waves"])
    assert result.exit_code == 0, result.output
    _assert_request(fake_post, "me", "waves")
    assert "* Claude waves" in _flat(result.output)


def test_notice_prints_confirmation(session, fake_post):
    msg = {"handle": "Claude", "text": "build is green", "kind": "notice"}
    fake_post.respond({"message": msg}, name="notice")
    result = runner.invoke(cli.app, ["notice", "build is green"])
    assert result.exit_code == 0, result.output
    _assert_request(fake_post, "notice", "build is green")
    assert "Notice sent: build is green" in _flat(result.output)


def test_me_json(session, fake_post):
    msg = {"handle": "Claude", "text": "waves", "kind": "action"}
    fake_post.respond({"message": msg}, name="me")
    result = runner.invoke(cli.app, ["me", "waves", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"message": msg}


# ---------------------------------------------------------------------------
# topic
# ---------------------------------------------------------------------------


def test_topic_set(session, fake_post):
    fake_post.respond({"topic": "ship v0.2", "topic_set_by": "Claude"}, name="topic")
    result = runner.invoke(cli.app, ["topic", "ship v0.2"])
    assert result.exit_code == 0, result.output
    _assert_request(fake_post, "topic", "ship v0.2")
    assert "Topic set: ship v0.2 (set by Claude)" in _flat(result.output)


def test_topic_show(session, fake_post):
    fake_post.respond({"topic": "ship v0.2", "topic_set_by": "Codex"}, name="topic")
    result = runner.invoke(cli.app, ["topic"])
    assert result.exit_code == 0, result.output
    _assert_request(fake_post, "topic", "")
    assert "Topic: ship v0.2 (set by Codex)" in _flat(result.output)


def test_topic_unset(session, fake_post):
    fake_post.respond({"topic": None, "topic_set_by": None}, name="topic")
    result = runner.invoke(cli.app, ["topic"])
    assert result.exit_code == 0
    assert "No topic set" in result.output


# ---------------------------------------------------------------------------
# away / back
# ---------------------------------------------------------------------------


def test_away_with_reason(session, fake_post):
    fake_post.respond({"participant": {**ACTIVE, "state": "away", "away_reason": "lunch"}})
    result = runner.invoke(cli.app, ["away", "lunch"])
    assert result.exit_code == 0, result.output
    _assert_request(fake_post, "away", "lunch")
    assert "Claude is away: lunch" in _flat(result.output)


def test_away_without_reason(session, fake_post):
    fake_post.respond({"participant": {**ACTIVE, "state": "away"}})
    result = runner.invoke(cli.app, ["away"])
    assert result.exit_code == 0
    _assert_request(fake_post, "away", "")
    out = _flat(result.output)
    assert "Claude is away" in out and "away:" not in out


def test_back(session, fake_post):
    fake_post.respond({"participant": ACTIVE}, name="back")
    result = runner.invoke(cli.app, ["back"])
    assert result.exit_code == 0, result.output
    _assert_request(fake_post, "back", "")
    assert "Claude is back" in _flat(result.output)


def test_back_json(session, fake_post):
    fake_post.respond({"participant": ACTIVE}, name="back")
    result = runner.invoke(cli.app, ["back", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"participant": ACTIVE}


# ---------------------------------------------------------------------------
# errors and session handling
# ---------------------------------------------------------------------------


def test_unknown_command_error_exits_1(session, fake_post):
    fake_post.respond({}, status=400, body={"error": "unknown command: names"})
    result = runner.invoke(cli.app, ["names"])
    assert result.exit_code == 1
    assert "unknown command: names" in result.output


def test_fastapi_detail_error_is_shown(session, fake_post):
    fake_post.respond({}, status=404, body={"detail": "Unknown participation key"})
    result = runner.invoke(cli.app, ["back"])
    assert result.exit_code == 1
    assert "Unknown participation key" in result.output


def test_not_joined_exits_1(monkeypatch, tmp_path, fake_post):
    monkeypatch.setattr(cli, "SESSION_FILE", tmp_path / "missing.json")
    result = runner.invoke(cli.app, ["names"])
    assert result.exit_code == 1
    assert "Not in any channel" in result.output
    assert fake_post.calls == []


def test_auth_header_is_sent(session, fake_post, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_API_KEY", "secret")
    fake_post.respond({"participant": ACTIVE})
    result = runner.invoke(cli.app, ["back"])
    assert result.exit_code == 0
    assert fake_post.calls[0]["headers"] == {"Authorization": "Bearer secret"}


def test_auth_key_falls_back_to_announcer_env(session, fake_post, monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_PTT_API_KEY", raising=False)
    (tmp_path / "announcer.env").write_text("AGENT_NAME=x\nAGENT_PTT_API_KEY='from-file'\n")
    fake_post.respond({"participant": ACTIVE})
    result = runner.invoke(cli.app, ["back"])
    assert result.exit_code == 0
    assert fake_post.calls[0]["headers"] == {"Authorization": "Bearer from-file"}
    monkeypatch.setenv("AGENT_PTT_API_KEY", "from-env")
    assert cli._auth_headers() == {"Authorization": "Bearer from-env"}


# ---------------------------------------------------------------------------
# leave
# ---------------------------------------------------------------------------


def test_leave_sends_key_in_body_not_query(session, fake_post):
    fake_post.respond({}, body={"status": "left"})
    result = runner.invoke(cli.app, ["leave"])
    assert result.exit_code == 0, result.output
    call = fake_post.calls[0]
    assert call["url"].endswith(f"/channels/{CHANNEL}/leave")
    assert call["json"] == {"key_id": KEY}
    assert call["params"] is None
    assert KEY not in call["url"]
    saved = json.loads(session.read_text())
    assert "key_id" not in saved and "channel_id" not in saved


# ---------------------------------------------------------------------------
# say: rejoin once when the server dropped the key (left or reaped as idle)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ws(monkeypatch):
    """Fake the agent WebSocket: keys in `refused` are rejected at the handshake."""
    import websockets
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    state = SimpleNamespace(refused={KEY}, sent=[])

    class Socket:
        def __init__(self, uri):
            self.uri = uri

        async def __aenter__(self):
            if any(f"key={key}" in self.uri for key in state.refused):
                raise InvalidStatus(Response(403, "Forbidden", Headers()))
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, data):
            state.sent.append((self.uri, json.loads(data)))

        async def recv(self):
            text = state.sent[-1][1]["text"]
            return json.dumps({"type": "message", "handle": "Claude", "text": text})

    monkeypatch.setattr(websockets, "connect", lambda uri, **kwargs: Socket(uri))
    return state


def _route_posts(monkeypatch, routes: dict[str, tuple[int, dict]]):
    calls: list[tuple[str, dict]] = []

    def post(url, json=None, headers=None, **kwargs):
        calls.append((url, json))
        status, body = next(v for k, v in routes.items() if url.endswith(k))
        return SimpleNamespace(status_code=status, json=lambda: body, text=json_mod.dumps(body))

    monkeypatch.setattr(cli.httpx, "post", post)
    return calls


def test_say_sends_with_saved_key(session, fake_ws, monkeypatch):
    fake_ws.refused = set()
    calls = _route_posts(monkeypatch, {})
    result = runner.invoke(cli.app, ["say", "hello"])
    assert result.exit_code == 0, result.output
    assert calls == []
    assert f"key={KEY}" in fake_ws.sent[0][0]


def test_say_fyi_flag_marks_the_frame(session, fake_ws, monkeypatch):
    fake_ws.refused = set()
    _route_posts(monkeypatch, {})
    assert runner.invoke(cli.app, ["say", "plain"]).exit_code == 0
    assert "fyi" not in fake_ws.sent[-1][1]  # Old servers see the same frame as before.
    result = runner.invoke(cli.app, ["say", "--fyi", "build is green"])
    assert result.exit_code == 0, result.output
    assert fake_ws.sent[-1][1] == {"type": "message", "text": "build is green", "fyi": True}


def test_say_rejoins_once_when_key_was_dropped(session, fake_ws, monkeypatch):
    saved = json.loads(session.read_text())
    session.write_text(json.dumps({**saved, "voice": "marius"}))
    calls = _route_posts(
        monkeypatch,
        {
            "/heartbeat": (404, {"error": "Participant not found in channel"}),
            "/join": (200, {"key_id": "key-new", "voice_id": "marius", "handle": "Claude"}),
        },
    )
    result = runner.invoke(cli.app, ["say", "still here"])
    assert result.exit_code == 0, result.output
    assert "rejoined as Claude" in _flat(result.output)
    assert calls == [
        (f"{cli.DEFAULT_BASE_URL}/channels/{CHANNEL}/heartbeat", {"key_id": KEY}),
        (
            f"{cli.DEFAULT_BASE_URL}/channels/{CHANNEL}/join",
            {
                "handle": "Claude",
                "voice_id": "marius",
                "session_id": json.loads(session.read_text())["session_id"],
                "agent": "cli",
                "pin_handle": True,
                "machine": platform.node().strip()[:100] or None,
                "folder": os.getcwd()[:500],
            },
        ),
    ]
    assert [(uri.endswith("key=key-new"), msg["text"]) for uri, msg in fake_ws.sent] == [
        (True, "still here")
    ]
    saved = json.loads(session.read_text())
    assert (saved["key_id"], saved["channel_id"], saved["voice"]) == ("key-new", CHANNEL, "marius")


def test_say_does_not_undo_a_kick(session, fake_ws, monkeypatch):
    calls = _route_posts(monkeypatch, {"/heartbeat": (403, {"detail": "removed"})})
    result = runner.invoke(cli.app, ["say", "hi"])
    assert result.exit_code == 1
    assert "refused" in result.output
    assert [url for url, _ in calls] == [f"{cli.DEFAULT_BASE_URL}/channels/{CHANNEL}/heartbeat"]
    assert json.loads(session.read_text())["key_id"] == KEY


def test_say_gives_up_if_the_new_key_is_refused_too(session, fake_ws, monkeypatch):
    fake_ws.refused = {KEY, "key-new"}
    _route_posts(
        monkeypatch,
        {
            "/heartbeat": (404, {"error": "Participant not found in channel"}),
            "/join": (200, {"key_id": "key-new", "voice_id": "marius"}),
        },
    )
    result = runner.invoke(cli.app, ["say", "hi"])
    assert result.exit_code == 1
    assert len(fake_ws.sent) == 0


def test_say_does_not_rejoin_on_a_server_without_heartbeat(session, fake_ws, monkeypatch):
    # FastAPI's route-missing 404 has no "error" field: not proof the key is gone.
    calls = _route_posts(monkeypatch, {"/heartbeat": (404, {"detail": "Not Found"})})
    result = runner.invoke(cli.app, ["say", "hi"])
    assert result.exit_code == 1
    assert len(calls) == 1
