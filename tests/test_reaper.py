"""Liveness, offline-after-restore presence, heartbeat and the idle reaper."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from agent_ptt import channel as ch
from agent_ptt import server
from agent_ptt.db import SessionLocal
from agent_ptt.models import MentionReceiverDB, ParticipantKeyDB


def room(client, name="room"):
    return client.post("/channels", json={"name": name}).json()["channel_id"]


def join(client, cid, handle="Ada", **extra):
    response = client.post(
        f"/channels/{cid}/join", json={"handle": handle, "voice_id": "alba", **extra}
    )
    assert response.status_code == 200
    return response.json()


def restart():
    ch._channels.clear()
    ch._message_queues.clear()
    with SessionLocal() as db:
        ch.restore_channels(db)


def participant(key_id):
    found = ch.get_participant(key_id)
    assert found is not None
    return found


def age(key_id, seconds):
    """Pretend the participant was last seen ``seconds`` ago."""
    participant(key_id).last_seen = datetime.now(UTC) - timedelta(seconds=seconds)


def settle(ws):
    """Round-trip a command so the server has finished registering the socket."""
    ws.send_json({"type": "command", "name": "names"})
    assert ws.receive_json()["type"] == "result"


def heartbeat(client, cid, key_id):
    return client.post(f"/channels/{cid}/heartbeat", json={"key_id": key_id})


# ---------------------------------------------------------------------------
# Restore: offline until seen
# ---------------------------------------------------------------------------


def test_restored_participants_are_offline_with_real_last_active(client):
    cid = room(client)
    talker = join(client, cid, "Ada")
    quiet = join(client, cid, "Bob")
    said = client.post(f"/channels/{cid}/say", json={"key_id": talker["key_id"], "text": "hi"})
    assert said.status_code == 200

    before = datetime.now(UTC)
    restart()

    roster = client.get(f"/channels/{cid}").json()["participants"]
    assert {p["state"] for p in roster.values()} == {"offline"}
    assert all("last_seen" not in p and "key_id" not in p for p in roster.values())
    ada, bob = participant(talker["key_id"]), participant(quiet["key_id"])
    assert ada.last_active == datetime.fromisoformat(said.json()["timestamp"])
    assert bob.last_active == datetime.fromisoformat(quiet["created_at"])
    assert ada.since >= before and ada.last_seen >= before


@pytest.mark.parametrize("action", ["say", "command", "heartbeat", "inbox", "join"])
def test_any_sign_of_life_brings_a_restored_participant_back(client, action):
    cid = room(client)
    identity = {"session_id": "s-1", "agent": "Claude", "receive_mentions": True}
    me = join(client, cid, **identity)
    restart()
    assert participant(me["key_id"]).state == "offline"
    restored_active = participant(me["key_id"]).last_active

    key = me["key_id"]
    if action == "say":
        response = client.post(f"/channels/{cid}/say", json={"key_id": key, "text": "back"})
    elif action == "command":
        response = client.post(f"/channels/{cid}/command", json={"key_id": key, "name": "names"})
    elif action == "heartbeat":
        response = heartbeat(client, cid, key)
    elif action == "inbox":
        response = client.get(f"/channels/{cid}/inbox", headers={"X-Participant-Key": key})
    else:
        response = client.post(
            f"/channels/{cid}/join", json={"handle": "ignored", "voice_id": "alba", **identity}
        )
        assert response.json()["key_id"] == key
    assert response.status_code == 200

    p = participant(key)
    assert p.state == "active"
    # Only saying or doing something counts as activity; polling doesn't.
    if action in ("say", "command"):
        assert p.last_active > restored_active
    else:
        assert p.last_active == restored_active


def test_back_from_offline_and_away_stays_away(client):
    cid = room(client)
    me = join(client, cid)
    client.post(f"/channels/{cid}/command", json={"key_id": me["key_id"], "name": "away"})
    heartbeat(client, cid, me["key_id"])
    assert participant(me["key_id"]).state == "away"

    restart()
    result = client.post(f"/channels/{cid}/command", json={"key_id": me["key_id"], "name": "back"})
    assert result.json()["result"]["participant"]["state"] == "active"


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_ok_unknown_and_kicked(client):
    cid, other = room(client), room(client, "other")
    me = join(client, cid)
    age(me["key_id"], 300)

    assert heartbeat(client, cid, me["key_id"]).json() == {"status": "ok"}
    assert (datetime.now(UTC) - participant(me["key_id"]).last_seen).total_seconds() < 5
    assert heartbeat(client, cid, "no-such-key").status_code == 404
    assert heartbeat(client, other, me["key_id"]).status_code == 404

    kicked = client.post(f"/channels/{cid}/kick/{me['participant_id']}")
    assert kicked.status_code == 200
    assert heartbeat(client, cid, me["key_id"]).status_code == 403


# ---------------------------------------------------------------------------
# Selecting idle participants
# ---------------------------------------------------------------------------


def test_idle_participants_picks_only_stale_keys_without_a_socket(client):
    cid = room(client)
    stale = join(client, cid, "Stale")
    fresh = join(client, cid, "Fresh")
    held = join(client, cid, "Held")
    age(stale["key_id"], 700)
    age(fresh["key_id"], 30)
    age(held["key_id"], 700)

    picked = ch.idle_participants(datetime.now(UTC), 600, {held["key_id"]})
    assert [p.handle for p in picked] == ["Stale"]
    assert ch.idle_participants(datetime.now(UTC), 800, set()) == []


def test_archived_channels_are_not_scanned(client):
    cid = room(client)
    me = join(client, cid)
    age(me["key_id"], 700)
    ch._channels[cid].archived = True
    assert ch.idle_participants(datetime.now(UTC), 600, set()) == []


# ---------------------------------------------------------------------------
# One reaper pass
# ---------------------------------------------------------------------------


def test_reaper_pass_removes_idle_participant_persistently(client):
    cid = room(client)
    # No mention receiver: hooked sessions go away instead (see the away tests).
    identity = {"session_id": "s-ghost", "agent": "Claude"}
    ghost = join(client, cid, **identity)
    alive = join(client, cid, "Alive")
    age(ghost["key_id"], 30)

    with client.websocket_connect(f"/channels/{cid}/ws?key={alive['key_id']}") as ws:
        settle(ws)
        reaped = client.portal.call(server._reap_idle_once, 10)
        assert reaped == [ghost["handle"]]
        assert ws.receive_json() == {
            "type": "system",
            "text": f"{ghost['handle']} left the channel (idle)",
        }

    say = client.post(f"/channels/{cid}/say", json={"key_id": ghost["key_id"], "text": "hi"})
    assert say.status_code == 404
    with SessionLocal() as db:
        assert db.get(ParticipantKeyDB, ghost["key_id"]).channel_id is None
        assert db.query(MentionReceiverDB).filter_by(key_id=ghost["key_id"]).count() == 0

    restart()
    assert ch.get_participant(ghost["key_id"]) is None
    assert ch.get_participant(alive["key_id"]) is not None

    # Reaping is a leave, not a kick: the same session comes back as itself.
    again = join(client, cid, **identity)
    assert again["key_id"] != ghost["key_id"]
    assert again["handle"] == ghost["handle"]


def test_held_agent_websocket_keeps_a_stale_participant(client):
    cid = room(client)
    me = join(client, cid)
    with client.websocket_connect(f"/channels/{cid}/ws?key={me['key_id']}") as ws:
        settle(ws)
        assert server._live_agent_sockets == {me["key_id"]: 1}
        age(me["key_id"], 700)
        assert client.portal.call(server._reap_idle_once, 600) == []
    # Disconnecting starts a fresh grace period rather than reaping at once.
    deadline = time.monotonic() + 2
    while server._live_agent_sockets and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server._live_agent_sockets == {}
    assert client.portal.call(server._reap_idle_once, 600) == []
    assert ch.get_participant(me["key_id"]) is not None


async def test_reaper_pass_without_clients_and_interval_bounds(client):
    cid = room(client)
    me = join(client, cid)
    age(me["key_id"], 2)
    assert await server._reap_idle_once(1) == [me["handle"]]
    assert await server._reap_idle_once(1) == []
    assert server._reap_interval(600) == 60
    assert server._reap_interval(60) == 15
    assert server._reap_interval(4) == 5


# ---------------------------------------------------------------------------
# Setting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 600), ("", 600), ("0", 0), ("120", 120), (" 30 ", 30), ("soon", 600), ("-5", 600)],
)
def test_idle_reap_seconds(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("AGENT_PTT_IDLE_REAP_SECONDS", raising=False)
    else:
        monkeypatch.setenv("AGENT_PTT_IDLE_REAP_SECONDS", raw)
    assert ch.idle_reap_seconds() == expected


def test_garbage_setting_warns(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_PTT_IDLE_REAP_SECONDS", "soon")
    with caplog.at_level("WARNING"):
        assert ch.idle_reap_seconds() == 600
    assert "AGENT_PTT_IDLE_REAP_SECONDS" in caplog.text


def test_reap_message_describes_mode():
    assert "off" in ch.idle_reap_message(0)
    assert "600s" in ch.idle_reap_message(600)


def test_reaper_task_runs_only_when_enabled(monkeypatch, fake_tts, fake_mixer):
    from fastapi.testclient import TestClient

    started = []

    async def fake_reaper(grace):
        started.append(grace)

    monkeypatch.setattr(server, "_idle_reaper", fake_reaper)
    monkeypatch.setenv("AGENT_PTT_IDLE_REAP_SECONDS", "0")
    with TestClient(server.app):
        pass
    monkeypatch.setenv("AGENT_PTT_IDLE_REAP_SECONDS", "45")
    with TestClient(server.app):
        pass
    assert started == [45]


# ---------------------------------------------------------------------------
# Leave is announced
# ---------------------------------------------------------------------------


def test_leave_is_broadcast(client):
    cid = room(client)
    me = join(client, cid, "Ada")
    other = join(client, cid, "Bob")
    with client.websocket_connect(f"/channels/{cid}/ws?key={other['key_id']}") as ws:
        settle(ws)
        assert (
            client.post(f"/channels/{cid}/leave", json={"key_id": me["key_id"]}).status_code == 200
        )
        assert ws.receive_json() == {"type": "system", "text": "Ada left the channel"}
