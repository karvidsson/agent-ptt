"""Presence uses real authentication and deterministic server time, never chat."""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from agent_ptt import workspace
from agent_ptt import workspace_presence as presence
from agent_ptt.workspace_auth import COOKIE, _attempts, issue_credential
from agent_ptt.workspace_models import Agent, Credential, Member, Organization, Room, User

BASE = "/api/workspace/organizations"
CLOCK = datetime(2030, 1, 1, 12)


@pytest.fixture
def setup(db_session, monkeypatch):
    _attempts.clear()
    monkeypatch.setenv("AGENT_PTT_PUBLIC_ORIGIN", "http://localhost:8770")
    monkeypatch.setattr(presence, "now", lambda: CLOCK)
    db = db_session
    tokens = {}
    for suffix in ("a", "b"):
        user = User(
            id=f"user-{suffix}",
            email=f"{suffix}@example.com",
            name=suffix,
            password_hash="not-used",
        )
        db.add(user)
        db.flush()
        db.add(Organization(id=suffix, name=suffix, owner_id=user.id))
        db.flush()
        db.add_all(
            [
                Member(org_id=suffix, user_id=user.id, role="member"),
                Agent(id=f"agent-{suffix}", org_id=suffix, name=f"Agent {suffix}"),
                Room(id=f"room-{suffix}", org_id=suffix, name="general"),
            ]
        )
        db.flush()
        tokens[f"user-{suffix}"] = issue_credential(db, user_id=user.id)
        tokens[f"agent-{suffix}"] = issue_credential(db, agent_id=f"agent-{suffix}")
    db.add(Agent(id="other-agent", org_id="a", name="Other"))
    db.flush()
    tokens["other-agent"] = issue_credential(db, agent_id="other-agent")
    db.commit()
    app = FastAPI()
    workspace.install(app)
    if not any(
        getattr(route, "path", "").endswith("/sessions/{session_id}/presence")
        for route in app.routes
    ):
        presence.install(app)
    with TestClient(app) as client:
        yield client, db, tokens
    _attempts.clear()


def headers(setup, who="agent-a"):
    return {"Authorization": f"Bearer {setup[2][who]}"}


def post(setup, session="one", org="a", who="agent-a", **body):
    return setup[0].post(
        f"{BASE}/{org}/sessions/{session}/presence",
        json={"state": "working", **body},
        headers=headers(setup, who),
    )


def roster(setup, org="a", who="agent-a", **params):
    return setup[0].get(f"{BASE}/{org}/sessions", headers=headers(setup, who), params=params)


def test_server_timestamps_and_fixed_response(setup):
    assert (
        post(setup, task="Implement presence", channel_id="room-a", tokens_in=4).status_code == 204
    )
    row = roster(setup).json()[0]
    assert row == {
        "session_id": "one",
        "principal_id": "agent-a",
        "kind": "agent",
        "agent": "Agent a",
        "handle": "Agent a",
        "state": "working",
        "since": "2030-01-01T12:00:00Z",
        "last_refresh": "2030-01-01T12:00:00Z",
        "task": "Implement presence",
        "channel_id": "room-a",
        "tokens_in": 4,
        "tokens_out": None,
        "presumed_hung": False,
        "stale": False,
        "effective_state": "working",
    }


@pytest.mark.parametrize(
    "seconds,hung,stale",
    [(90, False, False), (91, True, False), (3600, True, False), (3601, True, True)],
)
def test_expiry_preserves_reported_state(setup, monkeypatch, seconds, hung, stale):
    post(setup)
    monkeypatch.setattr(presence, "now", lambda: CLOCK + timedelta(seconds=seconds))
    row = roster(setup).json()[0]
    assert (row["state"], row["presumed_hung"], row["stale"]) == ("working", hung, stale)
    assert row["effective_state"] == ("offline" if stale else "working")
    assert setup[1].get(presence.SessionPresence, ("a", "one")).state == "working"


def test_heartbeat_recovers_and_only_transitions_change_since(setup, monkeypatch):
    post(setup, task="long tool", tokens_out=7)
    monkeypatch.setattr(presence, "now", lambda: CLOCK + timedelta(hours=2))
    assert roster(setup).json()[0]["stale"]
    post(setup)
    row = roster(setup).json()[0]
    assert not row["stale"] and not row["presumed_hung"]
    assert row["since"] == "2030-01-01T12:00:00Z"
    assert row["last_refresh"] == "2030-01-01T14:00:00Z"
    assert (row["task"], row["tokens_out"]) == ("long tool", 7)
    post(setup, state="idle", task=None, tokens_out=None)
    row = roster(setup).json()[0]
    assert row["since"] == row["last_refresh"]
    assert row["task"] is None and row["tokens_out"] is None


@pytest.mark.parametrize("state", ["idle", "listening", "draining", "offline"])
def test_nonworking_states_are_never_presumed_hung(setup, monkeypatch, state):
    post(setup, state=state)
    monkeypatch.setattr(presence, "now", lambda: CLOCK + timedelta(hours=2))
    row = roster(setup).json()[0]
    assert row["stale"] and not row["presumed_hung"]
    assert row["state"] == state and row["effective_state"] == "offline"


