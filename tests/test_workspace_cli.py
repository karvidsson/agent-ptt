"""CLI enrollment persists identity before HTTP and selects channels within its tenant."""

import json
import stat

import httpx
import pytest
from typer.testing import CliRunner

from agent_ptt import workspace_cli
from agent_ptt.cli import app
from tests.test_join_links import link
from tests.test_onboarding import owner as owner_fixture
from tests.test_workspace import API, ORIGIN, second_client, signup

owner = owner_fixture
runner = CliRunner()


@pytest.fixture
def transport(owner, monkeypatch, tmp_path):
    org = signup(owner)["organizations"][0]["id"]
    token = link(owner, org)
    monkeypatch.setattr(workspace_cli, "PROFILE_DIR", tmp_path)
    client = second_client()

    def request(method, url, **kwargs):
        kwargs.pop("timeout")
        kwargs.pop("follow_redirects")
        return client.request(method, url.removeprefix(ORIGIN), **kwargs)

    monkeypatch.setattr(workspace_cli.httpx, "request", request)
    return org, f"{ORIGIN}/workspace/#join={token}", tmp_path, request


def invoke(*args):
    return runner.invoke(app, ["workspace", "--profile", "reviewer", *args])


def test_enroll_retry_join_and_send(transport, owner):
    org, url, directory, _ = transport
    result = invoke("enroll", url, "--name", "Reviewer")
    assert result.exit_code == 0, result.output
    path = directory / "reviewer.json"
    data = json.loads(path.read_text())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert data["token"] not in result.output and url not in path.read_text()
    assert invoke("enroll", url, "--name", "Reviewer").exit_code == 0
    assert len(owner.get(f"{API}/organizations/{org}/agents").json()) == 1
    assert "general" in invoke("channels").output
    assert invoke("join", "Missing").exit_code != 0
    assert invoke("join", "Review", "--create").exit_code == 0
    assert invoke("join", "Review", "--create").exit_code == 0
    rooms = owner.get(f"{API}/organizations/{org}/channels").json()
    assert len(rooms) == 2
    assert invoke("say", "Ready for review").exit_code == 0
    room = next(r for r in rooms if r["name"] == "Review")
    messages = owner.get(f"{API}/organizations/{org}/channels/{room['id']}/messages").json()
    assert messages[0]["sender_id"] == data["agent_id"]
    assert invoke("enroll", url, "--name", "Different").exit_code != 0
    other_url = f"{ORIGIN}/workspace/#join=" + "x" * 43
    assert invoke("enroll", other_url, "--name", "Reviewer").exit_code != 0


def test_lost_enrollment_response_recovers_same_identity(transport, monkeypatch, owner):
    org, url, directory, request = transport
    first = True

    def flaky(*args, **kwargs):
        nonlocal first
        result = request(*args, **kwargs)
        if first:
            first = False
            raise httpx.ReadTimeout("lost")
        return result

    monkeypatch.setattr(workspace_cli.httpx, "request", flaky)
    assert invoke("enroll", url, "--name", "Reviewer").exit_code != 0
    pending = json.loads((directory / "reviewer.json").read_text())
    assert "organization" not in pending
    assert invoke("enroll", url, "--name", "Reviewer").exit_code == 0
    data = json.loads((directory / "reviewer.json").read_text())
    assert data["token"] == pending["token"]
    assert len(owner.get(f"{API}/organizations/{org}/agents").json()) == 1


def test_run_scopes_configuration_to_child_process(transport, monkeypatch):
    _, url, directory, _ = transport
    assert invoke("enroll", url, "--name", "Reviewer").exit_code == 0
    assert invoke("join", "general").exit_code == 0
    calls = []
    monkeypatch.setattr(
        workspace_cli.subprocess, "call", lambda args, env: calls.append((args, env)) or 0
    )
    result = invoke("run", "--", "some-cli", "--flag")
    assert result.exit_code == 0, result.output
    args, env = calls[0]
    assert args == ["some-cli", "--flag"]
    assert env["AGENT_PTT_WORKSPACE_PROFILE"] == str(directory / "reviewer.json")
    assert env["AGENT_PTT_WORKSPACE_TOKEN"] not in result.output


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/workspace/#join=" + "a" * 43,
        "https://user:password@example.com/workspace/#join=" + "a" * 43,
        "https://example.com/workspace/#invite=" + "a" * 43,
    ],
)
def test_rejects_invalid_join_links(url, monkeypatch, tmp_path):
    monkeypatch.setattr(workspace_cli, "PROFILE_DIR", tmp_path)
    assert invoke("enroll", url, "--name", "Reviewer").exit_code != 0
    assert not list(tmp_path.iterdir())
