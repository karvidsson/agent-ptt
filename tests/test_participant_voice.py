"""Changing a participant's voice from the web UI (PUT .../participants/{pid}/voice)."""

import time
from datetime import UTC, datetime, timedelta

from agent_ptt import channel as ch
from agent_ptt import handles
from agent_ptt.auth import API_KEY_ENV
from agent_ptt.db import SessionLocal
from agent_ptt.models import (
    HandleClaimDB,
    ParticipantKeyDB,
    PinnedVoiceDB,
    SessionIdentityDB,
)


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _channel(client, name="voices") -> str:
    return client.post("/channels", json={"name": name}).json()["channel_id"]


def _join(client, cid, **body) -> dict:
    resp = client.post(f"/channels/{cid}/join", json={"handle": "Claude", **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _set_voice(client, cid, pid, voice_id, headers=None):
    return client.put(
        f"/channels/{cid}/participants/{pid}/voice",
        json={"voice_id": voice_id},
        headers=headers or {},
    )


def test_catalog_voice_is_wrapped_in_a_user_profile_and_pinned(client):
    cid = _channel(client)
    joined = _join(client, cid)
    assert joined["voice_id"] == "auto-claude"

    resp = _set_voice(client, cid, joined["participant_id"], "fake-voice")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["voice"]["voice_id"] == "user-claude"
    assert body["voice"]["display_name"] == "Fake"
    assert body["voice"]["engine"] == "fake"
    assert body["voice_pin"] == {"handle": "claude", "voice_id": "user-claude", "source": "user"}
    assert body["participant"]["voice_id"] == "user-claude"
    assert joined["key_id"] not in resp.text

    pins = client.get("/voices/pinned").json()
    assert [(p["handle"], p["voice_id"], p["source"]) for p in pins] == [
        ("claude", "user-claude", "user")
    ]
    detail = client.get(f"/channels/{cid}/participants/{joined['participant_id']}").json()
    assert detail["voice"]["voice_id"] == "user-claude"
    assert detail["voice_pin"]["source"] == "user"
    roster = client.get(f"/channels/{cid}").json()["participants"]
    assert [p["voice_id"] for p in roster.values()] == ["user-claude"]


def test_saved_profile_is_pinned_as_is(client):
    cid = _channel(client)
    joined = _join(client, cid)
    client.post(
        "/voices/profiles",
        json={"voice_id": "narrator", "display_name": "Narrator", "settings": {"voice": "jean"}},
    )
    body = _set_voice(client, cid, joined["participant_id"], "narrator").json()
    assert body["voice"]["voice_id"] == "narrator"
    assert body["voice_pin"]["voice_id"] == "narrator"
    assert client.get("/voices/profiles/user-claude").status_code == 404


def test_unknown_voice_is_422_and_changes_nothing(client):
    cid = _channel(client)
    joined = _join(client, cid)
    resp = _set_voice(client, cid, joined["participant_id"], "no-such-voice")
    assert resp.status_code == 422
    assert ch.get_participant(joined["key_id"]).voice_id == "auto-claude"
    assert client.get("/voices/pinned").json()[0]["source"] == "hash"
    assert _set_voice(client, cid, joined["participant_id"], "").status_code == 422


def test_unknown_participant_is_404(client):
    cid = _channel(client)
    assert _set_voice(client, cid, "p_missing", "fake-voice").status_code == 404
    assert _set_voice(client, "no-channel", "p_missing", "fake-voice").status_code == 404


def test_needs_the_api_key_when_configured(client, monkeypatch):
    cid = _channel(client)
    joined = _join(client, cid)
    monkeypatch.setenv(API_KEY_ENV, "s3cret")
    assert _set_voice(client, cid, joined["participant_id"], "fake-voice").status_code == 401
    ok = _set_voice(
        client, cid, joined["participant_id"], "fake-voice", {"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200


def test_change_is_persisted_and_survives_restore(client):
    cid = _channel(client)
    joined = _join(client, cid)
    _set_voice(client, cid, joined["participant_id"], "fake-voice")

    with SessionLocal() as db:
        assert db.get(ParticipantKeyDB, joined["key_id"]).voice_id == "user-claude"
        ch._channels.clear()
        ch._message_queues.clear()
        ch.restore_channels(db)
    assert ch.get_participant(joined["key_id"]).voice_id == "user-claude"


def test_next_message_is_spoken_in_the_new_voice(client, fake_tts, fake_mixer):
    cid = _channel(client)
    joined = _join(client, cid)
    _set_voice(client, cid, joined["participant_id"], "fake-voice")

    client.post(f"/channels/{cid}/say", json={"key_id": joined["key_id"], "text": "new voice"})
    assert _wait_for(lambda: fake_mixer.enqueued)
    [(text, voice)] = fake_tts.calls
    assert text == "new voice"
    assert voice.voice_id == "user-claude"
    assert voice.settings == {}  # the fake catalog voice's settings


def test_later_join_without_a_voice_keeps_the_user_pin(client):
    cid = _channel(client)
    joined = _join(client, cid)
    _set_voice(client, cid, joined["participant_id"], "fake-voice")
    client.post(f"/channels/{cid}/leave", json={"key_id": joined["key_id"]})

    rejoined = _join(client, _channel(client, "elsewhere"))
    assert rejoined["voice_id"] == "user-claude"
    assert "designed_voice" not in rejoined or rejoined["designed_voice"]["voice_id"] == (
        "user-claude"
    )
    with SessionLocal() as db:
        assert db.get(PinnedVoiceDB, "claude").source == "user"
    # An explicit voice is used for that join only; the pin stays.
    explicit = _join(client, cid, voice_id="alba")
    assert explicit["voice_id"] == "alba"
    assert client.get("/voices/pinned").json()[0]["voice_id"] == "user-claude"


def test_redesign_replaces_a_user_pin(client):
    cid = _channel(client)
    joined = _join(client, cid)
    _set_voice(client, cid, joined["participant_id"], "fake-voice")
    assert client.post("/voices/pinned/Claude/redesign").json()["voice_id"] == "auto-claude"
    assert client.get("/voices/pinned").json()[0]["source"] == "hash"


def test_session_join_keeps_the_new_voice_and_reuses_the_participant(client):
    cid = _channel(client)
    body = {"handle": "ignored", "session_id": "sess-1", "agent": "Claude"}
    first = client.post(f"/channels/{cid}/join", json=body).json()
    _set_voice(client, cid, first["participant_id"], "fake-voice")
    handle = first["handle"]

    again = client.post(f"/channels/{cid}/join", json=body).json()
    assert again["participant_id"] == first["participant_id"]
    assert again["voice_id"] == f"user-{handle.lower()}"
    with SessionLocal() as db:
        identity = db.get(SessionIdentityDB, ("sess-1", "Claude"))
        assert identity.voice_id == f"user-{handle.lower()}"

    # A new session pinning the same handle (AGENT_PTT_HANDLE) inherits it too,
    # once the old session has been quiet for 10 minutes (same harness and machine).
    with SessionLocal() as db:
        claim = db.get(HandleClaimDB, handle.casefold())
        claim.last_seen = datetime.now(UTC) - timedelta(minutes=15)
        db.commit()
    handles.reset_runtime_state()
    pinned = client.post(
        f"/channels/{_channel(client, 'other')}/join",
        json={"handle": handle, "session_id": "sess-2", "agent": "Claude", "pin_handle": True},
    ).json()
    assert pinned["voice_id"] == f"user-{handle.lower()}"


def test_every_connection_of_the_handle_changes_and_agents_are_told(client):
    cid = _channel(client)
    other = _channel(client, "second")
    a = _join(client, cid)
    b = _join(client, other)
    with client.websocket_connect(f"/channels/{other}/ws?key={b['key_id']}") as ws:
        _set_voice(client, cid, a["participant_id"], "fake-voice")
        event = ws.receive_json()
    assert event == {
        "type": "voice",
        "handle": "Claude",
        "participant_id": a["participant_id"],
        "voice_id": "user-claude",
        "display_name": "Fake",
    }
    assert ch.get_participant(b["key_id"]).voice_id == "user-claude"


def test_user_profiles_are_not_handed_to_new_sessions(client):
    cid = _channel(client)
    joined = _join(client, cid)
    _set_voice(client, cid, joined["participant_id"], "fake-voice")
    # Make the user-claude profile eligible engine-wise: a pocket-tts copy.
    client.post(
        "/voices/profiles",
        json={"voice_id": "user-claude", "display_name": "Fake", "settings": {"voice": "alba"}},
    )
    fresh = client.post(
        f"/channels/{cid}/join", json={"handle": "x", "session_id": "new", "agent": "Codex"}
    ).json()
    assert fresh["voice_id"] != "user-claude"
