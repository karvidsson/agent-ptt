from concurrent.futures import ThreadPoolExecutor

from agent_ptt.db import SessionLocal
from agent_ptt.models import VoiceProfile
from agent_ptt.session_identity import get_session_identity
from agent_ptt.voices import delete_voice_profile, save_voice_profile


def seed(db):
    for voice_id in ("builder", "ruler", "auto-old-project"):
        save_voice_profile(VoiceProfile(voice_id=voice_id, display_name=voice_id), db)


def test_session_pair_survives_new_db_connection(db_session):
    seed(db_session)
    first = get_session_identity("s1", "Codex", db_session)
    with SessionLocal() as db:
        resumed = get_session_identity("s1", "Codex", db)
        assert (resumed.handle, resumed.voice_id) == (first.handle, first.voice_id)


def test_sessions_get_distinct_names_and_spread_existing_voices(db_session):
    seed(db_session)
    first = get_session_identity("s1", "Codex", db_session)
    second = get_session_identity("s2", "Codex", db_session)
    third = get_session_identity("s1", "Claude", db_session)
    assert len({first.handle, second.handle, third.handle}) == 3
    assert {first.voice_id, second.voice_id} == {"builder", "ruler"}
    assert third.voice_id in {"builder", "ruler"}


def test_deleted_voice_is_replaced_without_changing_name(db_session):
    seed(db_session)
    identity = get_session_identity("s1", "Codex", db_session)
    original_name, original_voice = identity.handle, identity.voice_id
    delete_voice_profile(original_voice, db_session)
    repaired = get_session_identity("s1", "Codex", db_session)
    assert repaired.handle == original_name
    assert repaired.voice_id in {"builder", "ruler"} - {original_voice}


def test_concurrent_hooks_share_identity(db_session):
    seed(db_session)

    def join(_):
        with SessionLocal() as db:
            identity = get_session_identity("s1", "Codex", db)
            return identity.handle, identity.voice_id

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert len(set(executor.map(join, range(8)))) == 1


def test_empty_library_gets_stable_fallback(db_session):
    first = get_session_identity("s1", "Codex", db_session)
    assert first.voice_id == f"auto-{first.handle.lower()}"
    assert get_session_identity("s1", "Codex", db_session).voice_id == first.voice_id


def test_session_join_reuses_participant_and_follows_channel_changes(client, db_session):
    seed(db_session)
    first_channel = client.post("/channels", json={"name": "first"}).json()["channel_id"]
    second_channel = client.post("/channels", json={"name": "second"}).json()["channel_id"]
    payload = {"handle": "Codex · project", "session_id": "s1", "agent": "Codex"}
    first = client.post(f"/channels/{first_channel}/join", json=payload).json()
    repeat = client.post(f"/channels/{first_channel}/join", json=payload).json()
    moved = client.post(f"/channels/{second_channel}/join", json=payload).json()
    assert first["handle"] != payload["handle"]
    assert first["key_id"] == repeat["key_id"]
    assert (first["handle"], first["voice_id"]) == (moved["handle"], moved["voice_id"])
    assert first["voice_id"] in {"builder", "ruler"}
