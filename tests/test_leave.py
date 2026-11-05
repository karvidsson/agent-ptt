"""Leaving a channel is persistent: a restart must not bring the participant back."""

from agent_ptt import channel as ch
from agent_ptt.db import SessionLocal
from agent_ptt.models import MentionReceiverDB, ParticipantKeyDB, RevokedParticipantDB


def room(client, name="room"):
    return client.post("/channels", json={"name": name}).json()["channel_id"]


def join(client, cid, handle="Ada", **extra):
    response = client.post(
        f"/channels/{cid}/join", json={"handle": handle, "voice_id": "alba", **extra}
    )
    assert response.status_code == 200
    return response.json()


def leave(client, cid, key_id):
    return client.post(f"/channels/{cid}/leave", json={"key_id": key_id})


def restart():
    ch._channels.clear()
    ch._message_queues.clear()
    with SessionLocal() as db:
        ch.restore_channels(db)


def test_left_participant_stays_gone_after_restore(client):
    cid = room(client)
    gone = join(client, cid, session_id="s-gone", agent="Claude", receive_mentions=True)
    stays = join(client, cid, "Bob")
    with SessionLocal() as db:
        assert db.query(MentionReceiverDB).filter_by(key_id=gone["key_id"]).count() == 1

    assert leave(client, cid, gone["key_id"]).status_code == 200
    restart()

    roster = client.get(f"/channels/{cid}").json()["participants"]
    assert [p["handle"] for p in roster.values()] == ["Bob"]
    assert ch.get_participant(stays["key_id"]) is not None
    say = client.post(f"/channels/{cid}/say", json={"key_id": gone["key_id"], "text": "hi"})
    assert say.status_code == 404
    command = client.post(
        f"/channels/{cid}/command", json={"key_id": gone["key_id"], "name": "names"}
    )
    assert command.status_code == 404
    assert leave(client, cid, gone["key_id"]).status_code == 404
    with SessionLocal() as db:
        assert db.get(ParticipantKeyDB, gone["key_id"]).channel_id is None
        assert db.query(MentionReceiverDB).filter_by(key_id=gone["key_id"]).count() == 0
        # Leaving is not a kick: nothing blocks a later explicit join.
        assert db.get(RevokedParticipantDB, gone["key_id"]) is None


def test_rejoin_after_leave_works_and_survives_restore(client):
    cid = room(client)
    first = join(client, cid)
    assert leave(client, cid, first["key_id"]).status_code == 200
    again = join(client, cid)
    assert again["key_id"] != first["key_id"]
    restart()
    assert ch.get_participant(again["key_id"]) is not None
    assert ch.get_participant(first["key_id"]) is None
    say = client.post(f"/channels/{cid}/say", json={"key_id": again["key_id"], "text": "back"})
    assert say.status_code == 200


def test_session_identity_after_leave_gets_a_fresh_key_with_same_identity(client):
    cid = room(client)
    identity = {"session_id": "s-1", "agent": "Claude", "receive_mentions": True}
    first = join(client, cid, **identity)
    # Before leaving, a repeated session join reuses the participant.
    assert join(client, cid, **identity)["key_id"] == first["key_id"]

    assert leave(client, cid, first["key_id"]).status_code == 200
    again = join(client, cid, **identity)
    assert again["key_id"] != first["key_id"]
    assert (again["handle"], again["voice_id"]) == (first["handle"], first["voice_id"])
    assert again["mention_id"] == first["mention_id"]
    # And the new participant is reused in turn, also across a restart.
    restart()
    assert join(client, cid, **identity)["key_id"] == again["key_id"]
    roster = client.get(f"/channels/{cid}").json()["participants"]
    assert len(roster) == 1
