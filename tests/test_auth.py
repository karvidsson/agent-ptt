"""Perimeter API-key auth (agent_ptt/auth.py) on REST and WebSocket routes."""

import pytest
from starlette.websockets import WebSocketDisconnect

from agent_ptt.auth import API_KEY_ENV, WS_CLOSE_UNAUTHORIZED

KEY = "s3cret-test-key"
CONFIRM = {"X-Confirm-Delete-All": "yes"}


@pytest.fixture
def auth_enabled(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, KEY)


@pytest.fixture
def auth_disabled(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)


def _bearer(key=KEY) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _create_channel(client, headers=None) -> str:
    resp = client.post("/channels", json={"name": "Auth Room"}, headers=headers or {})
    assert resp.status_code == 200, resp.text
    return resp.json()["channel_id"]


def _join(client, channel_id, headers=None) -> str:
    resp = client.post(
        f"/channels/{channel_id}/join",
        json={"handle": "Claude", "voice_id": "alba"},
        headers=headers or {},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["key_id"]


# ---------------------------------------------------------------------------
# Auth disabled — behaviour unchanged
# ---------------------------------------------------------------------------


def test_disabled_rest_works_without_key(client, auth_disabled):
    channel_id = _create_channel(client)
    assert client.get("/channels").status_code == 200
    assert client.get(f"/channels/{channel_id}").status_code == 200
    assert client.get("/voices/profiles").status_code == 200


def test_disabled_empty_env_value_counts_as_disabled(client, monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "   ")
    assert client.get("/channels").status_code == 200


def test_disabled_websockets_connect_without_key(client, auth_disabled):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)
    with (
        client.websocket_connect(f"/channels/{channel_id}/audio"),
        client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}"),
    ):
        pass


def test_disabled_delete_all_still_needs_confirmation(client, auth_disabled):
    _create_channel(client)
    denied = client.delete("/channels")
    assert denied.status_code == 403
    assert client.get("/channels").json() != []

    ok = client.delete("/channels", headers=CONFIRM)
    assert ok.status_code == 200
    assert ok.json() == {"deleted": 1}


# ---------------------------------------------------------------------------
# Auth enabled — REST
# ---------------------------------------------------------------------------


def test_enabled_rejects_missing_key(client, auth_enabled):
    resp = client.get("/channels")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid or missing API key"}
    assert client.post("/channels", json={"name": "x"}).status_code == 401
    assert client.get("/voices/profiles").status_code == 401


def test_enabled_accepts_bearer(client, auth_enabled):
    channel_id = _create_channel(client, _bearer())
    assert client.get("/channels", headers=_bearer()).status_code == 200
    assert client.get(f"/channels/{channel_id}", headers=_bearer()).status_code == 200


def test_enabled_accepts_x_api_key(client, auth_enabled):
    assert client.get("/channels", headers={"X-API-Key": KEY}).status_code == 200


def test_enabled_rejects_wrong_key(client, auth_enabled):
    assert client.get("/channels", headers=_bearer("nope")).status_code == 401
    assert client.get("/channels", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/channels", headers={"Authorization": f"Basic {KEY}"}).status_code == 401


def test_enabled_root_and_ui_stay_public(client, auth_enabled):
    assert client.get("/", follow_redirects=False).status_code == 307
    assert client.get("/ui/").status_code == 200


def test_enabled_delete_all_needs_key_and_confirmation(client, auth_enabled):
    _create_channel(client, _bearer())
    assert client.delete("/channels").status_code == 401
    assert client.delete("/channels", headers=CONFIRM).status_code == 401
    assert client.delete("/channels", headers=_bearer()).status_code == 403
    ok = client.delete("/channels", headers={**_bearer(), **CONFIRM})
    assert ok.status_code == 200
    assert ok.json() == {"deleted": 1}


def test_enabled_delete_one_needs_only_key(client, auth_enabled):
    channel_id = _create_channel(client, _bearer())
    assert client.delete(f"/channels/{channel_id}").status_code == 401
    assert client.delete(f"/channels/{channel_id}", headers=_bearer()).status_code == 200


# ---------------------------------------------------------------------------
# Auth enabled — WebSockets
# ---------------------------------------------------------------------------


def test_enabled_agent_ws_rejected_without_key(client, auth_enabled):
    channel_id = _create_channel(client, _bearer())
    key_id = _join(client, channel_id, _bearer())
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}"),
    ):
        pass
    assert exc_info.value.code == WS_CLOSE_UNAUTHORIZED


def test_enabled_agent_ws_accepts_api_key_query_param(client, auth_enabled):
    channel_id = _create_channel(client, _bearer())
    key_id = _join(client, channel_id, _bearer())
    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}&api_key={KEY}") as ws:
        ws.send_json({"type": "message", "text": "hello"})
        assert ws.receive_json()["text"] == "hello"


def test_enabled_agent_ws_accepts_header(client, auth_enabled):
    channel_id = _create_channel(client, _bearer())
    key_id = _join(client, channel_id, _bearer())
    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}", headers=_bearer()):
        pass


def test_enabled_agent_ws_rejects_wrong_query_key(client, auth_enabled):
    channel_id = _create_channel(client, _bearer())
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(f"/channels/{channel_id}/ws?api_key=nope"),
    ):
        pass
    assert exc_info.value.code == WS_CLOSE_UNAUTHORIZED


def test_enabled_audio_ws_rejected_without_key(client, auth_enabled):
    channel_id = _create_channel(client, _bearer())
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(f"/channels/{channel_id}/audio"),
    ):
        pass
    assert exc_info.value.code == WS_CLOSE_UNAUTHORIZED


def test_enabled_audio_ws_accepts_x_api_key_header(client, auth_enabled):
    channel_id = _create_channel(client, _bearer())
    with client.websocket_connect(f"/channels/{channel_id}/audio", headers={"X-API-Key": KEY}):
        pass


def test_enabled_auth_checked_before_channel_lookup(client, auth_enabled):
    """Unauthenticated callers should not learn whether a channel exists."""
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/channels/nonexistent/audio"),
    ):
        pass
    assert exc_info.value.code == WS_CLOSE_UNAUTHORIZED
