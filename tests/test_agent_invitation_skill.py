"""An agent with only the published guide/client can enroll and exchange messages."""

import io
import json
import stat
import subprocess
import sys
import urllib.error
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.test_join_links import link
from tests.test_onboarding import owner as owner_fixture
from tests.test_workspace import API, ORIGIN, second_client, signup

owner = owner_fixture


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in {"a", "link"}:
            self.links.append(dict(attrs).get("href"))


@pytest.fixture
def published(owner, tmp_path, monkeypatch):
    for key in (
        "CODEX_THREAD_ID",
        "CLAUDE_SESSION_ID",
        "AGENT_PTT_WORKSPACE_PROFILE",
        "AGENT_PTT_MODE",
    ):
        monkeypatch.delenv(key, raising=False)
    org = signup(owner)["organizations"][0]["id"]
    token = link(owner, org)
    anonymous = second_client()
    html = anonymous.get("/workspace/")
    links = Links()
    links.feed(html.text)
    assert "/workspace/SKILL.md" in links.links
    assert "/workspace/SKILL.md" in html.headers["Link"]
    guide = anonymous.get("/workspace/SKILL.md")
    assert guide.status_code == 200
    assert guide.headers["content-type"].startswith("text/markdown")
    assert "{{ORIGIN}}" not in guide.text
    assert ORIGIN + "/workspace/agent-client.py" in guide.text
    source = anonymous.get("/workspace/agent-client.py")
    assert source.status_code == 200
    path = tmp_path / "client.py"
    path.write_text(source.text)
    # Demonstrate that the downloaded client has no installed package dependencies.
    result = subprocess.run([sys.executable, "-I", "-S", str(path), "--help"], capture_output=True)
    assert result.returncode == 0, result.stderr
    namespace = {"__name__": "downloaded_client", "__file__": str(path)}
    exec(compile(source.text, str(path), "exec"), namespace)

    class Opener:
        def open(self, req, timeout):
            response = anonymous.request(
                req.method,
                req.full_url.removeprefix(ORIGIN),
                headers=dict(req.header_items()),
                content=req.data,
            )
            stream = io.BytesIO(response.content)
            stream.status = response.status_code
            if response.status_code >= 400:
                raise urllib.error.HTTPError(
                    req.full_url, response.status_code, "failed", {}, stream
                )
            return stream

    monkeypatch.setattr(namespace["urllib"].request, "build_opener", lambda *args: Opener())
    return namespace, org, ORIGIN + "/workspace/#join=" + token, tmp_path / "workspaces"


def invoke(published, *args):
    namespace, _, _, directory = published
    return namespace["main"](["--state-dir", str(directory), "--profile", "reviewer", *args])


def test_published_client_enrolls_without_human_login_then_joins_and_replies(
    published, owner, capsys
):
    _, org, url, directory = published
    assert invoke(published, "enroll", url, "--name", "Reviewer") == 0
    enrolled = json.loads(capsys.readouterr().out)
    assert enrolled["status"] == "registered" and enrolled["organization_name"] == "Acme"
    assert invoke(published, "enroll", url, "--name", "Reviewer") == 0
    capsys.readouterr()
    profile = json.loads((directory / "reviewer.json").read_text())
    assert stat.S_IMODE((directory / "reviewer.json").stat().st_mode) == 0o600
    base = f"{API}/organizations/{org}"
    assert len(owner.get(base + "/agents").json()) == 1
    assert invoke(published, "channels") == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "general"
    assert invoke(published, "join", "general") == 0
    capsys.readouterr()
    assert invoke(published, "history") == 0
    assert json.loads(capsys.readouterr().out) == []
    assert invoke(published, "join", "Invitation review", "--create") == 0
    room = json.loads(capsys.readouterr().out)["selected_channel"]
    assert invoke(published, "say", "I am reviewing invitations") == 0
    introduction = json.loads(capsys.readouterr().out)
    assert introduction["sender_id"] == enrolled["id"]
    path = base + f"/channels/{room['id']}"
    mention = owner.post(
        path + "/messages",
        json={
            "text": "Confirm you can reply",
            "client_id": "test-mention",
            "recipient_ids": [enrolled["id"]],
        },
    ).json()
    assert invoke(published, "inbox") == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == mention["id"]
    assert invoke(published, "say", "Confirmed", "--reply-to", str(mention["id"])) == 0
    assert json.loads(capsys.readouterr().out)["reply_to"] == mention["id"]
    assert invoke(published, "inbox") == 0
    assert json.loads(capsys.readouterr().out) == []
    assert profile["token"] not in json.dumps(enrolled)
    owner.delete(base + "/agents/" + enrolled["id"])
    assert invoke(published, "channels") == 1
    capsys.readouterr()
    assert invoke(published, "enroll", url, "--name", "Reviewer") == 1
    assert len(owner.get(base + "/agents").json()) == 1


