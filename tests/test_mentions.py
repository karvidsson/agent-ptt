"""Routing, persistence, isolation, and handoff receipts for agent mentions."""

import pytest

from agent_ptt import channel
from agent_ptt.db import SessionLocal
from agent_ptt.models import MessageDB, MessageDeliveryDB


def room(client):
    return client.post("/channels", json={"name": "Mentions"}).json()["channel_id"]


def join(client, cid, session=None):
    data = {"handle": "Human", "voice_id": "fake"}
    if session:
        data.update(session_id=session, agent="Claude", receive_mentions=True)
    response = client.post(f"/channels/{cid}/join", json=data)
    assert response.status_code == 200
    return response.json()


def inbox(client, cid, agent):
    return client.get(f"/channels/{cid}/inbox", headers={"X-Participant-Key": agent["key_id"]})


def say(client, cid, user, text, **extra):
    return client.post(
        f"/channels/{cid}/say", json={"key_id": user["key_id"], "text": text, **extra}
    )


def token(agent):
    return f'@"{agent["handle"]}"'


def test_only_explicit_mentions_enter_target_inbox(client):
    cid = room(client)
    human, a, b = join(client, cid), join(client, cid, "a"), join(client, cid, "b")
    say(client, cid, human, f"Hello {a['handle']}")
    say(client, cid, human, f"mail user@{a['handle']}.com")
    say(client, cid, human, f"Example `{token(a)}`")
    assert inbox(client, cid, a).json() == []
    response = say(client, cid, human, f"{token(a)} check tests")
    assert response.status_code == 200
    msg = response.json()
    assert msg["deliveries"] == [
        {"recipient_id": a["mention_id"], "handle": a["handle"], "status": "pending"}
    ]
    assert [m["message_id"] for m in inbox(client, cid, a).json()] == [msg["message_id"]]
    assert inbox(client, cid, b).json() == []
    assert inbox(client, cid, a).json()  # Reading is not an ack.


def test_receipts_ack_is_scoped_and_idempotent(client):
    cid = room(client)
    human, a, b = join(client, cid), join(client, cid, "a"), join(client, cid, "b")
    mid = say(client, cid, human, token(a)).json()["message_id"]
    payload = {"message_ids": [mid]}
    path = f"/channels/{cid}/inbox/ack"
    assert (
        client.post(path, json=payload, headers={"X-Participant-Key": b["key_id"]}).status_code
        == 404
    )
    assert inbox(client, cid, a).json()
    for _ in range(2):
        assert (
            client.post(path, json=payload, headers={"X-Participant-Key": a["key_id"]}).status_code
            == 200
        )
    assert inbox(client, cid, a).json() == []
    history = client.get(f"/channels/{cid}/history").json()
    assert history[0]["deliveries"][0]["status"] == "delivered"


def test_pending_mentions_survive_restart_and_rejoin(client):
    cid = room(client)
    human, a = join(client, cid), join(client, cid, "a")
    mid = say(client, cid, human, token(a)).json()["message_id"]
    channel._channels.clear()
    channel._message_queues.clear()
    with SessionLocal() as db:
        channel.restore_channels(db)
    assert inbox(client, cid, a).json()[0]["message_id"] == mid
    channel.leave_channel(a["key_id"])
    again = join(client, cid, "a")
    assert again["mention_id"] == a["mention_id"]
    assert again["key_id"] != a["key_id"]
    assert inbox(client, cid, again).json()[0]["message_id"] == mid


def test_inbox_requires_channel_participant_and_server_auth(client, monkeypatch):
    cid, other = room(client), room(client)
    human, a = join(client, cid), join(client, cid, "a")
    assert inbox(client, cid, human).status_code == 403
    assert inbox(client, other, a).status_code == 403
    assert client.get(f"/channels/{cid}/inbox").status_code == 422
    monkeypatch.setenv("AGENT_PTT_API_KEY", "secret")
    assert inbox(client, cid, a).status_code == 401
    assert (
        client.get(
            f"/channels/{cid}/inbox",
            headers={"X-Participant-Key": a["key_id"], "Authorization": "Bearer secret"},
        ).status_code
        == 200
    )


def test_invalid_mention_does_not_persist_or_publish(client, fake_tts):
    cid = room(client)
    human = join(client, cid)
    assert say(client, cid, human, "@nobody hi").status_code == 422
    assert client.get(f"/channels/{cid}/history").json() == []
    with SessionLocal() as db:
        assert db.query(MessageDB).count() == 0
        assert db.query(MessageDeliveryDB).count() == 0
    assert fake_tts.calls == []


def test_multi_recipient_dedup_and_automatic_announcements_opt_out(client):
    cid = room(client)
    human, a, b = join(client, cid), join(client, cid, "a"), join(client, cid, "b")
    message = say(client, cid, human, f"{token(a)} {token(a)} {token(b)} check").json()
    assert len(message["deliveries"]) == 2
    say(client, cid, human, token(a), recipient_ids=[])
    assert len(inbox(client, cid, a).json()) == 1


def test_explicit_ids_reject_cross_channel_and_resolve_ambiguous_names(client):
    cid, other = room(client), room(client)
    human, a, b = join(client, cid), join(client, cid, "a"), join(client, cid, "b")
    remote = join(client, other, "remote")
    assert say(client, cid, human, "hi", recipient_ids=[remote["mention_id"]]).status_code == 422
    # The stable ID from autocomplete disambiguates otherwise identical display names.
    channel.get_participant(b["key_id"]).handle = a["handle"]
    assert say(client, cid, human, token(a)).status_code == 422
    assert say(client, cid, human, token(a), recipient_ids=[a["mention_id"]]).status_code == 200
    assert len(inbox(client, cid, a).json()) == 1
    assert inbox(client, cid, b).json() == []


def test_websocket_routes_and_returns_validation_errors(client):
    cid = room(client)
    human, a = join(client, cid), join(client, cid, "a")
    with client.websocket_connect(f"/channels/{cid}/ws?key={human['key_id']}") as ws:
        ws.send_json({"type": "message", "text": "@unknown hello"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "message", "text": token(a)})
        assert ws.receive_json()["deliveries"][0]["recipient_id"] == a["mention_id"]
    assert len(inbox(client, cid, a).json()) == 1


def test_inbox_batch_limit_and_message_length(client):
    cid = room(client)
    human, a = join(client, cid), join(client, cid, "a")
    for n in range(8):
        assert say(client, cid, human, f"{token(a)} {n}").status_code == 200
    assert len(inbox(client, cid, a).json()) == 6
    assert say(client, cid, human, token(a) + "x" * 8001).status_code == 422


def test_message_and_delivery_commit_before_publication(client, monkeypatch):
    cid = room(client)
    human, a = join(client, cid), join(client, cid, "a")
    from sqlalchemy.orm import Session

    def fail_commit(self):
        raise RuntimeError("disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="disk failure"):
            say(client, cid, human, token(a))
    assert channel.get_history(cid) == []
    with SessionLocal() as db:
        assert db.query(MessageDB).count() == 0
        assert db.query(MessageDeliveryDB).count() == 0


def test_join_advertises_policy_to_receivers_including_reconnect(client):
    from agent_ptt.mention_policy import mention_policy

    cid = client.post("/channels", json={"name": "policy"}).json()["channel_id"]
    assert "mention_policy" not in join(client, cid)
    first = join(client, cid, "policy-agent")
    second = join(client, cid, "policy-agent")
    assert first["mention_policy"] == second["mention_policy"] == mention_policy()
