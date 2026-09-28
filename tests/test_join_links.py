"""General links admit independent humans/agents without channel or owner prerequisites."""

import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select

from agent_ptt.db import SessionLocal
from agent_ptt.workspace_auth import digest
from agent_ptt.workspace_models import Agent, AgentEnrollment, Credential, JoinLink, Room, User, now
from tests.test_onboarding import owner as owner_fixture
from tests.test_workspace import API, PASSWORD, second_client, signup

owner = owner_fixture


def link(owner, org):
    response = owner.post(f"{API}/organizations/{org}/join-link")
    assert response.status_code == 201, response.text
    return urlsplit(response.json()["url"]).fragment.removeprefix("join=")


def enroll(token, name="Reviewer", credential=None):
    payload = {"token": token, "name": name, "credential": credential or secrets.token_urlsafe(32)}
    bot = second_client()
    response = bot.post(API + "/join/agent", json=payload)
    bot.headers["Authorization"] = "Bearer " + payload["credential"]
    return bot, response, payload


def test_same_link_human_and_eight_agents_then_channels(owner, monkeypatch):
    org = signup(owner)["organizations"][0]["id"]
    token = link(owner, org)
    base = f"{API}/organizations/{org}"
    monkeypatch.setenv("AGENT_PTT_SIGNUP", "0")
    person = second_client()
    assert (
        person.post(
            API + "/signup",
            json={
                "email": "new@example.com",
                "name": "New",
                "password": PASSWORD,
                "join_token": token,
            },
        ).status_code
        == 201
    )
    assert person.get(API + "/me").json()["organizations"][0]["role"] == "member"
    for _ in range(2):
        assert person.post(API + "/join/person", json={"token": token}).json() == {"org_id": org}
    assert person.post(base + "/channels", json={"name": "Human work"}).status_code == 201
    for i in range(8):
        bot, response, payload = enroll(token, f"Agent {i}")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "registered"
        assert bot.get(API + "/me").json()["organizations"][0]["id"] == org
        room = bot.post(base + "/channels", json={"name": f"Work {i}"}).json()
        assert room["name"] == f"Work {i}"
        assert (
            bot.post(
                base + f"/channels/{room['id']}/messages",
                json={
                    "text": "Starting work",
                    "client_id": "first",
                },
            ).status_code
            == 201
        )
        assert bot.post(base + "/join-link").status_code == 403
        assert bot.post(base + "/agents", json={"name": "Extra"}).status_code == 403
        assert payload["credential"] not in response.text
    assert len(person.get(base + "/channels").json()) == 10
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 2
        assert db.scalar(select(func.count()).select_from(Agent)) == 8
        assert db.get(JoinLink, org).digest == digest(token)


def test_agent_only_empty_org_and_tenant_isolation(owner):
    org = signup(owner)["organizations"][0]["id"]
    other = owner.post(API + "/organizations", json={"name": "Other"}).json()["id"]
    with SessionLocal() as db:
        db.execute(delete(Room).where(Room.org_id == org))
        db.commit()
    token = link(owner, org)
    bot, response, _ = enroll(token)
    assert response.status_code == 200
    assert bot.get(f"{API}/organizations/{org}/channels").json() == []
    assert (
        bot.post(f"{API}/organizations/{org}/channels", json={"name": "First"}).status_code == 201
    )
    assert bot.get(f"{API}/organizations/{other}/channels").status_code == 404
    assert (
        bot.post(f"{API}/organizations/{other}/channels", json={"name": "Wrong"}).status_code == 404
    )
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1


def test_parallel_retry_and_revoked_credential_cannot_reenroll(owner):
    org = signup(owner)["organizations"][0]["id"]
    token = link(owner, org)
    key = secrets.token_urlsafe(32)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: enroll(token, credential=key), range(4)))
    assert {r.status_code for _, r, _ in results} == {200}
    assert len({r.json()["id"] for _, r, _ in results}) == 1
    bot, response, payload = results[0]
    agent_id = response.json()["id"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AgentEnrollment)) == 1
    owner.delete(f"{API}/organizations/{org}/agents/{agent_id}")
    assert bot.post(API + "/join/agent", json=payload).status_code == 403
    assert bot.get(f"{API}/organizations/{org}/channels").status_code == 401


def test_rotation_revocation_preserve_enrolled_access(owner):
    org = signup(owner)["organizations"][0]["id"]
    token = link(owner, org)
    bot, response, _ = enroll(token)
    assert response.status_code == 200
    replacement = link(owner, org)
    assert enroll(token)[1].status_code == 404
    assert enroll(replacement, "Second")[1].status_code == 200
    base = f"{API}/organizations/{org}"
    assert owner.delete(base + "/join-link").status_code == 204
    assert owner.get(base + "/join-link").json()["active"] is False
    assert enroll(replacement, "Third")[1].status_code == 404
    assert bot.get(base + "/channels").status_code == 200
    assert (
        second_client().post(API + "/join/inspect", json={"token": replacement}).status_code == 404
    )


def test_expiry_name_conflict_and_bad_signup(owner, monkeypatch):
    org = signup(owner)["organizations"][0]["id"]
    token = link(owner, org)
    bot, response, payload = enroll(token)
    assert response.status_code == 200
    assert bot.post(API + "/join/agent", json={**payload, "name": "Changed"}).status_code == 409
    with SessionLocal() as db:
        db.get(Credential, digest(payload["credential"])).expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert bot.post(API + "/join/agent", json=payload).status_code == 403
    monkeypatch.setenv("AGENT_PTT_SIGNUP", "0")
    failed = second_client().post(
        API + "/signup",
        json={
            "email": "bad@example.com",
            "name": "Bad",
            "password": PASSWORD,
            "join_token": secrets.token_urlsafe(32),
        },
    )
    assert failed.status_code == 404
    with SessionLocal() as db:
        assert db.scalar(select(User).where(User.email == "bad@example.com")) is None


def test_existing_human_role_preserved_and_member_cannot_manage_link(owner):
    org = signup(owner)["organizations"][0]["id"]
    token = link(owner, org)
    assert owner.post(API + "/join/person", json={"token": token}).status_code == 200
    assert owner.get(API + "/me").json()["organizations"][0]["role"] == "owner"
    person = second_client()
    signup(person, "person@example.com", None)
    assert person.post(API + "/join/person", json={"token": token}).status_code == 200
    base = f"{API}/organizations/{org}"
    for method in ("GET", "POST", "DELETE"):
        assert person.request(method, base + "/join-link").status_code == 403


def test_agent_mention_and_reply_after_enrollment(owner):
    org = signup(owner)["organizations"][0]["id"]
    bot, response, _ = enroll(link(owner, org))
    room = bot.post(f"{API}/organizations/{org}/channels", json={"name": "Testing"}).json()
    path = f"{API}/organizations/{org}/channels/{room['id']}"
    mention = owner.post(
        path + "/messages",
        json={
            "text": "Please confirm",
            "client_id": "test",
            "recipient_ids": [response.json()["id"]],
        },
    ).json()
    assert bot.get(path + "/inbox").json()[0]["id"] == mention["id"]
    reply = bot.post(
        path + "/messages",
        json={
            "text": "Confirmed",
            "client_id": "reply",
            "reply_to": mention["id"],
        },
    )
    assert reply.status_code == 201
    assert bot.get(path + "/inbox").json() == []
