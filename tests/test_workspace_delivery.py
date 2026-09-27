"""Tenant inbox authority, durable retry, and authenticated replies."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agent_ptt.db import SessionLocal, init_db
from agent_ptt.server import app
from agent_ptt.workspace_auth import issue_credential
from agent_ptt.workspace_delivery import Delivery
from agent_ptt.workspace_models import Agent, ChatMessage, Member, Organization, Room, User


@pytest.fixture
def tenant(client, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_HOSTED", "1")
    with SessionLocal() as db:
        user = User(email="delivery@test.example", name="Owner", password_hash="unused")
        db.add(user)
        db.flush()
        org = Organization(name="First", owner_id=user.id)
        other = Organization(name="Other", owner_id=user.id)
        db.add_all([org, other])
        db.flush()
        db.add_all(
            [Member(org_id=org.id, user_id=user.id), Member(org_id=other.id, user_id=user.id)]
        )
        room = Room(org_id=org.id, name="general")
        elsewhere = Room(org_id=other.id, name="general")
        a, b, remote = [
            Agent(org_id=o, name=n) for o, n in [(org.id, "A"), (org.id, "B"), (other.id, "C")]
        ]
        db.add_all([room, elsewhere, a, b, remote])
        db.flush()
        tokens = [issue_credential(db, agent_id=agent.id) for agent in (a, b, remote)]
        human_token = issue_credential(db, user_id=user.id)
        db.commit()
        data = {
            "org": org.id,
            "other": other.id,
            "room": room.id,
            "elsewhere": elsewhere.id,
            "a": a.id,
            "b": b.id,
            "remote": remote.id,
            "user": user.id,
        }
    data["owner"] = TestClient(app, headers={"Authorization": "Bearer " + human_token})
    for key, token in zip(("bot", "second", "foreign"), tokens, strict=True):
        data[key] = TestClient(app, headers={"Authorization": "Bearer " + token})
    data["base"] = f"/api/workspace/organizations/{data['org']}/channels/{data['room']}"
    yield data
    for key in ("owner", "bot", "second", "foreign"):
        data[key].close()


def send(t, **updates):
    payload = {"text": "Please check tests", "client_id": "request-1", "recipient_ids": [t["a"]]}
    return t["owner"].post(t["base"] + "/messages", json={**payload, **updates})


def test_only_selected_agent_receives_and_acknowledges(tenant):
    t = tenant
    response = send(t)
    assert response.status_code == 201
    mid = response.json()["id"]
    assert [m["id"] for m in t["bot"].get(t["base"] + "/inbox").json()] == [mid]
    assert t["second"].get(t["base"] + "/inbox").json() == []
    assert t["owner"].get(t["base"] + "/inbox").status_code == 403
    ack = {"message_ids": [mid]}
    assert t["second"].post(t["base"] + "/inbox/ack", json=ack).status_code == 404
    for _ in range(2):
        assert t["bot"].post(t["base"] + "/inbox/ack", json=ack).status_code == 200
    assert t["bot"].get(t["base"] + "/inbox").json() == []
    assert (
        t["owner"].get(t["base"] + "/messages").json()[0]["deliveries"][0]["status"] == "delivered"
    )


def test_cross_tenant_targets_reads_acks_and_db_constraint(tenant):
    t = tenant
    assert send(t, recipient_ids=[t["remote"]]).status_code == 422
    mid = send(t).json()["id"]
    for suffix in ("/inbox", "/messages"):
        assert t["foreign"].get(t["base"] + suffix).status_code == 404
    assert (
        t["foreign"].post(t["base"] + "/inbox/ack", json={"message_ids": [mid]}).status_code == 404
    )
    with SessionLocal() as db:
        db.add(
            Delivery(
                org_id=t["other"], message_id=mid, recipient_id=t["remote"], recipient_name="Bad"
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_retry_same_content_once_changed_recipients_conflict_and_restart(tenant):
    t = tenant
    first = send(t).json()
    init_db()
    assert send(t).json()["id"] == first["id"]
    assert send(t, recipient_ids=[t["b"]]).status_code == 409
    assert send(t, recipient_ids=[]).status_code == 409
    assert len(t["bot"].get(t["base"] + "/inbox").json()) == 1
    assert len(t["owner"].get(t["base"] + "/messages").json()) == 1


def test_revoked_agent_cannot_read_ack_or_reply_and_human_retry_stays_valid(tenant):
    t = tenant
    mid = send(t).json()["id"]
    path = f"/api/workspace/organizations/{t['org']}/agents/{t['a']}"
    assert t["owner"].delete(path).status_code == 204
    assert t["bot"].get(t["base"] + "/inbox").status_code == 401
    assert t["bot"].post(t["base"] + "/inbox/ack", json={"message_ids": [mid]}).status_code == 401
    assert (
        t["bot"]
        .post(t["base"] + "/messages", json={"text": "reply", "client_id": "r", "reply_to": mid})
        .status_code
        == 401
    )
    assert send(t).json()["id"] == mid  # A lost response can be recovered after revocation.
    assert send(t, client_id="new").status_code == 422


def test_reply_authority_identity_and_idempotence(tenant):
    t = tenant
    mid = send(t).json()["id"]
    body = {"text": "Tests passed", "client_id": "reply", "reply_to": mid}
    assert t["second"].post(t["base"] + "/messages", json=body).status_code == 404
    assert t["owner"].post(t["base"] + "/messages", json=body).status_code == 404
    result = t["bot"].post(t["base"] + "/messages", json=body).json()
    assert result["sender_id"] == t["a"] and result["sender_kind"] == "agent"
    assert result["reply_to"] == mid
    assert t["bot"].post(t["base"] + "/messages", json=body).json()["id"] == result["id"]
    assert (
        t["bot"].post(t["base"] + "/messages", json={**body, "sender_id": t["b"]}).status_code
        == 422
    )
    assert t["bot"].get(t["base"] + "/inbox").json() == []


def test_atomicity_limits_and_all_or_nothing_ack(tenant, monkeypatch):
    from agent_ptt import workspace_delivery

    t = tenant
    assert send(t, recipient_ids=[t["a"]] * 21).status_code == 422
    assert send(t, text="x" * 8001).status_code == 422
    mid = send(t).json()["id"]
    assert (
        t["bot"].post(t["base"] + "/inbox/ack", json={"message_ids": [mid, 999999]}).status_code
        == 404
    )
    assert t["bot"].get(t["base"] + "/inbox").json()

    def fail(*args):
        raise RuntimeError("write failure")

    with monkeypatch.context() as patch:
        patch.setattr(workspace_delivery, "persist", fail)
        with pytest.raises(RuntimeError, match="write failure"):
            send(t, client_id="fail")
    with SessionLocal() as db:
        assert db.scalar(select(ChatMessage).where(ChatMessage.client_id == "fail")) is None
