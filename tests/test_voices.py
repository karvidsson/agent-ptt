"""Voice profile persistence — CRUD and TTS-pipeline resolution."""

from agent_ptt.models import VoiceProfile
from agent_ptt.voices import (
    delete_voice_profile,
    get_voice_profile,
    list_voice_profiles,
    save_voice_profile,
)
from tests.test_api import _create_channel, _join, _wait_for


def _profile(voice_id="narrator", engine="pocket-tts", **settings) -> VoiceProfile:
    return VoiceProfile(
        voice_id=voice_id,
        display_name=voice_id.title(),
        engine=engine,
        settings=settings or {"voice": "marius"},
    )


def test_save_and_get_roundtrip(db_session):
    saved = save_voice_profile(_profile(), db_session)
    loaded = get_voice_profile("narrator", db_session)
    assert loaded is not None
    assert loaded.voice_id == saved.voice_id
    assert loaded.engine == saved.engine
    assert loaded.settings == saved.settings


def test_get_missing_returns_none(db_session):
    assert get_voice_profile("nonexistent", db_session) is None


def test_save_is_upsert(db_session):
    save_voice_profile(_profile(), db_session)
    updated = _profile()
    updated.display_name = "Epic Narrator"
    updated.settings = {"voice": "en-GB-RyanNeural"}
    save_voice_profile(updated, db_session)

    loaded = get_voice_profile("narrator", db_session)
    assert loaded.display_name == "Epic Narrator"
    assert loaded.settings == {"voice": "en-GB-RyanNeural"}
    assert len(list_voice_profiles(db_session)) == 1


def test_list_with_engine_filter(db_session):
    save_voice_profile(_profile("a", engine="pocket-tts"), db_session)
    save_voice_profile(_profile("b", engine="omnivoice"), db_session)

    assert {p.voice_id for p in list_voice_profiles(db_session)} == {"a", "b"}
    assert [p.voice_id for p in list_voice_profiles(db_session, engine="omnivoice")] == ["b"]


def test_delete(db_session):
    save_voice_profile(_profile(), db_session)
    assert delete_voice_profile("narrator", db_session) is True
    assert get_voice_profile("narrator", db_session) is None
    assert delete_voice_profile("narrator", db_session) is False


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


def _profile_payload(voice_id="narrator", engine="pocket-tts"):
    return {
        "voice_id": voice_id,
        "display_name": voice_id.title(),
        "engine": engine,
        "settings": {"voice": "marius"},
    }


def test_api_save_and_get_profile(client, db_session):
    resp = client.post("/voices/profiles", json=_profile_payload())
    assert resp.status_code == 200
    assert resp.json()["voice_id"] == "narrator"

    resp = client.get("/voices/profiles/narrator")
    assert resp.status_code == 200
    assert resp.json()["settings"] == {"voice": "marius"}


def test_api_get_missing_profile(client, db_session):
    assert client.get("/voices/profiles/nonexistent").status_code == 404


def test_api_save_is_upsert(client, db_session):
    client.post("/voices/profiles", json=_profile_payload())
    updated = _profile_payload()
    updated["display_name"] = "Epic Narrator"
    client.post("/voices/profiles", json=updated)

    profiles = client.get("/voices/profiles").json()
    assert len(profiles) == 1
    assert profiles[0]["display_name"] == "Epic Narrator"


def test_api_list_profiles_with_engine_filter(client, db_session):
    client.post("/voices/profiles", json=_profile_payload("a", engine="pocket-tts"))
    # Existing legacy rows remain queryable until startup migration runs.
    save_voice_profile(_profile("b", engine="omnivoice"), db_session)

    assert len(client.get("/voices/profiles").json()) == 2
    filtered = client.get("/voices/profiles", params={"engine": "omnivoice"}).json()
    assert [p["voice_id"] for p in filtered] == ["b"]


def test_api_delete_profile(client, db_session):
    client.post("/voices/profiles", json=_profile_payload())
    assert client.delete("/voices/profiles/narrator").status_code == 200
    assert client.get("/voices/profiles/narrator").status_code == 404
    assert client.delete("/voices/profiles/narrator").status_code == 404


# ---------------------------------------------------------------------------
# TTS pipeline resolution
# ---------------------------------------------------------------------------


def test_tts_worker_uses_stored_profile(client, db_session, fake_tts):
    """A message from a participant whose voice_id matches a stored profile
    must be synthesized with that profile's engine and settings."""
    save_voice_profile(
        _profile("designed-voice", engine="omnivoice", instruct="[gender:female][age:young]"),
        db_session,
    )
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id, voice_id="designed-voice")

    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        ws.send_json({"type": "message", "text": "use my designed voice"})
        ws.receive_json()
        assert _wait_for(lambda: fake_tts.calls)

    _text, voice = fake_tts.calls[0]
    assert voice.voice_id == "designed-voice"
    assert voice.engine == "omnivoice"
    assert voice.settings == {"instruct": "[gender:female][age:young]"}


def test_tts_worker_falls_back_to_raw_voice_id(client, fake_tts):
    """Without a stored profile, the raw voice_id keeps working as an pocket-tts voice."""
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id, voice_id="marius")

    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        ws.send_json({"type": "message", "text": "fallback voice"})
        ws.receive_json()
        assert _wait_for(lambda: fake_tts.calls)

    _text, voice = fake_tts.calls[0]
    assert voice.engine == "pocket-tts"
    assert voice.settings == {"voice": "marius"}
