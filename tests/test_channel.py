"""Channel manager — lifecycle, participants, messaging."""

import asyncio

import pytest

from agent_ptt import channel as ch
from agent_ptt.models import MessageDB


def test_create_and_get_channel():
    created = ch.create_channel("War Room")
    assert created.name == "War Room"
    assert ch.get_channel(created.channel_id) is created
    assert ch.get_message_queue(created.channel_id) is not None


def test_list_channels():
    assert ch.list_channels() == []
    a = ch.create_channel("A")
    b = ch.create_channel("B")
    ids = {c.channel_id for c in ch.list_channels()}
    assert ids == {a.channel_id, b.channel_id}


def test_delete_channel():
    created = ch.create_channel("Doomed")
    assert ch.delete_channel(created.channel_id) is True
    assert ch.get_channel(created.channel_id) is None
    assert ch.get_message_queue(created.channel_id) is None
    assert ch.delete_channel("nonexistent") is False


def test_join_channel():
    created = ch.create_channel("Room")
    key = ch.join_channel(created.channel_id, "Claude", "alba")
    assert key is not None
    assert key.handle == "Claude"
    assert key.channel_id == created.channel_id
    assert created.participants[key.key_id] is key


def test_join_unknown_channel():
    assert ch.join_channel("nonexistent", "Claude") is None


def test_join_persists_key(db_session):
    created = ch.create_channel("Room")
    key = ch.join_channel(created.channel_id, "Claude", db=db_session)
    from agent_ptt.models import ParticipantKeyDB

    row = db_session.get(ParticipantKeyDB, key.key_id)
    assert row is not None
    assert row.handle == "Claude"


def test_leave_channel_and_get_participant():
    created = ch.create_channel("Room")
    key = ch.join_channel(created.channel_id, "Claude")
    assert ch.get_participant(key.key_id) is key
    assert ch.leave_channel(key.key_id) is True
    assert ch.get_participant(key.key_id) is None
    assert ch.leave_channel(key.key_id) is False


async def test_send_message():
    created = ch.create_channel("Room")
    key = ch.join_channel(created.channel_id, "Claude")

    msg = await ch.send_message(key.key_id, "hello world")

    assert msg is not None
    assert msg.handle == "Claude"
    assert ch.get_history(created.channel_id) == [msg]
    queue = ch.get_message_queue(created.channel_id)
    assert queue.get_nowait() is msg


async def test_send_message_unknown_key():
    ch.create_channel("Room")
    assert await ch.send_message("nonexistent", "hello") is None


async def test_send_message_persists(db_session):
    created = ch.create_channel("Room")
    key = ch.join_channel(created.channel_id, "Claude")

    msg = await ch.send_message(key.key_id, "for the record", db=db_session)

    row = db_session.get(MessageDB, msg.message_id)
    assert row is not None
    assert row.text == "for the record"


def test_get_history_unknown_channel():
    assert ch.get_history("nonexistent") == []


# ---------------------------------------------------------------------------
# Surviving a restart
# ---------------------------------------------------------------------------


def test_restore_channels_brings_back_room_participants_and_transcript(db_session):
    created = ch.create_channel("War Room", db=db_session)
    key = ch.join_channel(created.channel_id, "Ada", "en-GB-SoniaNeural", db=db_session)
    asyncio.run(ch.send_message(key.key_id, "Tabs win and you know it.", db=db_session))

    # The server goes down: everything in memory is gone.
    ch._channels.clear()
    ch._message_queues.clear()
    assert ch.get_channel(created.channel_id) is None

    restored = ch.restore_channels(db_session)

    assert [c.channel_id for c in restored] == [created.channel_id]
    assert ch.get_channel(created.channel_id).name == "War Room"
    # The key Ada is still holding keeps working.
    assert ch.get_participant(key.key_id).handle == "Ada"
    assert [m.text for m in ch.get_history(created.channel_id)] == ["Tabs win and you know it."]
    assert ch.get_message_queue(created.channel_id) is not None


def test_restore_channels_is_idempotent(db_session):
    created = ch.create_channel("Room", db=db_session)
    assert ch.restore_channels(db_session) == []
    assert ch.get_channel(created.channel_id) is not None


def test_restored_transcript_timestamps_stay_utc(db_session):
    created = ch.create_channel("Room", db=db_session)
    key = ch.join_channel(created.channel_id, "Ada", db=db_session)
    asyncio.run(ch.send_message(key.key_id, "hello", db=db_session))

    ch._channels.clear()
    ch._message_queues.clear()
    ch.restore_channels(db_session)

    assert ch.get_history(created.channel_id)[0].timestamp.tzinfo is not None


def test_delete_channel_removes_the_persisted_row(db_session):
    created = ch.create_channel("Doomed", db=db_session)
    assert ch.delete_channel(created.channel_id, db=db_session) is True

    assert ch.restore_channels(db_session) == []


# ---------------------------------------------------------------------------
# IRC-style commands
# ---------------------------------------------------------------------------


def _room():
    created = ch.create_channel("Room")
    ada = ch.join_channel(created.channel_id, "Ada")
    bob = ch.join_channel(created.channel_id, "Bob")
    return created, ada, bob


async def test_run_command_unknown_is_400():
    created, ada, _bob = _room()
    with pytest.raises(ch.CommandError) as exc_info:
        await ch.run_command(created.channel_id, ada.key_id, "kick", "Bob")
    assert exc_info.value.status == 400
    assert exc_info.value.message == "unknown command: kick"


