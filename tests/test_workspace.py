"""Hosted boundary: real password/session flow, tenant isolation and live revocation."""

from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from agent_ptt import workspace_auth as auth
from agent_ptt.db import SessionLocal
from agent_ptt.server import app
from agent_ptt.workspace_models import Credential, User, now

API = "/api/workspace"
ORIGIN = "http://localhost:8770"
PASSWORD = "a long passphrase for testing"


@pytest.fixture(autouse=True)
def reset_limits():
    auth._attempts.clear()
    yield
    auth._attempts.clear()


@pytest.fixture
def hosted_client(client, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_HOSTED", "1")
    monkeypatch.setenv("AGENT_PTT_PUBLIC_ORIGIN", ORIGIN)
    client.headers["Origin"] = ORIGIN
    return client


def signup(client, email="owner@example.com", organization="Acme"):
    body = {"email": email, "name": email.split("@")[0], "password": PASSWORD}
    if organization:
        body["organization"] = organization
    response = client.post(API + "/signup", json=body)
    assert response.status_code == 201, response.text
    return client.get(API + "/me").json()


def room(client, org_id):
    return client.get(f"{API}/organizations/{org_id}/channels").json()[0]["id"]


def invite(client, org_id, role="member"):
    response = client.post(f"{API}/organizations/{org_id}/invitations", json={"role": role})
    assert response.status_code == 201, response.text
    return urlsplit(response.json()["url"]).fragment.removeprefix("invite=")


def accept(client, token):
    return client.post(API + "/invitations/accept", json={"token": token})


def second_client():
    # Uses the same app/DB without entering another lifespan or resetting state.
    return TestClient(app, headers={"Origin": ORIGIN})


def test_signup_owner_default_room_and_password_hash(hosted_client):
    client = hosted_client
    me = signup(client)
    assert me["organizations"][0]["role"] == "owner"
    assert room(client, me["organizations"][0]["id"])
    with SessionLocal() as db:
        user = db.scalar(select(User))
        assert user.password_hash.startswith("scrypt$")
        assert PASSWORD not in user.password_hash
        credential = db.scalar(select(Credential))
        assert credential.digest != client.cookies[auth.COOKIE]
    assert (
        "HttpOnly"
        in client.post(
            API + "/login", json={"email": "owner@example.com", "password": PASSWORD}
        ).headers["set-cookie"]
    )
    assert client.get("/").url.path == "/workspace/"
    assert client.get("/workspace/").status_code == 200


def test_signin_logout_and_password_whitespace(hosted_client):
    client = hosted_client
    password = "  a passphrase with spaces  "
    response = client.post(
        API + "/signup", json={"email": "space@example.com", "name": "A", "password": password}
    )
    assert response.status_code == 201
    old = client.cookies[auth.COOKIE]
    assert client.post(API + "/logout").status_code == 204
    assert client.get(API + "/me").status_code == 401
    assert client.get(API + "/me", headers={"Authorization": f"Bearer {old}"}).status_code == 401
    assert (
        client.post(
            API + "/login", json={"email": "space@example.com", "password": password.strip()}
        ).status_code
        == 401
    )
    assert (
        client.post(
            API + "/login", json={"email": "SPACE@example.com", "password": password}
        ).status_code
        == 200
    )


def test_no_anonymous_or_cross_tenant_access(hosted_client):
    alice = hosted_client
    first = signup(alice)["organizations"][0]["id"]
    first_room = room(alice, first)
    bob = second_client()
    second = signup(bob, "bob@example.com", "Other")["organizations"][0]["id"]
    assert bob.get(f"{API}/organizations/{first}/channels").status_code == 404
    assert (
        bob.get(f"{API}/organizations/{second}/channels/{first_room}/messages").status_code == 404
    )
    assert (
        bob.post(
            f"{API}/organizations/{first}/channels/{first_room}/messages",
            json={"text": "intrusion", "client_id": "x"},
        ).status_code
        == 404
    )
    assert bob.get(f"{API}/organizations/{first}/members").status_code == 404
    assert bob.get(f"{API}/organizations/{first}/agents").status_code == 404
    with (
        pytest.raises(WebSocketDisconnect),
        bob.websocket_connect(f"{API}/organizations/{first}/channels/{first_room}/stream"),
    ):
        pytest.fail("cross-tenant socket accepted")
    anon = second_client()
    assert anon.get(f"{API}/organizations/{first}/channels").status_code == 401


def test_hosted_blocks_every_legacy_surface(hosted_client):
    for path in [
        "/channels",
        "/channels/arbitrary/history",
        "/voices",
        "/voices/profiles",
        "/ui/",
        "/docs",
    ]:
        assert hosted_client.get(path).status_code == 404
    for suffix in ["ws", "audio"]:
        with (
            pytest.raises(WebSocketDisconnect),
            hosted_client.websocket_connect(f"/channels/arbitrary/{suffix}"),
        ):
            pytest.fail("legacy socket accepted")


def test_invitation_one_use_and_member_permissions(hosted_client):
    owner = hosted_client
    org_id = signup(owner)["organizations"][0]["id"]
    token = invite(owner, org_id)
    member = second_client()
    user_id = signup(member, "member@example.com", None)["id"]
    assert accept(member, token).status_code == 200
    assert accept(member, token).status_code == 404
    assert member.get(API + "/me").json()["organizations"][0]["role"] == "member"
    base = f"{API}/organizations/{org_id}"
    assert member.post(base + "/agents", json={"name": "Bot"}).status_code == 403
    assert member.post(base + "/channels", json={"name": "private"}).status_code == 403
    assert member.post(base + "/invitations", json={"role": "admin"}).status_code == 403
    assert member.patch(base + "/members/" + user_id, json={"role": "admin"}).status_code == 403
    assert owner.patch(base + "/members/" + user_id, json={"role": "admin"}).status_code == 200
    assert member.post(base + "/agents", json={"name": "Bot"}).status_code == 201
    assert member.post(base + "/invitations", json={"role": "admin"}).status_code == 403
    assert owner.delete(base + "/members/" + user_id).status_code == 204
    assert member.get(base + "/channels").status_code == 404


def test_owner_cannot_be_demoted_or_removed(hosted_client):
    me = signup(hosted_client)
    path = f"{API}/organizations/{me['organizations'][0]['id']}/members/{me['id']}"
    assert hosted_client.patch(path, json={"role": "member"}).status_code == 409
    assert hosted_client.delete(path).status_code == 403


def test_agent_identity_expiry_revocation_and_idempotent_send(hosted_client):
    owner = hosted_client
    org_id = signup(owner)["organizations"][0]["id"]
    room_id = room(owner, org_id)
    base = f"{API}/organizations/{org_id}"
    agent = owner.post(base + "/agents", json={"name": "Build agent"}).json()
    bot = TestClient(app, headers={"Authorization": "Bearer " + agent["token"]})
    assert bot.get(API + "/me").json()["kind"] == "agent"
    assert "token" not in owner.get(base + "/agents").text
    path = base + f"/channels/{room_id}/messages"
    payload = {"text": "Tests passed", "client_id": "retry-safe"}
    first = bot.post(path, json=payload)
    assert first.status_code == 201
    assert first.json()["sender_kind"] == "agent"
    assert bot.post(path, json=payload).json()["id"] == first.json()["id"]
    assert len(owner.get(path).json()) == 1
    assert bot.post(path, json={**payload, "text": "different"}).status_code == 409
    assert bot.post(path, json={**payload, "sender_id": "owner"}).status_code == 422
    assert bot.post(base + "/agents", json={"name": "Nested bot"}).status_code == 403
    assert owner.delete(base + "/agents/" + agent["id"]).status_code == 204
    assert bot.get(path).status_code == 401


def test_live_stream_revoked_on_member_removal(hosted_client):
    owner = hosted_client
    org_id = signup(owner)["organizations"][0]["id"]
    token = invite(owner, org_id)
    member = second_client()
    user_id = signup(member, "member@example.com", None)["id"]
    accept(member, token)
    room_id = room(member, org_id)
    base = f"{API}/organizations/{org_id}"
    with member.websocket_connect(base + f"/channels/{room_id}/stream") as socket:
        assert socket.receive_json()["messages"] == []
        owner.delete(base + "/members/" + user_id)
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 4403


def test_live_stream_receives_persisted_messages_and_reconnect_cursor(hosted_client):
    client = hosted_client
    org_id = signup(client)["organizations"][0]["id"]
    room_id = room(client, org_id)
    path = f"{API}/organizations/{org_id}/channels/{room_id}"
    first = client.post(path + "/messages", json={"text": "First", "client_id": "1"}).json()
    second = client.post(path + "/messages", json={"text": "Second", "client_id": "2"}).json()
    with client.websocket_connect(path + f"/stream?after={first['id']}") as socket:
        assert [m["id"] for m in socket.receive_json()["messages"]] == [second["id"]]


def test_cookie_csrf_and_websocket_origin(hosted_client):
    client = hosted_client
    org_id = signup(client)["organizations"][0]["id"]
    room_id = room(client, org_id)
    assert (
        client.post(
            API + "/organizations",
            json={"name": "Forged"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    client.headers.pop("origin")
    assert client.post(API + "/organizations", json={"name": "Forged"}).status_code == 403
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            f"{API}/organizations/{org_id}/channels/{room_id}/stream",
            headers={"Origin": "https://evil.example"},
        ),
    ):
        pytest.fail("cross-origin socket accepted")
    assert (
        client.post(
            API + "/login", json={"email": "owner@example.com", "password": PASSWORD}
        ).status_code
        == 403
    )


def test_expired_session_and_secure_cookie(hosted_client, monkeypatch):
    client = hosted_client
    signup(client)
    with SessionLocal() as db:
        token = db.get(Credential, auth.digest(client.cookies[auth.COOKIE]))
        token.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.get(API + "/me").status_code == 401
    monkeypatch.setenv("AGENT_PTT_PUBLIC_ORIGIN", "https://chat.example.com")
    response = client.post(
        API + "/login",
        json={"email": "owner@example.com", "password": PASSWORD},
        headers={"Origin": "https://chat.example.com"},
    )
    assert "Secure" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]


def test_login_throttle_and_invalid_origin_config(hosted_client, monkeypatch):
    # Fill the quota without doing costly password work for every attempt.
    for _ in range(15):
        auth.rate_limit("auth:testclient", 15, 300)
    assert (
        hosted_client.post(
            API + "/login", json={"email": "nobody@example.com", "password": "bad"}
        ).status_code
        == 429
    )
    monkeypatch.setenv("AGENT_PTT_PUBLIC_ORIGIN", "http://public.example.com")
    with pytest.raises(RuntimeError):
        auth.public_origin()


def test_member_in_two_organizations_can_switch_without_data_leak(hosted_client):
    owner = hosted_client
    first = signup(owner)["organizations"][0]["id"]
    second = owner.post(API + "/organizations", json={"name": "Second"}).json()["id"]
    first_room, second_room = room(owner, first), room(owner, second)
    owner.post(
        f"{API}/organizations/{first}/channels/{first_room}/messages",
        json={"text": "Only first", "client_id": "1"},
    )
    assert owner.get(f"{API}/organizations/{second}/channels/{second_room}/messages").json() == []
    assert len(owner.get(API + "/me").json()["organizations"]) == 2


def test_agent_cannot_cross_tenant_or_use_cookie_and_revocation_closes_stream(hosted_client):
    client = hosted_client
    first = signup(client)["organizations"][0]["id"]
    second = client.post(API + "/organizations", json={"name": "Other"}).json()["id"]
    first_room = room(client, first)
    agent = client.post(f"{API}/organizations/{first}/agents", json={"name": "Bot"}).json()
    bot = TestClient(app, headers={"Authorization": "Bearer " + agent["token"]})
    assert bot.get(f"{API}/organizations/{second}/channels").status_code == 404
    cookie_client = second_client()
    cookie_client.cookies.set(auth.COOKIE, agent["token"])
    assert cookie_client.get(API + "/me").status_code == 401
    with bot.websocket_connect(
        f"{API}/organizations/{first}/channels/{first_room}/stream"
    ) as socket:
        socket.receive_json()
        client.delete(f"{API}/organizations/{first}/agents/{agent['id']}")
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 4403


def test_expired_invite_and_removed_inviter_cannot_grant_access(hosted_client):
    from agent_ptt.workspace_models import Invitation

    owner = hosted_client
    org = signup(owner)["organizations"][0]["id"]
    expired = invite(owner, org)
    with SessionLocal() as db:
        db.get(Invitation, auth.digest(expired)).expires_at = now() - timedelta(seconds=1)
        db.commit()
    other = second_client()
    signup(other, "other@example.com", None)
    assert accept(other, expired).status_code == 404
    admin_invite = invite(owner, org, "admin")
    accept(other, admin_invite)
    pending = invite(other, org)
    other_id = other.get(API + "/me").json()["id"]
    owner.delete(f"{API}/organizations/{org}/members/{other_id}")
    assert accept(other, pending).status_code == 404


def test_workspace_schema_is_additive_and_database_rejects_cross_tenant_room(hosted_client):
    from sqlalchemy.exc import IntegrityError

    from agent_ptt.db import init_db
    from agent_ptt.workspace_models import ChatMessage

    client = hosted_client
    first = signup(client)["organizations"][0]["id"]
    second = client.post(API + "/organizations", json={"name": "Other"}).json()["id"]
    second_room = room(client, second)
    init_db()  # Restart/schema initialization preserves memberships and rooms.
    assert len(client.get(API + "/me").json()["organizations"]) == 2
    with SessionLocal() as db:
        db.add(
            ChatMessage(
                org_id=first,
                room_id=second_room,
                sender_id="bad",
                sender_name="Bad",
                sender_kind="agent",
                text="No",
                client_id="bad",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_invitation_revocation_and_agent_cookie_stream_rejected(hosted_client):
    client = hosted_client
    org_id = signup(client)["organizations"][0]["id"]
    base = f"{API}/organizations/{org_id}"
    token = invite(client, org_id)
    pending = client.get(base + "/invitations").json()
    assert len(pending) == 1
    assert token not in str(pending)
    assert client.delete(base + "/invitations/" + pending[0]["id"]).status_code == 204
    assert accept(client, token).status_code == 404
    agent = client.post(base + "/agents", json={"name": "Bot"}).json()
    cookie_client = second_client()
    cookie_client.cookies.set(auth.COOKIE, agent["token"])
    with (
        pytest.raises(WebSocketDisconnect),
        cookie_client.websocket_connect(base + f"/channels/{room(client, org_id)}/stream"),
    ):
        pytest.fail("Agent credential accepted as browser session")
