"""REST + WebSocket endpoints via TestClient, with TTS and playback faked."""

import time

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.conftest import FAKE_AUDIO


def _create_channel(client, name="Test Room") -> str:
    return client.post("/channels", json={"name": name}).json()["channel_id"]


def _join(client, channel_id, handle="Claude", voice_id="alba") -> str:
    resp = client.post(
        f"/channels/{channel_id}/join",
        json={"handle": handle, "voice_id": voice_id},
    )
    return resp.json()["key_id"]


def _wait_for(condition, timeout=5.0) -> bool:
    """Poll until condition() is truthy; the app loop runs in TestClient's thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def test_create_channel(client):
    resp = client.post("/channels", json={"name": "War Room"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "War Room"
    assert body["channel_id"]


def test_list_channels(client):
    assert client.get("/channels").json() == []
    channel_id = _create_channel(client)
    listed = client.get("/channels").json()
    assert [c["channel_id"] for c in listed] == [channel_id]


def test_delete_all_channels(client):
    first = _create_channel(client, "First")
    second = _create_channel(client, "Second")

    resp = client.delete("/channels", headers={"X-Confirm-Delete-All": "yes"})

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 2}
    assert client.get("/channels").json() == []
    assert client.get(f"/channels/{first}").status_code == 404
    assert client.get(f"/channels/{second}").status_code == 404


def test_delete_one_empty_channel(client):
    channel_id = _create_channel(client)

    resp = client.delete(f"/channels/{channel_id}")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": channel_id}
    assert client.get("/channels").json() == []


def test_delete_one_channel_with_agent_is_rejected(client):
    channel_id = _create_channel(client)
    _join(client, channel_id)

    resp = client.delete(f"/channels/{channel_id}")

    assert resp.status_code == 409
    assert resp.json() == {"error": "Channel has connected agents"}
    assert client.get(f"/channels/{channel_id}").status_code == 200


def test_delete_one_unknown_channel(client):
    assert client.delete("/channels/nonexistent").status_code == 404


def test_get_channel_detail(client):
    channel_id = _create_channel(client)
    resp = client.get(f"/channels/{channel_id}")
    assert resp.status_code == 200
    assert resp.json()["channel_id"] == channel_id


def test_list_voices_defaults_to_designed_backend(client, fake_tts):
    resp = client.get("/voices")

    assert resp.status_code == 200
    assert resp.json()[0]["voice_id"] == "fake-voice"


def test_get_unknown_channel(client):
    assert client.get("/channels/nonexistent").status_code == 404


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------


def test_root_redirects_to_ui(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"] == "/ui/"


def test_ui_serves_html(client):
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Agent PTT" in resp.text


def test_join_channel(client):
    channel_id = _create_channel(client)
    resp = client.post(
        f"/channels/{channel_id}/join",
        json={"handle": "Claude", "voice_id": "alba"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["handle"] == "Claude"
    assert body["channel_id"] == channel_id
    assert body["key_id"]


def test_join_unknown_channel(client):
    resp = client.post("/channels/nonexistent/join", json={"handle": "Claude"})
    assert resp.status_code == 404


def test_leave_channel(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)
    resp = client.post(f"/channels/{channel_id}/leave", params={"key_id": key_id})
    assert resp.status_code == 200
    assert resp.json() == {"status": "left"}


def test_leave_with_unknown_key(client):
    channel_id = _create_channel(client)
    resp = client.post(f"/channels/{channel_id}/leave", params={"key_id": "nonexistent"})
    assert resp.status_code == 404


def test_history_empty(client):
    channel_id = _create_channel(client)
    assert client.get(f"/channels/{channel_id}/history").json() == []


# ---------------------------------------------------------------------------
# REST say
# ---------------------------------------------------------------------------


def test_rest_say_full_pipeline(client, fake_tts, fake_mixer):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)

    resp = client.post(
        f"/channels/{channel_id}/say",
        json={"key_id": key_id, "text": "hello over REST"},
    )
    assert resp.status_code == 200
    assert resp.json()["handle"] == "Claude"

    history = client.get(f"/channels/{channel_id}/history").json()
    assert [m["text"] for m in history] == ["hello over REST"]
    assert _wait_for(lambda: fake_mixer.enqueued), "REST message never reached TTS"
    assert [text for text, _voice in fake_tts.calls] == ["hello over REST"]


def test_rest_say_broadcasts_to_ws_clients(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)

    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        resp = client.post(
            f"/channels/{channel_id}/say",
            json={"key_id": key_id, "text": "REST to WS"},
        )
        assert resp.status_code == 200
        received = ws.receive_json()

    assert received["type"] == "message"
    assert received["text"] == "REST to WS"


def test_rest_say_unknown_key(client):
    channel_id = _create_channel(client)
    resp = client.post(
        f"/channels/{channel_id}/say",
        json={"key_id": "bogus", "text": "hello"},
    )
    assert resp.status_code == 404


def test_rest_say_key_from_other_channel(client):
    channel_a = _create_channel(client, "A")
    channel_b = _create_channel(client, "B")
    key_a = _join(client, channel_a)

    resp = client.post(f"/channels/{channel_b}/say", json={"key_id": key_a, "text": "hi"})
    assert resp.status_code == 404


def test_rest_say_empty_text(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)
    resp = client.post(f"/channels/{channel_id}/say", json={"key_id": key_id, "text": "   "})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Agent WebSocket
# ---------------------------------------------------------------------------


def test_ws_unknown_channel_rejected(client):
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/channels/nonexistent/ws"),
    ):
        pass
    assert exc_info.value.code == 4004


def test_ws_invalid_key_rejected(client):
    channel_id = _create_channel(client)
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(f"/channels/{channel_id}/ws?key=bogus"),
    ):
        pass
    assert exc_info.value.code == 4001


def test_ws_send_message_broadcasts(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)

    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        ws.send_json({"type": "message", "text": "hello channel"})
        received = ws.receive_json()

    assert received["type"] == "message"
    assert received["handle"] == "Claude"
    assert received["text"] == "hello channel"

    history = client.get(f"/channels/{channel_id}/history").json()
    assert [m["text"] for m in history] == ["hello channel"]


def test_ws_join_announced_to_others(client):
    channel_id = _create_channel(client)
    key1 = _join(client, channel_id, handle="Claude")
    key2 = _join(client, channel_id, handle="GPT")

    with (
        client.websocket_connect(f"/channels/{channel_id}/ws?key={key1}") as ws1,
        client.websocket_connect(f"/channels/{channel_id}/ws?key={key2}"),
    ):
        announcement = ws1.receive_json()

    assert announcement["type"] == "system"
    assert announcement["text"] == "GPT joined the channel"


def test_ws_message_reaches_tts_and_mixer(client, fake_tts, fake_mixer):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)

    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        ws.send_json({"type": "message", "text": "speak this"})
        ws.receive_json()
        assert _wait_for(lambda: fake_mixer.enqueued), "TTS worker never enqueued audio"

    assert [text for text, _voice in fake_tts.calls] == ["speak this"]
    assert fake_mixer.enqueued == [(FAKE_AUDIO, "Claude")]


# ---------------------------------------------------------------------------
# Spectator audio WebSocket
# ---------------------------------------------------------------------------


def test_spectator_unknown_channel_rejected(client):
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/channels/nonexistent/audio"),
    ):
        pass
    assert exc_info.value.code == 4004


def test_spectator_receives_audio(client, fake_mixer):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)

    with (
        client.websocket_connect(f"/channels/{channel_id}/audio") as spectator,
        client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws,
    ):
        ws.send_json({"type": "message", "text": "listen to this"})
        ws.receive_json()
        audio_bytes = spectator.receive_bytes()

    assert audio_bytes == FAKE_AUDIO


def test_find_or_create_channel_reuses_name(client):
    from concurrent.futures import ThreadPoolExecutor

    def create(_):
        return client.post(
            "/channels",
            json={
                "name": "same-repo",
                "reuse_existing": True,
            },
        ).json()["channel_id"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(create, range(8)))
    assert len(set(ids)) == 1
    assert len(client.get("/channels").json()) == 1


# ---------------------------------------------------------------------------
# IRC-style commands
# ---------------------------------------------------------------------------


def _command(client, channel_id, key_id, name, args=""):
    return client.post(
        f"/channels/{channel_id}/command",
        json={"key_id": key_id, "name": name, "args": args},
    )


def test_command_unknown_is_400(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)
    resp = _command(client, channel_id, key_id, "kick", "GPT")
    assert resp.status_code == 400
    assert resp.json() == {"error": "unknown command: kick"}


def test_command_bad_key_is_404(client):
    channel_id = _create_channel(client)
    other = _create_channel(client, "Other")
    other_key = _join(client, other, handle="Eve")
    assert _command(client, channel_id, "bogus", "names").status_code == 404
    assert _command(client, channel_id, other_key, "names").status_code == 404
    assert _command(client, "nonexistent", other_key, "names").status_code == 404


def test_command_names(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id, handle="Claude")
    _join(client, channel_id, handle="GPT")

    resp = _command(client, channel_id, key_id, "names")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["name"] == "names"
    participants = {p["handle"]: p for p in body["result"]["participants"]}
    assert set(participants) == {"Claude", "GPT"}
    assert participants["GPT"]["state"] == "active"
    assert participants["GPT"]["doing"] is None


def test_command_whois(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id, handle="Claude")
    gpt = _join(client, channel_id, handle="GPT")
    client.post(f"/channels/{channel_id}/say", json={"key_id": gpt, "text": "last words"})

    resp = _command(client, channel_id, key_id, "whois", "gp")

    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["participant"]["handle"] == "GPT"
    assert result["last_message"]["text"] == "last words"
    assert result["last_message"]["kind"] == "message"

    miss = _command(client, channel_id, key_id, "whois", "nobody")
    assert miss.status_code == 404
    assert "nobody" in miss.json()["error"]


def test_command_me_broadcasts_action_and_is_spoken_with_handle(client, fake_tts, fake_mixer):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id, handle="Claude")

    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        resp = _command(client, channel_id, key_id, "me", "waves at everyone")
        assert resp.status_code == 200
        event = ws.receive_json()
        assert _wait_for(lambda: fake_mixer.enqueued), "action never reached TTS"

    assert resp.json()["result"]["message"]["kind"] == "action"
    assert event["type"] == "message"
    assert event["kind"] == "action"
    assert event["handle"] == "Claude"
    assert event["text"] == "waves at everyone"
    assert [text for text, _voice in fake_tts.calls] == ["Claude waves at everyone"]
    assert fake_mixer.enqueued == [(FAKE_AUDIO, "Claude")]


def test_command_notice_broadcasts_but_is_not_spoken(client, fake_tts, fake_mixer):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id, handle="Claude")

    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        resp = _command(client, channel_id, key_id, "notice", "deploy starting")
        assert resp.status_code == 200
        event = ws.receive_json()
        # A plain message after the notice proves the worker drained the queue
        # and skipped the notice rather than merely not having gotten to it.
        client.post(f"/channels/{channel_id}/say", json={"key_id": key_id, "text": "spoken"})
        ws.receive_json()
        assert _wait_for(lambda: fake_mixer.enqueued), "follow-up message never reached TTS"

    assert event["type"] == "message"
    assert event["kind"] == "notice"
    assert event["text"] == "deploy starting"
    assert [text for text, _voice in fake_tts.calls] == ["spoken"]
    assert len(fake_mixer.enqueued) == 1


def test_history_returns_kind(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)
    client.post(f"/channels/{channel_id}/say", json={"key_id": key_id, "text": "plain"})
    _command(client, channel_id, key_id, "me", "acts")
    _command(client, channel_id, key_id, "notice", "notes")

    history = client.get(f"/channels/{channel_id}/history").json()

    assert [(m["text"], m["kind"]) for m in history] == [
        ("plain", "message"),
        ("acts", "action"),
        ("notes", "notice"),
    ]


def test_plain_say_broadcast_carries_default_kind(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)
    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        client.post(f"/channels/{channel_id}/say", json={"key_id": key_id, "text": "hi"})
        assert ws.receive_json()["kind"] == "message"
        ws.send_json({"type": "message", "text": "again"})
        assert ws.receive_json()["kind"] == "message"


def test_command_topic(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id, handle="Claude")

    empty = _command(client, channel_id, key_id, "topic")
    assert empty.json()["result"] == {"topic": None, "topic_set_by": None}

    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        resp = _command(client, channel_id, key_id, "topic", "voice channels")
        event = ws.receive_json()
        # Reading the topic back must not broadcast.
        read = _command(client, channel_id, key_id, "topic")
        ws.send_json({"type": "message", "text": "ping"})
        assert ws.receive_json()["text"] == "ping"

    assert resp.json()["result"] == {"topic": "voice channels", "topic_set_by": "Claude"}
    assert event == {"type": "topic", "topic": "voice channels", "handle": "Claude"}
    assert read.json()["result"]["topic"] == "voice channels"
    detail = client.get(f"/channels/{channel_id}").json()
    assert detail["topic"] == "voice channels"
    assert detail["topic_set_by"] == "Claude"


def test_command_away_and_back(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id, handle="Claude")

    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        away = _command(client, channel_id, key_id, "away", "lunch")
        away_event = ws.receive_json()
        back = _command(client, channel_id, key_id, "back")
        back_event = ws.receive_json()

    assert away.json()["result"]["participant"]["state"] == "away"
    assert away.json()["result"]["participant"]["away_reason"] == "lunch"
    assert away_event == {
        "type": "presence",
        "handle": "Claude",
        "state": "away",
        "reason": "lunch",
    }
    assert back.json()["result"]["participant"]["state"] == "active"
    assert back_event == {"type": "presence", "handle": "Claude", "state": "active"}

    names = _command(client, channel_id, key_id, "names").json()["result"]["participants"]
    assert names[0]["state"] == "active"


def test_ws_command_result_and_error(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id, handle="Claude")
    other = _join(client, channel_id, handle="GPT")

    with (
        client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws,
        client.websocket_connect(f"/channels/{channel_id}/ws?key={other}") as observer,
    ):
        ws.receive_json()  # GPT joined

        ws.send_json({"type": "command", "name": "away", "args": "thinking"})
        reply = ws.receive_json()
        assert reply["type"] == "result"
        assert reply["name"] == "away"
        assert reply["result"]["participant"]["state"] == "away"
        # The sender also gets the broadcast; the observer only the broadcast.
        assert ws.receive_json()["type"] == "presence"
        assert observer.receive_json() == {
            "type": "presence",
            "handle": "Claude",
            "state": "away",
            "reason": "thinking",
        }

        ws.send_json({"type": "command", "name": "names"})
        reply = ws.receive_json()
        assert reply["type"] == "result"
        assert {p["handle"] for p in reply["result"]["participants"]} == {"Claude", "GPT"}

        ws.send_json({"type": "command", "name": "bogus", "args": ""})
        assert ws.receive_json() == {"type": "error", "error": "unknown command: bogus"}

        ws.send_json({"type": "command", "name": "whois", "args": "zed"})
        error = ws.receive_json()
        assert error["type"] == "error"
        assert "zed" in error["error"]
