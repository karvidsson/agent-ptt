from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from agent_ptt import channel as ch
from agent_ptt import handles
from agent_ptt.db import SessionLocal
from agent_ptt.models import HandleClaimDB, SessionIdentityDB, VoiceProfile
from agent_ptt.session_identity import NAMES, get_session_identity
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


# --- The handle registry decides which names are free ------------------------


def fill_dormant(db, count=40):
    """Hold the first ``count`` plain names with sessions that are in no channel."""
    for index, name in enumerate(NAMES[:count]):
        db.add(
            SessionIdentityDB(
                session_id=f"old-{index}", agent="Codex", handle=name, voice_id="builder"
            )
        )
    db.commit()


def occupy(db, handles):
    """Put plain-handle participants into a channel."""
    channel = ch.create_channel("busy", db=db)
    for handle in handles:
        ch.join_channel(channel.channel_id, handle, "alba", db=db)
    return channel


def test_all_plain_names_busy_gives_a_numbered_name(db_session):
    seed(db_session)
    occupy(db_session, NAMES)
    fresh = get_session_identity("new", "Codex", db_session)
    assert fresh.handle.endswith("2")
    assert fresh.handle.removesuffix("2") in NAMES


def test_plain_handle_participant_blocks_its_name(db_session):
    seed(db_session)
    fill_dormant(db_session)
    occupy(db_session, [name for name in NAMES if name != "Milo"])
    with SessionLocal() as db:
        # Only Milo is free of participants, but a plain "Milo" now joins.
        occupy(db, ["Milo"])
    fresh = get_session_identity("new", "Codex", db_session)
    assert fresh.handle.endswith("2")


def test_deleted_channel_does_not_keep_names_busy(db_session):
    seed(db_session)
    channel = occupy(db_session, NAMES)
    ch.delete_channel(channel.channel_id, db=db_session)
    assert get_session_identity("new", "Codex", db_session).handle in NAMES


def test_voices_are_balanced_over_sessions_in_channels(db_session):
    seed(db_session)
    fill_dormant(db_session)  # 40 remembered sessions use "builder", none in a channel
    talking = ["Zed", "Yan", "Xia"]
    for handle in talking:
        db_session.add(
            SessionIdentityDB(session_id=handle, agent="Codex", handle=handle, voice_id="ruler")
        )
    occupy(db_session, talking)
    # In channels: ruler 3, builder 0 → the new session gets builder, although
    # far more remembered sessions use it.
    assert get_session_identity("new", "Codex", db_session).voice_id == "builder"


def hold(db, names, *, age=timedelta(0), prefix="old"):
    """Claims on ``names`` by other sessions, last seen ``age`` ago."""
    when = datetime.now(UTC) - age
    for index, name in enumerate(names):
        db.add(
            SessionIdentityDB(
                session_id=f"{prefix}-{index}", agent="Codex", handle=name, voice_id="builder"
            )
        )
        claim = handles.take(
            db,
            name,
            handles.Owner(
                handles.session_owner("Codex", f"{prefix}-{index}"),
                harness="Codex",
                session_id=f"{prefix}-{index}",
            ),
        )
        claim.last_seen = when
    db.commit()


def test_expired_names_are_reused_before_numbered_ones(db_session):
    seed(db_session)
    hold(db_session, NAMES, age=timedelta(hours=25))
    fresh = get_session_identity("new", "Codex", db_session)
    assert fresh.handle in NAMES
    held = db_session.scalars(select(SessionIdentityDB.handle)).all()
    assert len(held) == len(set(held)) == len(NAMES)


def test_name_that_expired_longest_ago_is_reused(db_session):
    seed(db_session)
    hold(db_session, NAMES, age=timedelta(hours=25))
    claim = db_session.get(HandleClaimDB, "ivy")
    claim.last_seen = datetime.now(UTC) - timedelta(days=9)
    db_session.commit()
    assert get_session_identity("new", "Codex", db_session).handle == "Ivy"


def test_never_claimed_names_beat_expired_ones(db_session):
    seed(db_session)
    hold(db_session, NAMES[:-1], age=timedelta(hours=25))
    assert get_session_identity("new", "Codex", db_session).handle == NAMES[-1]


def test_live_claims_push_new_sessions_to_the_next_tier(db_session):
    seed(db_session)
    hold(db_session, NAMES, age=timedelta(hours=23))
    fresh = get_session_identity("new", "Codex", db_session)
    assert fresh.handle.endswith("2") and fresh.handle.removesuffix("2") in NAMES
    hold(db_session, [f"{n}2" for n in NAMES if f"{n}2" != fresh.handle], prefix="two")
    third = get_session_identity("newer", "Codex", db_session)
    assert third.handle.endswith("3") and third.handle.removesuffix("3") in NAMES


def test_session_keeps_its_name_out_of_every_channel(db_session):
    seed(db_session)
    hold(db_session, NAMES[:-1])
    mine = get_session_identity("mine", "Codex", db_session)
    assert mine.handle == NAMES[-1]
    # In no channel, but its claim is live: nobody else gets the name.
    other = get_session_identity("other", "Codex", db_session)
    assert other.handle.endswith("2")
    again = get_session_identity("mine", "Codex", db_session)
    assert (again.handle, again.voice_id) == (NAMES[-1], mine.voice_id)


def test_leaving_a_channel_does_not_free_the_name(client, db_session):
    seed(db_session)
    hold(db_session, NAMES[:-1])
    cid = client.post("/channels", json={"name": "room"}).json()["channel_id"]
    payload = {"handle": "Claude · project", "agent": "Claude"}
    first = client.post(f"/channels/{cid}/join", json={**payload, "session_id": "a"}).json()
    assert first["handle"] == NAMES[-1]
    assert first["handle_claim"] == {
        "handle": NAMES[-1],
        "pinned": False,
        "expires_after_hours": 24,
    }
    left = client.post(f"/channels/{cid}/leave", json={"key_id": first["key_id"]})
    assert left.status_code == 200
    later = client.post(f"/channels/{cid}/join", json={**payload, "session_id": "c"}).json()
    assert later["handle"].endswith("2")
    back = client.post(f"/channels/{cid}/join", json={**payload, "session_id": "a"}).json()
    assert back["handle"] == NAMES[-1]
