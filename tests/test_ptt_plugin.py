"""The announcer's collaboration scripts: session_start.py, ptt.py, doing.py and
receive.py's project delivery, run against the real API through a fake transport."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugins/announcer/hooks"
CODEX = ROOT / "plugins/codex-announcer"
NEW_SCRIPTS = ("project_link.py", "session_start.py", "ptt.py", "doing.py")


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"plugin_{name}", HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


session_start = load("session_start")
ptt = load("ptt")
doing = load("doing")
receive = load("receive")
LINKS = (session_start.link, ptt.link, doing.link, receive.link)


@pytest.fixture
def env(client, monkeypatch, tmp_path):
    """Every script's project_link talks to the TestClient; state lives in tmp_path."""
    calls = []

    def request(
        base, method, path, payload=None, *, member_key=None, participant_key=None, timeout=5.0
    ):
        calls.append((method, path, payload))
        headers = {}
        if member_key:
            headers["X-Member-Key"] = member_key
        if participant_key:
            headers["X-Participant-Key"] = participant_key
        response = client.request(method, path, json=payload, headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                base + path, response.status_code, "error", {}, io.BytesIO(response.content)
            )
        return response.json() if response.content else None

    def receive_request(base, method, path, payload=None, key=None):
        return request(base, method, path, payload, participant_key=key)

    for link in LINKS:
        monkeypatch.setattr(link, "request", request)
        monkeypatch.setattr(link, "PROJECTS_DIR", tmp_path / "projects")
        monkeypatch.setattr(link, "INBOX_DIR", tmp_path / "inboxes")
        monkeypatch.setattr(link, "project_identity", lambda cwd=None: {"name": "proj"})
        monkeypatch.setattr(link.routing, "CONFIG_FILE", tmp_path / "config")
        monkeypatch.setattr(link.routing, "ROUTES_DIR", tmp_path / "routes")
        monkeypatch.setattr(link.routing, "SESSIONS_DIR", tmp_path / "sessions")
        monkeypatch.setattr(link.routing, "project_name", lambda cwd=None: "proj")
    monkeypatch.setattr(receive, "STATE_DIR", tmp_path / "inboxes")
    monkeypatch.setattr(receive, "request", receive_request)
    for name in (
        "AGENT_PTT_HANDLE",
        "AGENT_PTT_ROLES",
        "AGENT_PTT_WAKE",
        "AGENT_PTT_URL",
        "AGENT_PTT_CHANNEL",
        "AGENT_PTT_MODE",
        "AGENT_PTT_RECEIVE",
        "AGENT_PTT_SESSION_ID",
        "CODEX_THREAD_ID",
        "CLAUDE_SESSION_ID",
        "AGENT_PTT_STOP_WAIT_SECONDS",
        "AGENT_PTT_WORKSPACE_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_PTT_AGENT", "Claude")
    return {"client": client, "calls": calls, "tmp": tmp_path}


def run_hook(event: dict) -> None:
    saved, sys.stdin = sys.stdin, io.StringIO(json.dumps(event))
    try:
        session_start.main()
    finally:
        sys.stdin = saved


def start(session="s1", cwd="/work/proj", capsys=None):
    run_hook({"hook_event_name": "SessionStart", "session_id": session, "cwd": cwd})
    if capsys is None:
        return None
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def other_member(client, handle="Rex", roles=None):
    joined = client.post(
        "/projects/join", json={"name": "proj", "handle": handle, "roles": roles or []}
    ).json()
    return joined["project"]["project_id"], joined["member"]["member_id"], joined["member_key"]


# --- session start ---------------------------------------------------------------


def test_session_start_joins_channel_and_project_and_injects_roster(env, capsys):
    other_member(env["client"], "Rex", ["reviewer"])
    context = start(capsys=capsys)
    assert len(context) <= session_start.MAX_CONTEXT
    assert "in project proj" in context
    assert "@Rex [reviewer]" in context
    ptt_path = str(HOOKS / "ptt.py")
    assert ptt_path in context and sys.executable in context
    assert "--session s1" in context
    assert "Monitor tool now" in context  # Claude: keep a Monitor running
    assert "introduce yourself" in context  # etiquette
    assert "~/Projects" not in context and "kbot" not in context

    link = session_start.link
    pointer = link.load_session(link.base_url(), "s1")
    assert pointer["agent"] == "Claude" and pointer["cwd"] == "/work/proj"
    _, record = link.load_membership(link.base_url(), "s1")
    assert record["member_key"] and record["join"]["mode"] == "hooks"
    assert record["join"]["harness"] == "claude" and "machine" in record["join"]
    # receive.py's channel state is shared, so the first prompt doesn't re-register
    state = json.loads(link.channel_state_path(link.base_url(), "Claude", "s1").read_text())
    assert state["key_id"] and state["name"] == "proj"
    members = env["client"].get(f"/projects/{record['project_id']}").json()["members"]
    assert {m["handle"] for m in members} == {"Rex", record["handle"]}


