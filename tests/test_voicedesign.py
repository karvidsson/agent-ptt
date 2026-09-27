"""Deterministic voice design and handle pinning."""

from agent_ptt.models import PinnedVoiceDB
from agent_ptt.voicedesign import (
    POCKET_VOICES,
    design_voice,
    get_or_create_pinned_voice,
)
from tests.test_api import _create_channel, _wait_for


def test_design_voice_pocket_tts():
    profile = design_voice("Claude")
    assert profile.engine == "pocket-tts"
    assert profile.voice_id == "auto-claude"
    assert profile.settings["voice"] in POCKET_VOICES
    assert profile.settings == design_voice("claude").settings


def test_get_or_create_pins_and_reuses(db_session):
    first = get_or_create_pinned_voice("Claude", db_session)
    second = get_or_create_pinned_voice("claude", db_session)

    assert first.voice_id == second.voice_id
    assert first.settings == second.settings

    pin = db_session.get(PinnedVoiceDB, "claude")
    assert pin is not None
    assert pin.voice_id == first.voice_id
    assert pin.source == "hash"


def test_join_without_voice_auto_designs(client, db_session, fake_tts):
    channel_id = _create_channel(client)
    resp = client.post(f"/channels/{channel_id}/join", json={"handle": "Claude"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["voice_id"] == "auto-claude"
    assert body["designed_voice"]["settings"]["voice"] in POCKET_VOICES

    # Rejoining without a voice reuses the same pinned voice
    # (compare settings, not created_at — SQLite roundtrips drop the timezone)
    resp2 = client.post(f"/channels/{channel_id}/join", json={"handle": "Claude"})
    assert resp2.json()["voice_id"] == "auto-claude"
    assert resp2.json()["designed_voice"]["settings"] == body["designed_voice"]["settings"]


def test_join_with_explicit_voice_skips_design(client, db_session):
    channel_id = _create_channel(client)
    resp = client.post(
        f"/channels/{channel_id}/join",
        json={"handle": "Claude", "voice_id": "marius"},
    )
    body = resp.json()
    assert body["voice_id"] == "marius"
    assert "designed_voice" not in body
    assert db_session.get(PinnedVoiceDB, "claude") is None


def test_join_defaults_to_pocket_with_custom_backends(client, db_session, monkeypatch):
    monkeypatch.setattr("agent_ptt.tts.has_backend", lambda engine: True)
    channel_id = _create_channel(client)
    resp = client.post(f"/channels/{channel_id}/join", json={"handle": "Claude"})
    designed = resp.json()["designed_voice"]
    assert designed["engine"] == "pocket-tts"
    assert designed["settings"]["voice"] in POCKET_VOICES


def test_auto_designed_voice_flows_to_tts(client, db_session, fake_tts):
    """The whole point: joining without a voice must synthesize with the
    designed profile's settings, resolved from the DB."""
    channel_id = _create_channel(client)
    key_id = client.post(f"/channels/{channel_id}/join", json={"handle": "Claude"}).json()["key_id"]

    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        ws.send_json({"type": "message", "text": "auto voice"})
        ws.receive_json()
        assert _wait_for(lambda: fake_tts.calls)

    _text, voice = fake_tts.calls[0]
    assert voice.voice_id == "auto-claude"
    assert voice.settings == design_voice("Claude").settings