async def test_run_command_bad_key_is_404():
    created, _ada, _bob = _room()
    other = ch.create_channel("Elsewhere")
    stranger = ch.join_channel(other.channel_id, "Eve")
    for key_id in ("nonexistent", stranger.key_id):
        with pytest.raises(ch.CommandError) as exc_info:
            await ch.run_command(created.channel_id, key_id, "names")
        assert exc_info.value.status == 404


async def test_names_lists_participants_with_presence_defaults():
    created, ada, _bob = _room()
    result = await ch.run_command(created.channel_id, ada.key_id, "names")
    participants = {p["handle"]: p for p in result["participants"]}
    assert set(participants) == {"Ada", "Bob"}
    assert participants["Ada"]["state"] == "active"
    assert participants["Ada"]["away_reason"] is None
    assert participants["Ada"]["doing"] is None
    assert participants["Ada"]["since"] and participants["Ada"]["last_active"]


async def test_whois_prefix_match_is_case_insensitive_with_last_message():
    created, ada, bob = _room()
    await ch.send_message(bob.key_id, "first")
    last = await ch.send_message(bob.key_id, "second")
    result = await ch.run_command(created.channel_id, ada.key_id, "whois", "bO")
    assert result["participant"]["key_id"] == bob.key_id
    assert result["last_message"]["message_id"] == last.message_id


async def test_whois_without_messages_and_miss():
    created, ada, bob = _room()
    result = await ch.run_command(created.channel_id, ada.key_id, "whois", "Bob")
    assert result["participant"]["handle"] == "Bob"
    assert result["last_message"] is None
    with pytest.raises(ch.CommandError) as exc_info:
        await ch.run_command(created.channel_id, bob.key_id, "whois", "Zed")
    assert exc_info.value.status == 404


async def test_whois_prefers_exact_match_over_prefix():
    created = ch.create_channel("Room")
    ch.join_channel(created.channel_id, "Alexander")
    alex = ch.join_channel(created.channel_id, "Alex")
    result = await ch.run_command(created.channel_id, alex.key_id, "whois", "alex")
    assert result["participant"]["key_id"] == alex.key_id


async def test_me_posts_an_action_and_sets_doing(db_session):
    created, ada, _bob = _room()
    result = await ch.run_command(
        created.channel_id, ada.key_id, "me", "refactors the mixer", db=db_session
    )
    msg = result["message"]
    assert msg["kind"] == "action"
    assert msg["text"] == "refactors the mixer"
    assert msg["handle"] == "Ada"
    assert ada.doing == "refactors the mixer"
    assert db_session.get(MessageDB, msg["message_id"]).kind == "action"
    assert ch.get_history(created.channel_id)[0].kind == "action"
    assert ch.get_message_queue(created.channel_id).get_nowait().kind == "action"


async def test_notice_posts_a_notice_without_touching_doing():
    created, ada, _bob = _room()
    result = await ch.run_command(created.channel_id, ada.key_id, "notice", "build is green")
    assert result["message"]["kind"] == "notice"
    assert ada.doing is None
    assert ch.get_history(created.channel_id)[0].kind == "notice"


async def test_me_and_notice_need_text():
    created, ada, _bob = _room()
    for name in ("me", "notice"):
        with pytest.raises(ch.CommandError) as exc_info:
            await ch.run_command(created.channel_id, ada.key_id, name, "   ")
        assert exc_info.value.status == 400


async def test_topic_reads_and_sets():
    created, ada, bob = _room()
    assert await ch.run_command(created.channel_id, ada.key_id, "topic") == {
        "topic": None,
        "topic_set_by": None,
    }
    assert await ch.run_command(created.channel_id, bob.key_id, "topic", "ship it") == {
        "topic": "ship it",
        "topic_set_by": "Bob",
    }
    assert created.topic == "ship it"
    assert created.topic_set_by == "Bob"
    # Reading again doesn't change who set it.
    assert (await ch.run_command(created.channel_id, ada.key_id, "topic"))["topic_set_by"] == "Bob"


async def test_away_and_back_toggle_presence():
    created, ada, _bob = _room()
    before = ada.since
    result = await ch.run_command(created.channel_id, ada.key_id, "away", "lunch")
    assert result["participant"]["state"] == "away"
    assert result["participant"]["away_reason"] == "lunch"
    assert ada.state == "away"
    assert ada.since >= before

    result = await ch.run_command(created.channel_id, ada.key_id, "back")
    assert result["participant"]["state"] == "active"
    assert result["participant"]["away_reason"] is None
    assert ada.state == "active"


async def test_away_without_reason():
    created, ada, _bob = _room()
    result = await ch.run_command(created.channel_id, ada.key_id, "away")
    assert result["participant"]["state"] == "away"
    assert result["participant"]["away_reason"] is None
    assert ada.away_reason is None


async def test_messages_and_commands_bump_last_active():
    created, ada, _bob = _room()
    start = ada.last_active
    await ch.send_message(ada.key_id, "hi")
    after_message = ada.last_active
    assert after_message >= start
    await ch.run_command(created.channel_id, ada.key_id, "names")
    assert ada.last_active >= after_message


def test_restore_channels_keeps_message_kind(db_session):
    created = ch.create_channel("Room", db=db_session)
    key = ch.join_channel(created.channel_id, "Ada", db=db_session)
    asyncio.run(ch.run_command(created.channel_id, key.key_id, "me", "waves", db=db_session))
    asyncio.run(ch.send_message(key.key_id, "plain", db=db_session))

    ch._channels.clear()
    ch._message_queues.clear()
    ch.restore_channels(db_session)

    assert [m.kind for m in ch.get_history(created.channel_id)] == ["action", "message"]
