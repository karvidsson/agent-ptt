"""Channel moderation preserves history and revokes participant access."""

from agent_ptt import channel as ch
from agent_ptt.db import SessionLocal
from agent_ptt.models import ArchivedChannelDB, ParticipantKeyDB


def room(client, name="room"):
    return client.post("/channels", json={"name": name}).json()["channel_id"]


def join(client, cid, handle="Ada"):
    return client.post(f"/channels/{cid}/join", json={"handle": handle, "voice_id": "alba"}).json()


def test_kick_revokes_duplicates_only_in_target_channel_and_survives_restore(client):
    cid, other = room(client), room(client, "other")
    first, duplicate, neighbor = join(client, cid), join(client, cid), join(client, other)
    response = client.post(f"/channels/{cid}/kick/{neighbor['key_id']}")
    assert response.status_code == 404
    assert client.post(f"/channels/{cid}/kick/{first['key_id']}").status_code == 200
    assert client.get(f"/channels/{cid}").json()["participants"] == {}
    for participant in (first, duplicate):
        assert (
            client.post(
                f"/channels/{cid}/say", json={"key_id": participant["key_id"], "text": "retry"}
            ).status_code
            == 403
        )
    assert (
        client.post(
            f"/channels/{other}/say", json={"key_id": neighbor["key_id"], "text": "hello"}
        ).status_code
        == 200
    )
    ch._channels.clear()
    with SessionLocal() as db:
        ch.restore_channels(db)
        assert db.get(ParticipantKeyDB, first["key_id"]).channel_id is None
    assert ch.get_channel(cid).participants == {}
    # Kick is removal, not a permanent ban: an explicit new join is allowed.
    assert join(client, cid)["key_id"] != first["key_id"]


def test_archive_preserves_history_blocks_auto_recreation_and_reopens(client):
    cid = room(client)
    participant = join(client, cid)
    message = client.post(
        f"/channels/{cid}/say", json={"key_id": participant["key_id"], "text": "keep me"}
    ).json()
    assert client.post(f"/channels/{cid}/archive").json()["archived"] is True
    assert client.get("/channels").json() == []
    assert client.get("/channels?include_archived=true").json()[0]["channel_id"] == cid
    assert client.get(f"/channels/{cid}/history").json()[0]["message_id"] == message["message_id"]
    assert client.post(f"/channels/{cid}/join", json={"handle": "new"}).status_code == 409
    assert (
        client.post("/channels", json={"name": "room", "reuse_existing": True}).status_code == 409
    )
    assert (
        client.post(
            f"/channels/{cid}/say", json={"key_id": participant["key_id"], "text": "retry"}
        ).status_code
        == 403
    )
    ch._channels.clear()
    with SessionLocal() as db:
        restored = ch.restore_channels(db)
        assert not restored
        assert db.get(ArchivedChannelDB, cid) is not None
    assert ch.get_channel(cid).archived
    assert client.post(f"/channels/{cid}/reopen").json()["archived"] is False
    assert len(client.get("/channels").json()) == 1
    assert join(client, cid)["channel_id"] == cid
    assert len(client.get(f"/channels/{cid}/history").json()) == 1


def test_kick_disconnects_agent_websocket(client):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    cid = room(client)
    participant = join(client, cid)
    with client.websocket_connect(f"/channels/{cid}/ws?key={participant['key_id']}") as ws:
        assert client.post(f"/channels/{cid}/kick/{participant['key_id']}").status_code == 200
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 4003


def test_archive_disconnects_audio_spectator(client):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    cid = room(client)
    with client.websocket_connect(f"/channels/{cid}/audio") as ws:
        assert client.post(f"/channels/{cid}/archive").status_code == 200
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_bytes()
        assert error.value.code == 4003