def test_session_start_rejoins_with_stored_key(env, capsys):
    start(capsys=capsys)
    _, first = session_start.link.load_membership(session_start.link.base_url(), "s1")
    start(capsys=capsys)
    _, again = session_start.link.load_membership(session_start.link.base_url(), "s1")
    assert again["member_id"] == first["member_id"]


def test_pinned_handle_is_keyed_by_handle_and_pins_the_channel_join(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_HANDLE", "Ember")
    monkeypatch.setenv("AGENT_PTT_ROLES", "lead, reviewer")
    context = start("s1", capsys=capsys)
    assert "you are @Ember" in context
    assert "You coordinate others" in context
    joins = [p for m, path, p in env["calls"] if path.endswith("/join") and "channels" in path]
    assert joins[0]["handle"] == "Ember" and joins[0]["pin_handle"] is True
    link = session_start.link
    _, first = link.load_membership(link.base_url(), "s1")
    assert first["roles"] == ["lead", "reviewer"]
    start("s2", capsys=capsys)  # a new session with the same pinned handle
    _, second = link.load_membership(link.base_url(), "s2")
    assert second["member_id"] == first["member_id"]


def test_codex_context_relies_on_the_stop_hook(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_AGENT", "Codex")
    context = start(capsys=capsys)
    assert "Monitor" not in context
    assert "Stop hook waits" in context


def test_session_start_is_a_noop_in_workspace_mode_and_for_subagents(env, capsys, monkeypatch):
    run_hook({"hook_event_name": "SessionStart", "session_id": "s1", "agent_id": "a"})
    monkeypatch.setenv("AGENT_PTT_MODE", "workspace")
    start(capsys=capsys)
    assert capsys.readouterr().out == ""
    assert env["calls"] == []


def test_summarizer_child_runs_no_collaboration_hook(env, capsys, monkeypatch):
    """announce.py's one-shot `claude -p` / `codex exec` loads the plugin too: it must not
    join, set doing, get inbox checks or block in the Stop wait."""
    monkeypatch.setenv("AGENT_PTT_HOOKS", "0")
    event = {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/work/proj"}
    for module in (session_start, doing, receive):
        saved, sys.stdin = sys.stdin, io.StringIO(json.dumps(event))
        try:
            module.main()
        finally:
            sys.stdin = saved
    assert capsys.readouterr().out == ""
    assert env["calls"] == []


def test_roster_orders_present_first_and_skips_me():
    members = [
        {"member_id": "a", "handle": "Away", "presence": "away"},
        {"member_id": "me", "handle": "Me", "presence": "active"},
        {"member_id": "b", "handle": "Busy", "presence": "active", "doing": "fix login"},
        {"member_id": "c", "handle": "Lis", "presence": "listening"},
    ]
    participants = [{"handle": "Busy", "state": "active"}, {"handle": "Guest", "state": "away"}]
    people = session_start.link.roster(members, participants, {"member_id": "me"})
    assert [p["handle"] for p in people] == ["Lis", "Busy", "Away", "Guest"]
    assert "doing: fix login" in session_start.link.person_line(people[1])


def test_roster_lines_fit_the_budget():
    people = [{"handle": f"Agent{i}", "presence": "active", "doing": "x" * 60} for i in range(20)]
    lines = session_start.roster_lines(people)
    assert sum(len(line) for line in lines) < session_start.ROSTER_BUDGET + 200
    assert lines[-1].startswith("- … and ")


def test_roster_changes_report_joins_and_departures():
    link = session_start.link
    before = {"a": True, "b": False}
    people = [
        {"member_id": "a", "handle": "Theo", "presence": "away", "harness": "codex"},
        {
            "member_id": "b",
            "handle": "Poppy",
            "presence": "active",
            "harness": "claude",
            "machine": "mbp",
            "doing": "review docs",
        },
    ]
    lines = link.roster_changes(before, people)
    assert lines[0].startswith("Theo (codex) went away")
    assert lines[1].startswith("Poppy (claude on mbp) joined, doing: review docs")
    assert link.roster_changes(None, people) == []


# --- ptt -------------------------------------------------------------------------


def test_ptt_tell_who_inbox_and_done(env, capsys):
    pid, _, rex_key = other_member(env["client"], "Rex")
    start(capsys=capsys)
    assert ptt.main(["--session", "s1", "tell", "@Rex", "review the login fix"]) == 0
    assert "sent instruction to agent:Rex" in capsys.readouterr().out
    inbox = env["client"].get(f"/projects/{pid}/inbox", headers={"X-Member-Key": rex_key})
    item = inbox.json()["items"][0]
    assert item["title"] == "review the login fix"

    assert ptt.main(["--session", "s1", "who"]) == 0
    assert "@Rex" in capsys.readouterr().out

    reply = env["client"].post(
        f"/projects/{pid}/items",
        json={"kind": "instruction", "to": "role:none", "body": "x"},
        headers={"X-Member-Key": rex_key},
    )
    assert reply.status_code == 201
    me = ptt.link.load_membership(ptt.link.base_url(), "s1")[1]
    sent = (
        env["client"]
        .post(
            f"/projects/{pid}/items",
            json={"kind": "instruction", "to": f"agent:{me['member_id']}", "body": "run tests"},
            headers={"X-Member-Key": rex_key},
        )
        .json()
    )
    assert ptt.main(["--session", "s1", "inbox"]) == 0
    assert f"[ptt] instruction from Rex: run tests (item {sent['item_id']})" in (
        capsys.readouterr().out
    )
    assert ptt.main(["--session", "s1", "done", sent["item_id"], "--note", "green"]) == 0
    assert (
        env["client"].get(f"/projects/{pid}/items/{sent['item_id']}").json()["item"]["state"]
        == "done"
    )


def test_ptt_finds_the_most_recent_session_for_this_folder(env, capsys, tmp_path):
    start("old", cwd=str(tmp_path / "repo"), capsys=capsys)
    start("new", cwd=str(tmp_path / "repo"), capsys=capsys)
    start("elsewhere", cwd=str(tmp_path / "other"), capsys=capsys)
    base = ptt.link.base_url()
    assert ptt.link.find_session(base, str(tmp_path / "repo" / "sub")) == "new"
    assert ptt.link.find_session(base, str(tmp_path / "other")) == "elsewhere"
    assert ptt.link.find_session(base, str(tmp_path / "nowhere")) is None


def test_ptt_say_posts_text_with_mentions_resolved_by_the_server(env, capsys):
    start(capsys=capsys)
    assert ptt.main(["--session", "s1", "say", "hello team", "--quiet"]) == 0
    assert "[ptt] said" in capsys.readouterr().out
    said = [p for m, path, p in env["calls"] if path.endswith("/say")][-1]
    assert said["text"] == "hello team" and said["speak"] is False
    assert "recipient_ids" not in said  # the server resolves @Handle in the text


def test_ptt_without_a_session_fails_clearly(env, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ptt.main(["who"]) == 1
    assert "No Agent PTT session found" in capsys.readouterr().err


def test_address_forms():
    address = ptt.link.address
    assert address("Theo") == "agent:Theo"
    assert address("@Theo") == "agent:Theo"
    assert address('@"Two Words"') == "agent:Two Words"
    assert address("role:reviewer") == "role:reviewer"
    assert address("@project") == "project"
    assert ptt.link.mention_text("Two Words") == '@"Two Words"'


# --- wait ------------------------------------------------------------------------


def test_waiter_prints_one_line_per_item_and_acks(env, capsys):
    pid, _, rex_key = other_member(env["client"], "Rex")
    start(capsys=capsys)
    session = ptt.Session("s1")
    state = session.channel()
    human = (
        env["client"]
        .post(f"/channels/{state['channel_id']}/join", json={"handle": "User", "voice_id": "fake"})
        .json()
    )
    message = (
        env["client"]
        .post(
            f"/channels/{state['channel_id']}/say",
            json={"key_id": human["key_id"], "text": f'@"{session.member["handle"]}" ping'},
        )
        .json()
    )
    item = (
        env["client"]
        .post(
            f"/projects/{pid}/items",
            json={"kind": "question", "to": f"agent:{session.member['member_id']}", "body": "why?"},
            headers={"X-Member-Key": rex_key},
        )
        .json()
    )
    out = io.StringIO()
    waiter = ptt.Waiter(session, out=out)
    assert waiter.channel_once(0) == 1
    assert waiter.project_once(0) == 1
    lines = out.getvalue().splitlines()
    handle = session.member["handle"]
    expected = f'[ptt] mention from User: @"{handle}" ping (message {message["message_id"]})'
    assert lines[0] == expected
    assert lines[1] == f"[ptt] question from Rex: why? (item {item['item_id']})"
    # acked: neither the waiter nor receive.py hands them out again
    assert waiter.channel_once(0) == 0 and waiter.project_once(0) == 0
    pending = env["client"].get(
        f"/channels/{state['channel_id']}/inbox", headers={"X-Participant-Key": state["key_id"]}
    )
    assert pending.json() == []


def test_wait_loop_backs_off_and_never_spins():
    waiter = ptt.Waiter.__new__(ptt.Waiter)
    waiter.wait = 30
    stop = threading.Event()
    sleeps, results = [], [OSError(), OSError(), OSError(), 0, 0]

    def poll(wait):
        result = results.pop(0)
        if not results:
            stop.set()
        if isinstance(result, Exception):
            raise result
        return result

    waiter.loop(poll, stop, sleep=sleeps.append)
    assert sleeps[:3] == [2.0, 4.0, 8.0]  # exponential backoff on errors
    assert all(s > ptt.MIN_CYCLE - 1 for s in sleeps[3:])  # an instant empty poll waits


def test_wait_loop_ends_without_a_project():
    waiter = ptt.Waiter.__new__(ptt.Waiter)
    waiter.wait = 30

    def poll(wait):
        raise ptt.Failure("no project")

    waiter.loop(poll, threading.Event(), sleep=lambda s: pytest.fail("slept"))


def test_wait_lock_is_single_instance_and_recovers_when_stale(env, capsys):
    start(capsys=capsys)
    session = ptt.Session("s1")
    lock = ptt._wait_lock(session)
    assert lock is not None and ptt._wait_lock(session) is None
    import os

    os.utime(lock, (0, 0))  # an old crashed waiter
    assert ptt._wait_lock(session) == lock


# --- receive: project items, roster notices, Codex Stop wait -----------------------


def _deliver(name="UserPromptSubmit", session="s1", **extra):
    receive.deliver({"hook_event_name": name, "session_id": session, "cwd": "/work/proj", **extra})


def test_receive_delivers_project_items_once_then_acks(env, capsys):
    pid, _, rex_key = other_member(env["client"], "Rex")
    start(capsys=capsys)
    _, me = receive.link.load_membership(receive.link.base_url(), "s1")
    item = (
        env["client"]
        .post(
            f"/projects/{pid}/items",
            json={
                "kind": "instruction",
                "to": f"agent:{me['member_id']}",
                "body": "split the review",
            },
            headers={"X-Member-Key": rex_key},
        )
        .json()
    )
    _deliver()
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert item["item_id"] in context and "split the review" in context
    assert "--reply-to <item_id>" in context
    assert (
        env["client"].get(f"/projects/{pid}/items/{item['item_id']}").json()["item"]["state"]
        == "acked"
    )
    _deliver()
    assert item["item_id"] not in capsys.readouterr().out


def test_receive_bootstraps_a_session_that_missed_session_start(env, capsys):
    other_member(env["client"], "Rex")
    _deliver()
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "@Rex" in context and "ptt.py" in context
    assert receive.link.load_session(receive.link.base_url(), "s1")


def test_stop_wait_seconds(monkeypatch, env):
    assert receive.stop_wait_seconds("Claude") == 0
    assert receive.stop_wait_seconds("Codex") == receive.STOP_WAIT_CODEX
    monkeypatch.setenv("AGENT_PTT_STOP_WAIT_SECONDS", "9999")
    assert receive.stop_wait_seconds("Codex") == receive.STOP_WAIT_MAX
    monkeypatch.setenv("AGENT_PTT_WAKE", "0")
    assert receive.stop_wait_seconds("Codex") == 0


def test_codex_stop_waits_for_a_work_item_and_continues(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_AGENT", "Codex")
    pid, _, rex_key = other_member(env["client"], "Rex")
    start(capsys=capsys)
    _, me = receive.link.load_membership(receive.link.base_url(), "s1")
    polls = []
    original = receive.Project.pending

    def pending(self, name, wait=0):
        polls.append(wait)
        if len(polls) == 2:  # arrives while the Stop hook is waiting
            env["client"].post(
                f"/projects/{pid}/items",
                json={"kind": "question", "to": f"agent:{me['member_id']}", "body": "ready?"},
                headers={"X-Member-Key": rex_key},
            )
        return original(self, name, 0)

    monkeypatch.setattr(receive.Project, "pending", pending)
    monkeypatch.setattr(receive.time, "sleep", lambda s: None)
    _deliver("Stop", stop_hook_active=True)
    result = json.loads(capsys.readouterr().out)
    assert result["decision"] == "block" and "ready?" in result["reason"]
    pointer = receive.link.load_session(receive.link.base_url(), "s1")
    assert len(pointer["wakes"]) == 1


def test_wake_cap_and_nobody_present_block_the_stop_wait(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_AGENT", "Codex")
    start(capsys=capsys)  # alone: nobody to wait for
    monkeypatch.setattr(receive, "wait_for_wake", lambda *a: pytest.fail("waited alone"))
    _deliver("Stop")
    assert capsys.readouterr().out == ""
    project = receive.Project(receive.link.base_url(), "s1", {})
    project.pointer["roster"] = {"x": True}
    assert project.may_wake()
    project.pointer["wakes"] = [receive.time.time()] * 6
    assert not project.may_wake()


# --- doing ------------------------------------------------------------------------


def test_doing_patches_only_when_changed(env, capsys, monkeypatch):
    start(capsys=capsys)
    patches = []
    original = doing.link.request

    def request(base, method, path, payload=None, **kw):
        if method == "PATCH":
            patches.append((path, payload, kw.get("member_key")))
            return {}
        return original(base, method, path, payload, **kw)

    monkeypatch.setattr(doing.link, "request", request)
    event = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "fix it\nmore"}
    assert doing.update(event) is True
    assert doing.update(event) is False
    assert doing.update({**event, "prompt": "<task-notification>x"}) is False
    assert len(patches) == 1
    path, payload, key = patches[0]
    assert path.endswith("/members/me") and payload == {"doing": "fix it"} and key


# --- packaging ----------------------------------------------------------------------


def test_codex_copies_are_identical_and_hooks_registered():
    for path in HOOKS.glob("*.py"):
        assert path.read_bytes() == (CODEX / path.name).read_bytes(), path.name
    claude = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
    codex = json.loads((CODEX / "hooks" / "hooks.json").read_text())["hooks"]
    for hooks in (claude, codex):
        assert "session_start.py" in json.dumps(hooks["SessionStart"])
        assert "doing.py" in json.dumps(hooks["UserPromptSubmit"])
    stop = [h for g in codex["Stop"] for h in g["hooks"] if "receive.py" in h["command"]]
    assert stop[0]["timeout"] > receive.STOP_WAIT_MAX


def test_new_scripts_are_portable():
    for name in NEW_SCRIPTS:
        text = (HOOKS / name).read_text()
        assert "import fcntl" not in text, name
        assert "kbot" not in text and "~/Projects" not in text, name


def test_wait_support_comes_from_the_mention_policy():
    link = ptt.link
    assert link.wait_support({"version": 1, "wait_supported": True, "wait_max_seconds": 60}) == {
        "wait_supported": True,
        "wait_max_seconds": 60,
    }
    assert link.wait_support({"version": 1})["wait_supported"] is False
    assert link.wait_support(None) == {"wait_supported": False, "wait_max_seconds": 0}


def test_waiter_does_not_long_poll_a_server_without_wait(env, capsys):
    start(capsys=capsys)
    session = ptt.Session("s1")
    ptt.link.update_channel_state(session.channel_path, wait_supported=False)
    ptt.Waiter(session, out=io.StringIO()).channel_once(30)
    polled = [path for _, path, _ in env["calls"] if "/inbox?" in path][-1]
    assert "wait=0" in polled


def test_ptt_say_prints_queued_items(env, capsys, monkeypatch):
    start(capsys=capsys)
    session = ptt.Session("s1")
    queued = {"item_id": "i-1", "to": "agent:m", "to_label": "agent:Theo"}
    monkeypatch.setattr(
        session,
        "channel_call",
        lambda *a, **k: ({}, {"message_id": "m1", "queued_items": [queued]}),
    )
    ptt.cmd_say(session, ptt.parser().parse_args(["say", "@Theo hi"]))
    out = capsys.readouterr().out
    assert "[ptt] queued for agent:Theo (item i-1)" in out


def test_item_line_matches_the_cli_follow_line():
    item = {"kind": "instruction", "from": {"handle": "Rex"}, "title": "a  b", "item_id": "u"}
    assert ptt.link.item_line(item) == "[ptt] instruction from Rex: a b (item u)"
