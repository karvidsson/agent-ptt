"""Bounded invitations: transactional registration, isolation, and private launchers."""

from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from sqlalchemy import func, select

from agent_ptt import agent_launcher, workspace_auth
from agent_ptt.db import SessionLocal
from agent_ptt.workspace_models import Agent, Credential, Invitation, User, now
from tests.test_workspace import API, ORIGIN, PASSWORD, room, second_client, signup


@pytest.fixture
def owner(client, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_HOSTED", "1")
    monkeypatch.setenv("AGENT_PTT_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("AGENT_PTT_SIGNUP", "1")
    workspace_auth._attempts.clear()
    client.headers["Origin"] = ORIGIN
    yield client
    workspace_auth._attempts.clear()


def invitation(owner, org, channel, count=8):
    result = owner.post(
        f"{API}/organizations/{org}/invitations",
        json={"agent_limit": count, "channel_id": channel, "role": "member"},
    )
    assert result.status_code == 201, result.text
    return urlsplit(result.json()["url"]).fragment.removeprefix("invite=")


def specs(count=8):
    return [
        {"name": f"Developer agent {i}", "harness": "claude" if i % 2 else "codex"}
        for i in range(count)
    ]


def unpack_launcher(source):
    namespace = {"__name__": "test_launcher"}
    exec(compile(source, "agent-ptt-team.py", "exec"), namespace)
    return namespace["BUNDLE"]


def test_eight_agent_invitation_has_individual_scoped_credentials(owner):
    org = signup(owner)["organizations"][0]["id"]
    channel = room(owner, org)
    token = invitation(owner, org, channel)
    developer = second_client()
    developer_id = signup(developer, "developer@example.com", None)["id"]
    info = developer.post(API + "/invitations/inspect", json={"token": token}).json()
    assert info["agent_limit"] == 8 and info["organization"] == "Acme"
    response = developer.post(API + "/invitations/accept", json={"token": token, "agents": specs()})
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    bundle = unpack_launcher(response.json()["launcher"])
    assert len(bundle["agents"]) == len({a["token"] for a in bundle["agents"]}) == 8
    assert developer.get(API + "/me").json()["organizations"][0]["role"] == "member"
    assert (
        developer.post(f"{API}/organizations/{org}/agents", json={"name": "Extra"}).status_code
        == 403
    )
    assert developer.post(API + "/invitations/accept", json={"token": token}).status_code == 404
    outsider = second_client()
    other_org = signup(outsider, "outsider@example.com", "Other")["organizations"][0]["id"]
    for agent in bundle["agents"]:
        bot = second_client()
        bot.headers["Authorization"] = "Bearer " + agent["token"]
        assert bot.get(API + "/me").json()["id"] == agent["id"]
        assert bot.get(f"{API}/organizations/{org}/channels").status_code == 200
        assert bot.get(f"{API}/organizations/{other_org}/channels").status_code == 404
        assert bot.post(API + "/invitations/inspect", json={"token": token}).status_code == 403
    registered = owner.get(f"{API}/organizations/{org}/agents").json()
    assert all(a["onboarded_by"] == developer_id for a in registered)
    assert all("token" not in a for a in registered)
    with SessionLocal() as db:
        stored = set(db.scalars(select(Credential.digest)))
        assert all(a["token"] not in stored for a in bundle["agents"])
    first = bundle["agents"][0]
    owner.delete(f"{API}/organizations/{org}/agents/{first['id']}")
    bot = second_client()
    bot.headers["Authorization"] = "Bearer " + first["token"]
    assert bot.get(API + "/me").status_code == 401


def test_limits_duplicate_names_wrong_channel_and_member_cannot_invite(owner):
    org = signup(owner)["organizations"][0]["id"]
    channel = room(owner, org)
    other = second_client()
    second_org = signup(other, "other@example.com", "Other")["organizations"][0]["id"]
    invalid = owner.post(
        f"{API}/organizations/{org}/invitations",
        json={"agent_limit": 8, "channel_id": room(other, second_org)},
    )
    assert invalid.status_code == 400
    assert (
        owner.post(
            f"{API}/organizations/{org}/invitations",
            json={"agent_limit": 21, "channel_id": channel},
        ).status_code
        == 422
    )
    token = invitation(owner, org, channel, count=2)
    for agents, expected in [(specs(3), 400), ([specs(1)[0]] * 2, 422)]:
        assert (
            other.post(
                API + "/invitations/accept", json={"token": token, "agents": agents}
            ).status_code
            == expected
        )
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Agent)) == 0
        assert db.get(Invitation, workspace_auth.digest(token))
    assert (
        other.post(
            API + "/invitations/accept", json={"token": token, "agents": specs(2)}
        ).status_code
        == 200
    )
    assert (
        other.post(
            f"{API}/organizations/{org}/invitations", json={"agent_limit": 1, "channel_id": channel}
        ).status_code
        == 403
    )


