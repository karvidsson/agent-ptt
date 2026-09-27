"""Message context: stored with a message, summarized on the wire, pulled in full."""

import asyncio

import pytest
from pydantic import ValidationError

from agent_ptt import channel as ch
from agent_ptt.models import Message, MessageContext, MessageDB

CONTEXT = {
    "agent": "Claude",
    "repo": "agent-ptt",
    "branch": "main",
    "dirty": 3,
    "files": [
        {"path": "agent_ptt/server.py", "op": "edit"},
        {"path": "docs/api-reference.md", "op": "read"},
    ],
    "tools": {"Edit": 2, "Bash": 1},
    "task": "message context slice 1",
}
SUMMARY = {"files": 2, "branch": "main", "repo": "agent-ptt"}


def _create_channel(client, name="Test Room") -> str:
    return client.post("/channels", json={"name": name}).json()["channel_id"]


def _join(client, channel_id, handle="Claude") -> str:
    resp = client.post(f"/channels/{channel_id}/join", json={"handle": handle, "voice_id": "alba"})
    return resp.json()["key_id"]


def _say(client, channel_id, key_id, text="hi", context=CONTEXT):
    body = {"key_id": key_id, "text": text}
    if context is not None:
        body["context"] = context
    return client.post(f"/channels/{channel_id}/say", json=body)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def test_say_with_context_persists_and_single_message_returns_it(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)

    resp = _say(client, channel_id, key_id)
    assert resp.status_code == 200
    message_id = resp.json()["message_id"]
    assert resp.json()["context"]["repo"] == "agent-ptt"

    single = client.get(f"/channels/{channel_id}/messages/{message_id}")
    assert single.status_code == 200
    body = single.json()
    assert body["text"] == "hi"
    assert body["has_context"] is True
    assert body["context_summary"] == SUMMARY
    assert body["context"]["files"] == CONTEXT["files"]
    assert body["context"]["tools"] == {"Edit": 2, "Bash": 1}
    assert body["context"]["worktree"] is None


def test_history_carries_summary_but_not_full_context(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)
    _say(client, channel_id, key_id, "with")
    _say(client, channel_id, key_id, "without", context=None)

    history = client.get(f"/channels/{channel_id}/history").json()

    assert [m["text"] for m in history] == ["with", "without"]
    assert all("context" not in m for m in history)
    assert history[0]["has_context"] is True
    assert history[0]["context_summary"] == SUMMARY
    assert history[1]["has_context"] is False
    assert history[1]["context_summary"] is None


def test_history_with_context_flag_includes_it(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)
    _say(client, channel_id, key_id, "with")
    _say(client, channel_id, key_id, "without", context=None)

    history = client.get(f"/channels/{channel_id}/history", params={"with_context": 1}).json()

    assert history[0]["context"]["branch"] == "main"
    assert history[0]["context"]["files"] == CONTEXT["files"]
    assert history[1]["context"] is None
    assert history[1]["has_context"] is False


def test_single_message_unknown_is_404(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)
    message_id = _say(client, channel_id, key_id).json()["message_id"]

    assert client.get(f"/channels/{channel_id}/messages/nope").status_code == 404
    other = _create_channel(client, "Other")
    # A real message id doesn't leak across channels.
    assert client.get(f"/channels/{other}/messages/{message_id}").status_code == 404


def test_too_many_files_is_422(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)
    files = [{"path": f"f{i}.py", "op": "edit"} for i in range(51)]

    resp = _say(client, channel_id, key_id, context={"files": files})

    assert resp.status_code == 422
    assert client.get(f"/channels/{channel_id}/history").json() == []


def test_oversized_context_is_422(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)
    files = [{"path": "src/" + "x" * 100 + f"{i}.py", "op": "read"} for i in range(50)]

    resp = _say(client, channel_id, key_id, context={"files": files})

    assert resp.status_code == 422
    assert "4096" in resp.text


def test_unknown_context_key_and_bad_op_are_422(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)

    assert _say(client, channel_id, key_id, context={"secret": "x"}).status_code == 422
    bad_op = {"files": [{"path": "a.py", "op": "delete"}]}
    assert _say(client, channel_id, key_id, context=bad_op).status_code == 422


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