def test_lost_responses_reuse_agent_and_message_identity(published, monkeypatch, capsys, owner):
    namespace, org, url, directory = published
    original = namespace["request"]
    lose = {"/join/agent"}

    def flaky(data, method, path, *args, **kwargs):
        result = original(data, method, path, *args, **kwargs)
        if path in lose:
            lose.remove(path)
            raise namespace["Failure"]("Lost response")
        return result

    monkeypatch.setitem(namespace, "request", flaky)
    assert invoke(published, "enroll", url, "--name", "Reviewer") == 1
    capsys.readouterr()
    key = json.loads((directory / "reviewer.json").read_text())["token"]
    assert invoke(published, "enroll", url, "--name", "Reviewer") == 0
    capsys.readouterr()
    assert key == json.loads((directory / "reviewer.json").read_text())["token"]
    assert len(owner.get(f"{API}/organizations/{org}/agents").json()) == 1
    assert invoke(published, "join", "general") == 0
    room = json.loads(capsys.readouterr().out)["selected_channel"]["id"]
    path = f"/organizations/{org}/channels/{room}/messages"
    lose.add(path)
    assert invoke(published, "say", "Once only") == 1
    capsys.readouterr()
    assert invoke(published, "say", "Different message") == 1
    capsys.readouterr()
    assert invoke(published, "say", "--retry") == 0
    capsys.readouterr()
    assert len(owner.get(API + path).json()) == 1


def test_no_redirect_and_no_profile_reassignment(published, capsys):
    namespace, _, url, _ = published
    assert (
        namespace["NoRedirect"]().redirect_request(None, None, 302, None, None, "https://elsewhere")
        is None
    )
    assert invoke(published, "enroll", url, "--name", "Reviewer") == 0
    capsys.readouterr()
    assert invoke(published, "enroll", url, "--name", "Other agent") == 1
    capsys.readouterr()


def test_channel_invitation_connects_and_introduces_once_after_lost_response(
    published, owner, capsys, monkeypatch
):
    from urllib.parse import urlencode

    namespace, org, url, directory = published
    base = f"{API}/organizations/{org}"
    room = owner.get(base + "/channels").json()[0]
    url += "&" + urlencode(
        {"channel": room["id"], "channel_name": "Display only", "agent": "Codex", "introduce": "1"}
    )
    original = namespace["request"]
    lost = [True]

    def flaky(data, method, path, *args, **kwargs):
        result = original(data, method, path, *args, **kwargs)
        if method == "POST" and path.endswith("/messages") and lost[0]:
            lost[0] = False
            raise namespace["Failure"]("Lost introduction response")
        return result

    monkeypatch.setitem(namespace, "request", flaky)
    assert invoke(published, "connect", url) == 1
    capsys.readouterr()
    for _ in range(2):
        assert invoke(published, "connect", url) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["name"] == "Codex" and result["selected_channel"]["id"] == room["id"]
    assert len(owner.get(base + "/agents").json()) == 1
    messages = owner.get(base + "/channels/" + room["id"] + "/messages").json()
    assert len(messages) == 1 and "Codex" in messages[0]["text"]
    assert result["introduction_message_id"] == messages[0]["id"]
    assert json.loads((directory / "reviewer.json").read_text())["channel"] == room["id"]


def test_channel_invitation_no_introduction_and_explicit_name(published, owner, capsys):
    _, org, url, _ = published
    base = f"{API}/organizations/{org}"
    room = owner.get(base + "/channels").json()[0]
    url += "&channel=" + room["id"] + "&introduce=0"
    assert invoke(published, "connect", url, "--name", "My agent") == 0
    assert json.loads(capsys.readouterr().out)["introduction_message_id"] is None
    assert owner.get(base + "/channels/" + room["id"] + "/messages").json() == []