def test_closed_signup_reserves_invitation_for_one_account(owner, monkeypatch):
    org = signup(owner)["organizations"][0]["id"]
    token = invitation(owner, org, room(owner, org))
    monkeypatch.setenv("AGENT_PTT_SIGNUP", "0")
    developer = second_client()
    body = {"email": "invited@example.com", "name": "Developer", "password": PASSWORD}
    assert developer.post(API + "/signup", json=body).status_code == 403
    body["invitation_token"] = token
    assert developer.post(API + "/signup", json=body).status_code == 201
    thief = second_client()
    body["email"] = "reuse@example.com"
    assert thief.post(API + "/signup", json=body).status_code == 404
    assert owner.post(API + "/invitations/accept", json={"token": token}).status_code == 404
    assert (
        developer.post(
            API + "/invitations/accept", json={"token": token, "agents": specs()}
        ).status_code
        == 200
    )
    with SessionLocal() as db:
        assert not db.scalar(select(User).where(User.email == "reuse@example.com"))


def test_revoked_and_expired_links_cannot_enroll(owner):
    org = signup(owner)["organizations"][0]["id"]
    token = invitation(owner, org, room(owner, org))
    owner.delete(f"{API}/organizations/{org}/invitations/{workspace_auth.digest(token)}")
    assert (
        owner.post(
            API + "/invitations/accept", json={"token": token, "agents": specs()}
        ).status_code
        == 404
    )
    token = invitation(owner, org, room(owner, org))
    with SessionLocal() as db:
        db.get(Invitation, workspace_auth.digest(token)).expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert owner.post(API + "/invitations/inspect", json={"token": token}).status_code == 404


def test_launcher_passes_only_selected_agent_and_preserves_argv(monkeypatch):
    bundle = {
        "url": ORIGIN,
        "organization": "org",
        "channel": "channel",
        "agents": [
            {"key": "agent-1", "name": "One", "harness": "codex", "token": "first"},
            {"key": "agent-2", "name": "Two", "harness": "claude", "token": "second"},
        ],
    }
    calls = []
    monkeypatch.setattr(
        agent_launcher.sys, "argv", ["team.py", "agent-2", "--", "claude", "a; $(literal)"]
    )
    monkeypatch.setattr(agent_launcher.os, "execvpe", lambda *args: calls.append(args))
    agent_launcher.main(bundle)
    command, argv, env = calls[0]
    assert command == "claude" and argv == ["claude", "a; $(literal)"]
    assert env["AGENT_PTT_WORKSPACE_TOKEN"] == "second"
    assert env["AGENT_PTT_MODE"] == "workspace"
    assert env["AGENT_PTT_WORKSPACE_ORG"] == "org"


def test_launcher_rejects_remote_plaintext_origin():
    with pytest.raises(ValueError):
        agent_launcher.environment({"url": "http://remote.example"}, {"token": "secret"})


def test_failed_batch_rolls_back_invitation_and_agents(owner, monkeypatch):
    from agent_ptt import workspace

    org = signup(owner)["organizations"][0]["id"]
    token = invitation(owner, org, room(owner, org))
    original = workspace.issue_credential
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Simulated registration failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(workspace, "issue_credential", fail_second)
    with pytest.raises(RuntimeError, match="Simulated"):
        owner.post(API + "/invitations/accept", json={"token": token, "agents": specs()})
    with SessionLocal() as db:
        assert db.get(Invitation, workspace_auth.digest(token))
        assert db.scalar(select(func.count()).select_from(Agent)) == 0
    monkeypatch.setattr(workspace, "issue_credential", original)
    assert (
        owner.post(
            API + "/invitations/accept", json={"token": token, "agents": specs()}
        ).status_code
        == 200
    )