def test_authentication_and_cross_tenant_boundaries(setup):
    client = setup[0]
    assert client.get(f"{BASE}/a/sessions").status_code == 401
    assert (
        client.post(
            f"{BASE}/a/sessions/one/presence",
            json={"state": "idle"},
            headers={"Origin": "http://localhost:8770"},
        ).status_code
        == 401
    )
    post(setup)
    assert roster(setup, who="agent-b").status_code == 404
    assert roster(setup, who="user-b").status_code == 404
    assert post(setup, who="agent-b").status_code == 404
    assert post(setup, org="b", who="agent-b").status_code == 204
    assert roster(setup, org="b", who="agent-b").json()[0]["principal_id"] == "agent-b"
    assert roster(setup).json()[0]["principal_id"] == "agent-a"


def test_no_impersonation_even_by_owner(setup):
    post(setup)
    assert post(setup, who="other-agent", state="offline").status_code == 404
    assert post(setup, who="user-a", state="offline").status_code == 404
    assert roster(setup).json()[0]["state"] == "working"
    assert post(setup, session="two", who="other-agent").status_code == 204
    assert post(setup, session="human", who="user-a", state="idle").status_code == 204
    human = next(row for row in roster(setup).json() if row["kind"] == "human")
    assert human["agent"] is None and human["principal_id"] == "user-a"


@pytest.mark.parametrize(
    "extra",
    [
        {"agent_id": "other-agent"},
        {"handle": "Other"},
        {"since": "2099-01-01"},
        {"last_refresh": "2099-01-01"},
        {"state": "busy"},
        {"tokens_in": -1},
        {"tokens_out": True},
        {"tokens_in": 2**31},
        {"task": "x" * 129},
    ],
)
def test_rejects_spoofed_fields_and_invalid_inputs(setup, extra):
    assert post(setup, **extra).status_code == 422
    assert roster(setup).json() == []


def test_channel_filter_and_tenant_foreign_key(setup):
    assert post(setup, channel_id="room-b").status_code == 404
    post(setup, channel_id="room-a")
    post(setup, session="two")
    assert len(roster(setup).json()) == 2
    assert len(roster(setup, channel_id="room-a").json()) == 1
    assert roster(setup, channel_id="room-b").status_code == 404
    assert roster(setup, limit=1).json()[0]["session_id"] == "one"
    assert roster(setup, limit=1, offset=1).json()[0]["session_id"] == "two"
    row = setup[1].get(presence.SessionPresence, ("a", "one"))
    row.channel_id = "room-b"
    with pytest.raises(IntegrityError):
        setup[1].commit()
    setup[1].rollback()


def test_revocation_and_membership_removal(setup):
    post(setup)
    post(setup, session="human", who="user-a", state="idle")
    db = setup[1]
    db.get(Agent, "agent-a").active = False
    db.commit()
    assert post(setup).status_code == 401
    assert roster(setup).status_code == 401
    assert [row["session_id"] for row in roster(setup, who="other-agent").json()] == ["human"]
    db.execute(delete(Member).where(Member.org_id == "a", Member.user_id == "user-a"))
    db.commit()
    assert roster(setup, who="user-a").status_code == 404
    assert post(setup, session="human", who="user-a").status_code == 404
    assert roster(setup, who="other-agent").json() == []


def test_credential_expiry_and_cookie_origin(setup):
    db = setup[1]
    from agent_ptt.workspace_auth import digest

    db.get(Credential, digest(setup[2]["agent-a"])).expires_at = datetime(2000, 1, 1)
    db.commit()
    assert post(setup).status_code == 401
    client = setup[0]
    client.cookies.set(COOKIE, setup[2]["user-a"])
    url = f"{BASE}/a/sessions/browser/presence"
    assert client.post(url, json={"state": "idle"}).status_code == 403
    assert (
        client.post(
            url, json={"state": "idle"}, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            url, json={"state": "idle"}, headers={"Origin": "http://localhost:8770"}
        ).status_code
        == 204
    )


def test_chat_does_not_create_or_refresh_presence(setup, monkeypatch):
    url = f"{BASE}/a/channels/room-a/messages"
    response = setup[0].post(url, headers=headers(setup), json={"text": "hi", "client_id": "m1"})
    assert response.status_code == 201
    assert roster(setup).json() == []
    post(setup, state="idle")
    monkeypatch.setattr(presence, "now", lambda: CLOCK + timedelta(hours=2))
    response = setup[0].post(url, headers=headers(setup), json={"text": "hi", "client_id": "m2"})
    assert response.status_code == 201
    row = roster(setup).json()[0]
    assert row["state"] == "idle" and row["stale"]
    assert row["last_refresh"] == "2030-01-01T12:00:00Z"


def test_session_limit_still_allows_existing_heartbeats(setup, monkeypatch):
    monkeypatch.setattr(presence, "MAX_SESSIONS_PER_IDENTITY", 1)
    assert post(setup).status_code == 204
    assert post(setup, session="two").status_code == 429
    assert post(setup, state="offline").status_code == 204