def test_missing_invitation_channel_does_not_create_or_post(published, owner, capsys):
    _, org, url, _ = published
    url += "&channel=00000000-0000-4000-8000-000000000001&agent=Codex&introduce=1"
    assert invoke(published, "connect", url) == 1
    assert "Channel not found" in capsys.readouterr().err
    base = f"{API}/organizations/{org}"
    rooms = owner.get(base + "/channels").json()
    assert len(rooms) == 1
    assert owner.get(base + "/channels/" + rooms[0]["id"] + "/messages").json() == []


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_connect_attaches_existing_session_and_hooks_deliver_and_reply(
    published, owner, capsys, monkeypatch, harness
):
    import importlib.util

    _namespace, org, url, directory = published
    base = f"{API}/organizations/{org}"
    room = owner.get(base + "/channels").json()[0]
    url += "&channel=" + room["id"] + "&agent=Attached&introduce=0"
    session = "session-" + harness
    monkeypatch.setenv("CODEX_THREAD_ID" if harness == "codex" else "CLAUDE_SESSION_ID", session)
    assert invoke(published, "connect", url) == 0
    connected = json.loads(capsys.readouterr().out)
    assert connected["connection"]["attached"] is True
    assert connected["connection"]["mode"] == "awaiting-hook"
    assert connected["connection"]["idle_wakeup"] is False
    if harness == "claude":
        assert connected["connection"]["optional_idle_loop"].startswith("/loop 1m ")
    assert invoke(published, "connection") == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "awaiting-hook"
    file = Path(__file__).parents[1] / "plugins/codex-announcer/workspace_client.py"
    spec = importlib.util.spec_from_file_location("attached_hooks", file)
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    monkeypatch.setattr(hooks.routing, "SESSIONS_DIR", directory.parent / "workspace-sessions")
    monkeypatch.setattr(hooks.routing, "CONFIG_FILE", directory.parent / "unused.env")
    monkeypatch.setenv("AGENT_PTT_AGENT", harness.title())
    # Hook stdin, not inherited session env, must route the already-running session.
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    event = {"session_id": session, "hook_event_name": "PostToolUse"}
    hooks.routing.bind_event(event)
    assert hooks.enabled()
    message = owner.post(
        base + "/channels/" + room["id"] + "/messages",
        json={
            "text": "Can you hear me?",
            "client_id": "attach-mention",
            "recipient_ids": [connected["agent_id"]],
        },
    ).json()
    hooks.deliver(event, directory / "inboxes", lambda p, d: p.write_text(json.dumps(d)))
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "Can you hear me?" in output
    assert "--profile " in output and str(directory / "reviewer.json") in output
    assert invoke(published, "connection", "--harness", harness, "--session-id", session) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["mode"] == "activity-hooks" and status["recent"] and not status["idle_wakeup"]
    # A reply tool process can use the explicit profile without any session environment.
    hooks.routing.bind_event(None)
    monkeypatch.setenv("AGENT_PTT_WORKSPACE_PROFILE", str(directory / "reviewer.json"))
    reply = hooks.send("Yes, I can hear you", reply_to=message["id"])
    assert reply["reply_to"] == message["id"]
    monkeypatch.delenv("AGENT_PTT_WORKSPACE_PROFILE")
    hooks.routing.bind_event({"session_id": "other-session"})
    assert not hooks.enabled()
    hooks.routing.bind_event({"session_id": session, "agent_id": "subagent"})
    assert not hooks.enabled()
    assert invoke(published, "detach", "--harness", harness, "--session-id", session) == 0
    capsys.readouterr()
    hooks.routing.bind_event(event)
    assert not hooks.enabled()


def test_attach_rejects_profile_collision_and_broken_profile_fails_closed(
    published, owner, capsys, monkeypatch
):
    import importlib.util

    namespace, org, url, directory = published
    room = owner.get(f"{API}/organizations/{org}/channels").json()[0]
    url += "&channel=" + room["id"] + "&agent=Attached&introduce=0"
    assert invoke(published, "connect", url, "--harness", "codex", "--session-id", "one") == 0
    capsys.readouterr()
    assert (
        namespace["main"](
            [
                "--state-dir",
                str(directory),
                "--profile",
                "other",
                "connect",
                url,
                "--harness",
                "codex",
                "--session-id",
                "one",
            ]
        )
        == 1
    )
    assert "another profile" in capsys.readouterr().err
    spec = importlib.util.spec_from_file_location(
        "routing_attachment",
        Path(__file__).parents[1] / "plugins/codex-announcer/session_routing.py",
    )
    routing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routing)
    monkeypatch.setattr(routing, "SESSIONS_DIR", directory.parent / "workspace-sessions")
    monkeypatch.setenv("AGENT_PTT_AGENT", "Codex")
    monkeypatch.setenv("AGENT_PTT_WORKSPACE_TOKEN", "must-not-fallback")
    routing.bind_event({"session_id": "one"})
    (directory / "reviewer.json").unlink()
    assert routing.setting("AGENT_PTT_MODE", "local") == "workspace"
    assert routing.setting("AGENT_PTT_WORKSPACE_TOKEN", "") == ""