def test_ws_message_with_context_broadcasts_summary_only(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)

    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        ws.send_json({"type": "message", "text": "from ws", "context": CONTEXT})
        received = ws.receive_json()

    assert received["type"] == "message"
    assert received["text"] == "from ws"
    assert received["has_context"] is True
    assert received["context_summary"] == SUMMARY
    assert "context" not in received

    single = client.get(f"/channels/{channel_id}/messages/{received['message_id']}").json()
    assert single["context"]["tools"] == {"Edit": 2, "Bash": 1}


def test_ws_invalid_context_gets_error_frame_and_stays_open(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)

    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        ws.send_json({"type": "message", "text": "bad", "context": {"nope": 1}})
        error = ws.receive_json()
        assert error["type"] == "error"
        assert "nope" in error["detail"]

        ws.send_json({"type": "message", "text": "still here"})
        assert ws.receive_json()["text"] == "still here"

    assert [m["text"] for m in client.get(f"/channels/{channel_id}/history").json()] == [
        "still here"
    ]


def test_rest_broadcast_carries_no_context_when_absent(client):
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)

    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        _say(client, channel_id, key_id, "plain", context=None)
        received = ws.receive_json()

    assert received["has_context"] is False
    assert received["context_summary"] is None


# ---------------------------------------------------------------------------
# Persistence and restore
# ---------------------------------------------------------------------------


def test_send_message_persists_context_and_restore_keeps_it(db_session):
    created = ch.create_channel("Room", db=db_session)
    key = ch.join_channel(created.channel_id, "Ada", db=db_session)
    context = MessageContext.model_validate(CONTEXT)
    msg = asyncio.run(ch.send_message(key.key_id, "hello", db=db_session, context=context))
    asyncio.run(ch.send_message(key.key_id, "plain", db=db_session))

    row = db_session.get(MessageDB, msg.message_id)
    assert row.context["repo"] == "agent-ptt"
    assert "worktree" not in row.context  # None keys are not stored

    ch._channels.clear()
    ch._message_queues.clear()
    ch.restore_channels(db_session)

    restored = ch.get_history(created.channel_id)
    assert restored[0].context == context
    assert restored[1].context is None


def test_get_message_falls_back_to_the_archive(db_session):
    created = ch.create_channel("Room", db=db_session)
    key = ch.join_channel(created.channel_id, "Ada", db=db_session)
    context = MessageContext(branch="main")
    msg = asyncio.run(ch.send_message(key.key_id, "hello", db=db_session, context=context))

    assert ch.get_message(created.channel_id, msg.message_id) is msg

    # Not in memory any more (e.g. before restore ran): read from the DB.
    ch._channels.clear()
    found = ch.get_message(created.channel_id, msg.message_id, db=db_session)
    assert found is not None
    assert found.context == context
    assert found.timestamp.tzinfo is not None
    assert ch.get_message("other-channel", msg.message_id, db=db_session) is None
    assert ch.get_message(created.channel_id, "missing", db=db_session) is None


# ---------------------------------------------------------------------------
# Helpers and model
# ---------------------------------------------------------------------------


def _msg(context=None) -> Message:
    return Message(channel_id="c", sender_key="k", handle="h", text="t", context=context)


def test_context_summary_none_without_context():
    assert ch.context_summary(_msg()) is None


def test_context_summary_only_present_keys():
    assert ch.context_summary(_msg(MessageContext())) == {}
    assert ch.context_summary(_msg(MessageContext(branch="main"))) == {"branch": "main"}
    assert ch.context_summary(_msg(MessageContext(files=[]))) == {"files": 0}
    full = MessageContext.model_validate(CONTEXT)
    assert ch.context_summary(_msg(full)) == SUMMARY


def test_message_context_limits():
    with pytest.raises(ValidationError):
        MessageContext(task="x" * 201)
    with pytest.raises(ValidationError):
        MessageContext(files=[{"path": "a", "op": "edit"}] * 51)
    with pytest.raises(ValidationError, match="4096"):
        MessageContext(transcript="x" * 5000)
    assert MessageContext(task="x" * 200, files=[{"path": "a", "op": "run"}] * 50)
