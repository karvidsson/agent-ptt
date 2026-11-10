"""FYI messages and items: delivered as usual, but they wake no one.

``fyi: true`` on say and on project items marks every delivery non-waking;
``wake_only=1`` on both inboxes leaves FYIs out, and its long-poll ends only
on a waking message or item. FYIs stay pending for the next ordinary read.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agent_ptt import db as dbmod
from agent_ptt import projects as pj
from agent_ptt.db import SessionLocal
from agent_ptt.mention_policy import mention_policy
from agent_ptt.models import MessageDeliveryDB, WorkItemDB

REMOTE = "git@github.com:Owner/Fyi.git"


@pytest.fixture(autouse=True)
def _reset_projects():
    pj.reset_runtime_state()
    yield
    pj.reset_runtime_state()


def room(client):
    return client.post("/channels", json={"name": "fyi"}).json()["channel_id"]


def cjoin(client, cid, handle, session=None):
    body = {"handle": handle, "voice_id": "fake"}
    if session:
        body.update(session_id=session, agent="Claude", receive_mentions=True)
    response = client.post(f"/channels/{cid}/join", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def at(agent):
    return pj.mention_text(agent["handle"])


def say(client, cid, who, text, **extra):
    response = client.post(
        f"/channels/{cid}/say", json={"key_id": who["key_id"], "text": text, **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


def chan_inbox(client, cid, who, **params):
    response = client.get(
        f"/channels/{cid}/inbox", params=params, headers={"X-Participant-Key": who["key_id"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def pjoin(client, handle, **extra):
    response = client.post("/projects/join", json={"remote": REMOTE, "handle": handle, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def hdr(member):
    return {"X-Member-Key": member["member_key"]}


def pid(member):
    return member["project"]["project_id"]


def send(client, member, **body):
    response = client.post(f"/projects/{pid(member)}/items", json=body, headers=hdr(member))
    assert response.status_code == 201, response.text
    return response.json()


def pinbox(client, member, **params):
    response = client.get(f"/projects/{pid(member)}/inbox", params=params, headers=hdr(member))
    assert response.status_code == 200, response.text
    return response.json()


def in_background(fn):
    result: dict = {}

    def run():
        result["value"] = fn()
        result["at"] = time.monotonic()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_policy_advertises_fyi_and_stays_v1():
    policy = mention_policy()
    assert policy["version"] == 1 and policy["idle_wakeup"] is False
    assert policy["fyi_supported"] is True


# ---------------------------------------------------------------------------
# Channel inbox
# ---------------------------------------------------------------------------


def test_fyi_mention_is_delivered_but_not_waking(client):
    cid = room(client)
    human, agent = cjoin(client, cid, "Human"), cjoin(client, cid, "Otto", "s-otto")
    with client.websocket_connect(f"/channels/{cid}/ws?key={human['key_id']}") as ws:
        msg = say(client, cid, human, f"{at(agent)} build is green", fyi=True)
        event = ws.receive_json()
    assert msg["fyi"] is True and event["fyi"] is True
    assert msg["deliveries"] == [
        {"recipient_id": agent["mention_id"], "handle": agent["handle"], "status": "pending"}
    ]
    with SessionLocal() as db:
        assert db.get(MessageDeliveryDB, (msg["message_id"], agent["mention_id"])).wake is False

    # An ordinary read sees it, marked; a wake_only read does not.
    assert [(m["message_id"], m["fyi"]) for m in chan_inbox(client, cid, agent)] == [
        (msg["message_id"], True)
    ]
    assert chan_inbox(client, cid, agent, wake_only=1) == []
    # Neither read consumed it: it stays pending until acked.
    assert len(chan_inbox(client, cid, agent)) == 1

    history = client.get(f"/channels/{cid}/history").json()
    assert [m["fyi"] for m in history] == [True]
    single = client.get(f"/channels/{cid}/messages/{msg['message_id']}").json()
    assert single["fyi"] is True


def test_old_clients_without_the_field_behave_as_before(client):
    cid = room(client)
    human, agent = cjoin(client, cid, "Human"), cjoin(client, cid, "Otto", "s-otto")
    msg = say(client, cid, human, f"{at(agent)} please look")
    assert msg["fyi"] is False
    with SessionLocal() as db:
        assert db.get(MessageDeliveryDB, (msg["message_id"], agent["mention_id"])).wake is True
    rows = chan_inbox(client, cid, agent)
    assert [(m["message_id"], m["fyi"]) for m in rows] == [(msg["message_id"], False)]
    assert chan_inbox(client, cid, agent, wake_only=1) == rows


def test_websocket_say_can_be_fyi(client):
    cid = room(client)
    human, agent = cjoin(client, cid, "Human"), cjoin(client, cid, "Otto", "s-otto")
    with client.websocket_connect(f"/channels/{cid}/ws?key={human['key_id']}") as ws:
        ws.send_json({"type": "message", "text": f"{at(agent)} done, FYI", "fyi": True})
        assert ws.receive_json()["fyi"] is True
        ws.send_json({"type": "message", "text": f"{at(agent)} real question", "fyi": None})
        assert ws.receive_json()["fyi"] is False
    assert [m["fyi"] for m in chan_inbox(client, cid, agent)] == [True, False]
    assert [m["text"] for m in chan_inbox(client, cid, agent, wake_only=1)] == [
        f"{at(agent)} real question"
    ]


def test_wake_only_long_poll_ignores_fyis_and_ends_on_waking_mention(client):
    cid = room(client)
    human, agent = cjoin(client, cid, "Human"), cjoin(client, cid, "Otto", "s-otto")
    thread, result = in_background(lambda: chan_inbox(client, cid, agent, wait=10, wake_only=1))
    time.sleep(0.3)
    say(client, cid, human, f"{at(agent)} status: tests running", fyi=True)
    time.sleep(0.6)
    assert thread.is_alive()  # The FYI did not end the wait.
    sent_at = time.monotonic()
    say(client, cid, human, f"{at(agent)} which branch?")
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["at"] - sent_at < 3
    assert [m["text"] for m in result["value"]] == [f"{at(agent)} which branch?"]
    # The FYI is still pending for the next ordinary read, oldest first.
    assert [m["fyi"] for m in chan_inbox(client, cid, agent)] == [True, False]


def test_wake_only_wait_times_out_with_only_fyis_pending(client):
    cid = room(client)
    human, agent = cjoin(client, cid, "Human"), cjoin(client, cid, "Otto", "s-otto")
    say(client, cid, human, f"{at(agent)} fyi only", fyi=True)
    started = time.monotonic()
    assert chan_inbox(client, cid, agent, wait=0.5, wake_only=1) == []
    assert 0.4 < time.monotonic() - started < 3
    # An ordinary wait returns the pending FYI at once.
    started = time.monotonic()
    assert len(chan_inbox(client, cid, agent, wait=10)) == 1
    assert time.monotonic() - started < 2


# ---------------------------------------------------------------------------
# Project inbox
# ---------------------------------------------------------------------------


def test_fyi_item_is_stored_shown_and_skipped_by_wake_only(client):
    finn, rex = pjoin(client, "Finn"), pjoin(client, "Rex", roles=["dev"])
    fyi = send(client, finn, kind="note", to="role:dev", body="Released branch:x", fyi=True)
    plain = send(client, finn, kind="instruction", to="role:dev", body="Fix the parser")
    assert fyi["fyi"] is True and plain["fyi"] is False
    with SessionLocal() as db:
        assert db.get(WorkItemDB, fyi["item_id"]).fyi is True

    assert [i["item_id"] for i in pinbox(client, rex, wake_only=1)["items"]] == [plain["item_id"]]
    # The FYI stays pending (not acked, not hidden) for an ordinary read.
    items = pinbox(client, rex)["items"]
    assert [(i["item_id"], i["fyi"]) for i in items] == [
        (fyi["item_id"], True),
        (plain["item_id"], False),
    ]

    one = client.get(f"/projects/{pid(rex)}/items/{fyi['item_id']}", headers=hdr(rex)).json()
    assert one["item"]["fyi"] is True
    board = client.get("/work-items", params={"project_id": pid(rex)}).json()
    flags = {i["item_id"]: i["fyi"] for i in board["items"]}
    assert flags == {fyi["item_id"]: True, plain["item_id"]: False}


def test_project_wake_only_long_poll_ends_only_on_a_waking_item(client):
    finn, rex = pjoin(client, "Finn"), pjoin(client, "Rex", roles=["dev"])
    cursor = pinbox(client, rex)["cursor"]
    thread, result = in_background(lambda: pinbox(client, rex, wait=10, after=cursor, wake_only=1))
    time.sleep(0.3)
    send(client, finn, kind="note", to="role:dev", body="Status: halfway", fyi=True)
    time.sleep(0.6)
    assert thread.is_alive()
    sent_at = time.monotonic()
    real = send(client, finn, kind="question", to="role:dev", body="Which branch?")
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["at"] - sent_at < 3
    assert [i["item_id"] for i in result["value"]["items"]] == [real["item_id"]]
    assert result["value"]["cursor"] > cursor
    assert len(pinbox(client, rex)["items"]) == 2


def test_fyi_say_queues_fyi_project_items(client):
    theo = pjoin(client, "Theo")
    cid = theo["project"]["channel_id"]
    poppy = cjoin(client, cid, "Poppy")
    msg = say(client, cid, poppy, "@Theo released the parser claim", fyi=True)
    assert [q["to_label"] for q in msg["queued_items"]] == ["agent:Theo"]
    assert pinbox(client, theo, wake_only=1)["items"] == []
    items = pinbox(client, theo)["items"]
    assert len(items) == 1 and items[0]["fyi"] is True
    # Without fyi the queued item wakes as before.
    say(client, cid, poppy, "@Theo please review")
    assert [i["fyi"] for i in pinbox(client, theo, wake_only=1)["items"]] == [False]


# ---------------------------------------------------------------------------
# Upgrading an existing database
# ---------------------------------------------------------------------------


def test_init_db_adds_fyi_columns_to_existing_tables(tmp_path, monkeypatch):
    legacy = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy.begin() as connection:
        connection.execute(
            text("""
            CREATE TABLE messages (
                message_id VARCHAR PRIMARY KEY, channel_id VARCHAR,
                sender_key VARCHAR, handle VARCHAR, text TEXT, timestamp DATETIME
            )
        """)
        )
        connection.execute(
            text("""
            CREATE TABLE message_deliveries (
                message_id VARCHAR NOT NULL, recipient_id VARCHAR NOT NULL,
                channel_id VARCHAR NOT NULL, handle VARCHAR NOT NULL,
                created_at DATETIME NOT NULL, delivered_at DATETIME,
                PRIMARY KEY (message_id, recipient_id)
            )
        """)
        )
        connection.execute(
            text("""
            CREATE TABLE work_items (
                item_id VARCHAR PRIMARY KEY, project_id VARCHAR NOT NULL,
                kind VARCHAR NOT NULL, to_address VARCHAR NOT NULL,
                from_member_id VARCHAR NOT NULL, title VARCHAR NOT NULL, body TEXT NOT NULL,
                context JSON, reply_to VARCHAR, client_id VARCHAR, state VARCHAR NOT NULL,
                assignee_member_id VARCHAR, lease_expires_at DATETIME,
                notice_message_id VARCHAR, created_at DATETIME, updated_at DATETIME
            )
        """)
        )
        connection.execute(text("INSERT INTO messages VALUES ('m1', 'c1', 'k1', 'Ada', 'x', NULL)"))
        connection.execute(
            text(
                "INSERT INTO message_deliveries VALUES "
                "('m1', 'r1', 'c1', 'Otto', '2026-01-01', NULL)"
            )
        )
        connection.execute(
            text("""
            INSERT INTO work_items (item_id, project_id, kind, to_address, from_member_id,
                title, body, state)
            VALUES ('i1', 'p1', 'note', 'project', 'm1', 't', 'b', 'pending')
        """)
        )
    with monkeypatch.context() as patch:
        patch.setattr(dbmod, "engine", legacy)
        patch.setattr(dbmod, "SessionLocal", sessionmaker(bind=legacy, class_=Session))
        dbmod.init_db()
        dbmod.init_db()  # Repeated startup is safe.
    with legacy.connect() as connection:
        assert connection.execute(text("SELECT fyi FROM messages")).scalar_one() == 0
        assert connection.execute(text("SELECT wake FROM message_deliveries")).scalar_one() == 1
        assert connection.execute(text("SELECT fyi FROM work_items")).scalar_one() == 0
    legacy.dispose()


def test_quickstart_tells_agents_about_fyi_and_wake_only(client):
    quickstart = client.get("/llms.txt").text
    assert '"fyi": true' in quickstart and "wake_only=1" in quickstart


def test_fyi_survives_restart(client):
    from agent_ptt import channel

    cid = room(client)
    human, agent = cjoin(client, cid, "Human"), cjoin(client, cid, "Otto", "s-otto")
    msg = say(client, cid, human, f"{at(agent)} done", fyi=True)
    channel._channels.clear()
    channel._message_queues.clear()
    with SessionLocal() as db:
        channel.restore_channels(db)
    assert [m["fyi"] for m in client.get(f"/channels/{cid}/history").json()] == [True]
    assert chan_inbox(client, cid, agent, wake_only=1) == []
    assert [m["message_id"] for m in chan_inbox(client, cid, agent)] == [msg["message_id"]]