def test_inbox_timer_checkin_and_manual_endpoint(published, owner, capsys, monkeypatch):
    namespace, org, url, _ = published
    assert invoke(published, "enroll", url, "--name", "Heartbeat") == 0
    agent = json.loads(capsys.readouterr().out)["id"]
    assert invoke(published, "join", "general") == 0
    capsys.readouterr()
    base = f"{API}/organizations/{org}"

    def row():
        return next(a for a in owner.get(base + "/agents").json() if a["id"] == agent)

    assert row()["online_status"] == "unknown"
    assert invoke(published, "inbox", "--check-mode", "timer") == 0
    assert json.loads(capsys.readouterr().out) == []
    assert row()["online_status"] == "online" and row()["checkin_mode"] == "timer"
    assert invoke(published, "check-in") == 0
    assert json.loads(capsys.readouterr().out)["checkin_mode"] == "manual"
    original = namespace["request"]

    def unavailable(data, method, path, *args, **kwargs):
        if path.endswith("/check-in"):
            raise namespace["Failure"]("Unavailable", 503)
        return original(data, method, path, *args, **kwargs)

    monkeypatch.setitem(namespace, "request", unavailable)
    assert invoke(published, "inbox") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == [] and "check-in failed" in captured.err


@pytest.mark.parametrize("failed_step", ["/messages", "/inbox", "/agents/me/check-in"])
def test_connect_requires_verified_reads_and_checkin_before_introduction(
    published, owner, capsys, monkeypatch, failed_step
):
    namespace, org, url, directory = published
    base = f"{API}/organizations/{org}"
    room = owner.get(base + "/channels").json()[0]
    url += "&channel=" + room["id"] + "&agent=Claude&introduce=1"
    original = namespace["request"]

    def denied(data, method, path, *args, **kwargs):
        if path.endswith(failed_step):
            raise namespace["Failure"]("Verification unavailable", 403)
        return original(data, method, path, *args, **kwargs)

    monkeypatch.setitem(namespace, "request", denied)
    assert (
        invoke(published, "connect", url, "--harness", "claude", "--session-id", "actual-id") == 1
    )
    assert "Verification unavailable" in capsys.readouterr().err
    assert owner.get(base + "/channels/" + room["id"] + "/messages").json() == []
    identity = json.loads((directory / "reviewer.json").read_text())["agent_id"]
    monkeypatch.setitem(namespace, "request", original)
    assert (
        invoke(published, "connect", url, "--harness", "claude", "--session-id", "actual-id") == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["agent_id"] == identity
    assert result["verification"]["inbox_read"]
    assert result["verification"]["check_in"]["online_status"] == "online"
    assert result["verification"]["connection"]["mode"] == "awaiting-hook"
    assert not result["verification"]["connection"]["idle_wakeup"]
    assert len(owner.get(base + "/agents").json()) == 1
    assert len(owner.get(base + "/channels/" + room["id"] + "/messages").json()) == 1


def test_verify_resume_without_session_env_and_ignore_other_session_receipt(
    published, owner, capsys
):
    import time

    _, org, url, directory = published
    room = owner.get(f"{API}/organizations/{org}/channels").json()[0]
    assert invoke(published, "enroll", url, "--name", "Claude") == 0
    assert invoke(published, "join", room["id"]) == 0
    capsys.readouterr()
    assert invoke(published, "attach", "--harness", "claude", "--session-id", "real-session") == 0
    capsys.readouterr()
    receipt = directory / "reviewer.connection.json"
    receipt.write_text(
        json.dumps({"session_id": "real-session", "channel": room["id"], "last_check": time.time()})
    )
    for session, expected in [("real-session", "activity-hooks"), ("different-session", "manual")]:
        assert invoke(published, "verify", "--harness", "claude", "--session-id", session) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["connection"]["mode"] == expected
        assert result["history_read"] and result["inbox_read"]
        assert not result["two_way_verified"]
        assert not result["connection"]["idle_wakeup"]
    assert len(owner.get(f"{API}/organizations/{org}/agents").json()) == 1


def test_discover_profile_from_link_without_exposing_credentials(published, capsys):
    namespace, _, url, directory = published
    assert invoke(published, "enroll", url, "--name", "Claude") == 0
    capsys.readouterr()
    saved = json.loads((directory / "reviewer.json").read_text())
    # Ignore hook receipts, unrelated profiles, and malformed files.
    (directory / "reviewer.connection.json").write_text(json.dumps(saved))
    (directory / "broken.json").write_text("not json")
    (directory / "other.json").write_text(json.dumps({**saved, "enrollment": "different"}))
    assert namespace["main"](["--state-dir", str(directory), "profiles", url]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)["profiles"]
    assert len(result) == 1
    assert result[0]["profile"] == "reviewer"
    assert result[0]["agent_id"] == saved["agent_id"]
    assert saved["token"] not in output
    assert saved["enrollment"] not in output
    assert namespace["main"](["--state-dir", str(directory), "profiles", url + "&agent=Other"]) == 0
    assert json.loads(capsys.readouterr().out)["profiles"] == []


def test_name_collision_selects_stable_unique_name_even_after_lost_response(
    published, capsys, monkeypatch
):
    namespace, _, url, directory = published
    assert invoke(published, "enroll", url, "--name", "Checkout Scout") == 0
    capsys.readouterr()
    original = namespace["request"]
    lost = [True]

    def flaky(data, method, path, *args, **kwargs):
        result = original(data, method, path, *args, **kwargs)
        if path == "/join/agent" and lost[0]:
            lost[0] = False
            raise namespace["Failure"]("Lost response")
        return result

    monkeypatch.setitem(namespace, "request", flaky)
    args = [
        "--state-dir",
        str(directory),
        "--profile",
        "second",
        "enroll",
        url,
        "--name",
        "checkout scout",
    ]
    assert namespace["main"](args) == 1
    capsys.readouterr()
    saved = json.loads((directory / "second.json").read_text())
    assert saved["name"].startswith("checkout scout-")
    assert namespace["main"](args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["name"] == saved["name"]
    assert namespace["main"](args) == 0
    capsys.readouterr()
    assert json.loads((directory / "second.json").read_text())["name"] == saved["name"]


def test_peer_handoff_reply_and_retry_reach_original_agents(published, owner, capsys):
    namespace, org, url, directory = published
    room = owner.get(f"{API}/organizations/{org}/channels").json()[0]
    invitation = url + "&channel=" + room["id"] + "&introduce=0"

    def run(profile, *args):
        code = namespace["main"](["--state-dir", str(directory), "--profile", profile, *args])
        output = capsys.readouterr()
        assert code == 0, output.err
        return json.loads(output.out)

    first = run("first", "connect", invitation, "--name", "Checkout Builder")
    second = run("second", "connect", invitation, "--name", "Checkout Reviewer")
    assert len(run("first", "agents")) == 2
    sent = run(
        "first",
        "handoff",
        "--to",
        "checkout reviewer",
        "--task",
        "Review tests",
        "--context",
        "Branch checkout; tests/test_checkout.py",
        "--done-when",
        "Report test results",
        "--client-id",
        "handoff-1",
    )
    assert sent["recipient_ids"] == [second["agent_id"]]
    inbox = run("second", "inbox")
    assert inbox[0]["id"] == sent["id"] and "Done when: Report test results" in inbox[0]["text"]
    answer = run(
        "second", "say", "Accepted", "--reply-to", str(sent["id"]), "--client-id", "accept-1"
    )
    assert answer["recipient_ids"] == [first["agent_id"]]
    assert run("first", "inbox")[0]["id"] == answer["id"]
    repeated = run(
        "second", "say", "Accepted", "--reply-to", str(sent["id"]), "--client-id", "accept-1"
    )
    assert repeated["id"] == answer["id"]
    assert len(run("first", "inbox")) == 1
    assert run("second", "inbox") == []
    followup = run("first", "say", "Please include edge cases", "--reply-to", str(answer["id"]))
    assert run("second", "inbox")[0]["id"] == followup["id"]


def test_work_context_inferred_shared_updated_and_preserved(published, owner, capsys, monkeypatch):
    namespace, org, url, directory = published
    project = directory.parent / "checkout"
    project.mkdir()
    (project / "README.md").write_text("# Checkout Service\n\nPrivate details must not be sent.")
    monkeypatch.chdir(project)
    assert (
        invoke(published, "enroll", url, "--name", "Checkout Scout", "--task", "Review tests") == 0
    )
    capsys.readouterr()
    agents = owner.get(f"{API}/organizations/{org}/agents").json()
    assert agents[0]["work_context"] == {
        "project": "Checkout Service",
        "workspace": "checkout",
        "task": "Review tests",
    }
    assert str(project) not in json.dumps(agents)
    assert invoke(published, "context", "--task", "Review complete") == 0
    assert json.loads(capsys.readouterr().out)["work_context"]["task"] == "Review complete"
    assert invoke(published, "enroll", url, "--name", "Checkout Scout") == 0
    capsys.readouterr()
    assert (
        owner.get(f"{API}/organizations/{org}/agents").json()[0]["work_context"]["task"]
        == "Review complete"
    )
    assert invoke(published, "context", "--task", "") == 0
    assert json.loads(capsys.readouterr().out)["work_context"]["task"] is None
    assert (
        namespace["work_context"](__import__("argparse").Namespace(no_context=True), infer=True)
        == {}
    )
    assert (
        owner.patch(
            f"{API}/organizations/{org}/agents/me/context", json={"task": "change"}
        ).status_code
        == 403
    )
    assert invoke(published, "context", "--task", "x" * 501) == 1
    capsys.readouterr()


def test_project_channels_and_inbox_are_independent(published, owner, capsys):
    _, org, url, directory = published
    base = f"{API}/organizations/{org}"
    assert invoke(published, "enroll", url, "--name", "Project Scout", "--project", "Checkout") == 0
    agent_id = json.loads(capsys.readouterr().out)["id"]
    general = owner.get(base + "/channels").json()[0]
    mention = owner.post(
        base + "/channels/" + general["id"] + "/messages",
        json={
            "text": "Hello from general",
            "client_id": "general-request",
            "recipient_ids": [agent_id],
        },
    ).json()
    # Enrollment alone is sufficient for an organization inbox.
    assert invoke(published, "inbox") == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == mention["id"]
    assert invoke(published, "join-project") == 0
    joined = json.loads(capsys.readouterr().out)
    project = joined["selected_channel"]
    assert project["project"] == "Checkout"
    assert set(joined["channels"]) == {general["id"], project["id"]}
    assert invoke(published, "join-project") == 0
    assert json.loads(capsys.readouterr().out)["selected_channel"]["id"] == project["id"]
    assert len(owner.get(base + "/channels").json()) == 2
    assert invoke(published, "inbox") == 0
    assert json.loads(capsys.readouterr().out)[0]["channel_id"] == general["id"]
    assert (
        invoke(
            published, "say", "Hello", "--channel", general["id"], "--reply-to", str(mention["id"])
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["channel_id"] == general["id"]
    assert json.loads((directory / "reviewer.json").read_text())["channel"] == project["id"]


def test_persistent_memberships_join_leave_without_losing_other_channels(published, owner, capsys):
    _, org, url, directory = published
    assert invoke(published, "enroll", url, "--name", "Multi Channel Scout") == 0
    agent = json.loads(capsys.readouterr().out)
    assert invoke(published, "join", "general") == 0
    general = json.loads(capsys.readouterr().out)["selected_channel"]
    assert invoke(published, "join", "reviews", "--create") == 0
    reviews = json.loads(capsys.readouterr().out)["selected_channel"]
    assert invoke(published, "join", "general") == 0
    capsys.readouterr()
    assert invoke(published, "memberships") == 0
    assert {r["id"] for r in json.loads(capsys.readouterr().out)} == {general["id"], reviews["id"]}
    roster = owner.get(f"{API}/organizations/{org}/agents").json()
    assert set(next(a for a in roster if a["id"] == agent["id"])["channel_ids"]) == {
        general["id"],
        reviews["id"],
    }
    # Clear the local membership cache; a later process recovers server memberships.
    path = directory / "reviewer.json"
    data = json.loads(path.read_text())
    data["channels"] = []
    path.write_text(json.dumps(data))
    assert invoke(published, "memberships") == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert invoke(published, "leave", "reviews") == 0
    capsys.readouterr()
    assert invoke(published, "memberships") == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [general["id"]]
    assert invoke(published, "leave", "general") == 0
    capsys.readouterr()
    assert invoke(published, "inbox") == 0
    assert json.loads(capsys.readouterr().out) == []
