"""The announcer's collaboration scripts: session_start.py, ptt.py, doing.py and
receive.py's project delivery, run against the real API through a fake transport."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
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
    assert (
        "First, start the Monitor tool" in context.splitlines()[1]
    )  # Claude: keep a Monitor running
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


@pytest.mark.parametrize("entrypoint", ["sdk-cli", "sdk-py"])
def test_headless_child_runs_no_collaboration_hook_even_without_the_guard(
    env, capsys, monkeypatch, entrypoint
):
    """An old parent plugin spawns `claude -p` without AGENT_PTT_HOOKS=0 (task 32): Claude
    marks the run CLAUDE_CODE_ENTRYPOINT=sdk-*, and our hooks stay quiet anyway."""
    monkeypatch.delenv("AGENT_PTT_HOOKS", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entrypoint)
    event = {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/work/proj"}
    for module in (session_start, doing, receive):
        saved, sys.stdin = sys.stdin, io.StringIO(json.dumps(event))
        try:
            module.main()
        finally:
            sys.stdin = saved
    assert capsys.readouterr().out == ""
    assert env["calls"] == []


def test_headless_hooks_opt_in(monkeypatch):
    monkeypatch.delenv("AGENT_PTT_HOOKS", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    assert doing.routing.hooks_disabled()
    monkeypatch.setenv("AGENT_PTT_HEADLESS_HOOKS", "1")
    assert not doing.routing.hooks_disabled()
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.delenv("AGENT_PTT_HEADLESS_HOOKS")
    assert not doing.routing.hooks_disabled()


def test_doing_ignores_the_summarizer_prompt(monkeypatch):
    calls = []
    monkeypatch.setattr(doing.link, "request", lambda *a, **k: calls.append(a) or {})
    prompt = doing.routing.SUMMARIZER_PROMPT_PREFIX + " into what the agent says…"
    event = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": prompt}
    assert doing.update(event) is False
    assert calls == []


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
    assert f"[ptt] item {sent['item_id']} instruction from Rex: run tests" in (
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
    out, now = io.StringIO(), [100.0]
    waiter = ptt.Waiter(session, out=out, batch=4, clock=lambda: now[0])
    assert waiter.channel_once(0) == 1
    assert waiter.project_once(0) == 1
    assert out.getvalue() == "" and not waiter.due()  # batched: nothing printed yet
    now[0] += 4
    assert waiter.due() and waiter.flush() == 2  # one burst, one wake
    lines = out.getvalue().splitlines()
    handle = session.member["handle"]
    expected = f'[ptt] message {message["message_id"]} mention from User: @"{handle}" ping'
    assert lines[0] == expected
    assert lines[1] == f"[ptt] item {item['item_id']} question from Rex: why?"
    # acked: neither the waiter nor receive.py hands them out again
    assert waiter.channel_once(0) == 0 and waiter.project_once(0) == 0
    pending = env["client"].get(
        f"/channels/{state['channel_id']}/inbox", headers={"X-Participant-Key": state["key_id"]}
    )
    assert pending.json() == []


def _bare_waiter(out=None, clock=None):
    waiter = ptt.Waiter.__new__(ptt.Waiter)
    waiter.wait, waiter.out, waiter.lock = 30, out or io.StringIO(), threading.RLock()
    waiter.stopped, waiter.ended, waiter.trouble_after = None, False, ptt.TROUBLE_SECONDS
    waiter.clock = clock or (lambda: 0.0)
    return waiter


def test_wait_loop_backs_off_and_never_spins():
    waiter = _bare_waiter()
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


def test_wait_loop_failure_stops_the_whole_wait():
    waiter = _bare_waiter()
    stop = threading.Event()

    def poll(wait):
        raise ptt.Failure("no channel")

    waiter.loop(poll, stop, sleep=lambda s: pytest.fail("slept"), name="channel")
    assert stop.is_set()  # the other loop stops too: never one loop alone
    assert waiter.stopped == "the channel poll failed: no channel" and not waiter.ended


def test_wait_loop_says_once_when_failing_for_long_and_keeps_retrying():
    now = [0.0]
    waiter = _bare_waiter(clock=lambda: now[0])
    stop, sleeps = threading.Event(), []
    results = [OSError("down")] * 8 + [0]

    def poll(wait):
        result = results.pop(0)
        if not results:
            stop.set()
        if isinstance(result, Exception):
            raise result
        return result

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += 60

    waiter.loop(poll, stop, sleep=sleep, name="channel")
    lines = waiter.out.getvalue().splitlines()
    assert len(lines) == 1  # one line per failure streak
    assert lines[0].startswith("[ptt] wait: the channel poll has failed for 5 min (OSError: down)")
    assert "still retrying" in lines[0]
    assert len(sleeps) >= 8 and waiter.stopped is None  # it kept polling


def test_waiter_run_prints_one_restart_line_when_a_loop_gives_up(env, capsys):
    start(capsys=capsys)
    session = ptt.Session("s1")
    out = io.StringIO()
    waiter = ptt.Waiter(session, out=out, batch=0)
    waiter.channel_once = lambda wait: (_ for _ in ()).throw(ptt.Failure("boom"))
    waiter.project_once = lambda wait: threading.Event().wait(0.05) and 0
    waiter.run()
    lines = out.getvalue().splitlines()
    assert lines == [
        "[ptt] wait stopped: the channel poll failed: boom. Start it again with: "
        + ptt.link.script_command("ptt.py", "--session", "s1", "wait")
    ]


def test_reconnect_recovers_the_channel_name_from_the_session_pointer(env, capsys):
    start(capsys=capsys)
    session = ptt.Session("s1")
    cid = session.channel()["channel_id"]
    assert ptt.link.load_session(session.base, "s1")["channel_name"] == "proj"
    session.channel_path.unlink()  # what SessionEnd's cleanup (or an old file) leaves
    state = session.channel()
    assert state["channel_id"] == cid and state["key_id"] and state["name"] == "proj"
    # an older pointer without channel_name: the name comes from GET /channels/{id}
    session.channel_path.unlink()
    pointer = ptt.link.load_session(session.base, "s1")
    pointer.pop("channel_name")
    ptt.link.pointer_path(session.base, "s1").write_text(json.dumps(pointer))
    assert session.channel()["channel_id"] == cid
    assert any(m == "GET" and p == f"/channels/{cid}" for m, p, _ in env["calls"])


def test_wait_ends_with_the_session_and_says_not_to_restart(env, capsys):
    start(capsys=capsys)
    session = ptt.Session("s1")
    session.channel_path.unlink()
    ptt.link.save_session(session.base, "s1", ended=True)
    with pytest.raises(ptt.SessionEnded):
        session.channel()  # never rejoin a session that ended
    out = io.StringIO()
    waiter = ptt.Waiter(session, out=out, batch=0)
    waiter.run()
    line = out.getvalue().strip()
    assert line.startswith("[ptt] wait stopped: session s1 ended")
    assert "Don't restart" in line and "Start it again" not in line
    capsys.readouterr()
    ptt.cmd_wait(session, ptt.parser().parse_args(["wait"]))  # an ended session: exit at once
    assert capsys.readouterr().out.startswith("[ptt] wait stopped: session s1 ended")


def test_plugin_versions_compare_numerically():
    key = ptt.link.version_key
    assert key("0.4.10") > key("0.4.9") > key("0.4") and key("0.5") == key("0.5.0")
    assert key("v1.2.3-dev") == (1, 2, 3) and key("") == () and key(None) == ()
    people = [
        {"handle": "Old", "plugin_version": "0.4.4", "presence": "listening"},
        {"handle": "New", "plugin_version": "0.4.10", "presence": "idle"},
        {"handle": "Gone", "plugin_version": "9.0.0", "presence": "away"},
        {"handle": "None", "presence": "active"},
    ]
    assert ptt.link.stale_plugin_line("0.4.9", people) == (
        "Your Agent PTT plugin is 0.4.9, older than 0.4.10 used by @New: update it"
        " (plugins/README.md)."
    )
    assert ptt.link.stale_plugin_line("0.4.10", people) is None
    assert ptt.link.stale_plugin_line("", people) is None  # unknown: say nothing


def test_plugin_version_comes_from_the_sibling_manifest(tmp_path):
    version = json.loads((ROOT / "plugins/announcer/.claude-plugin/plugin.json").read_text())
    assert ptt.link.plugin_version() == version["version"]
    codex = json.loads((CODEX / ".codex-plugin/plugin.json").read_text())["version"]
    assert ptt.link.plugin_version(CODEX) == codex
    (tmp_path / "plugin.json").write_text(json.dumps({"version": "1.2.3"}))
    assert ptt.link.plugin_version(tmp_path) == "1.2.3"  # antigravity layout
    assert ptt.link.plugin_version(tmp_path / "missing") == ""


def test_session_start_sends_the_version_and_warns_when_older(env, capsys, monkeypatch):
    joined = env["client"].post(
        "/projects/join", json={"name": "proj", "handle": "Rex", "plugin_version": "99.0"}
    )
    assert joined.status_code == 200
    monkeypatch.setattr(session_start.link, "plugin_version", lambda here=None: "0.4.5")
    context = start(capsys=capsys)
    assert "Your Agent PTT plugin is 0.4.5, older than 99.0 used by @Rex" in context
    _, record = session_start.link.load_membership(session_start.link.base_url(), "s1")
    assert record["join"]["plugin_version"] == "0.4.5"
    members = env["client"].get(f"/projects/{record['project_id']}").json()["members"]
    assert {m["handle"]: m["plugin_version"] for m in members}[record["handle"]] == "0.4.5"
    monkeypatch.setattr(ptt.link, "plugin_version", lambda here=None: "0.4.5")
    ptt.cmd_who(ptt.Session("s1"), None)
    out = capsys.readouterr().out
    assert "@Rex" in out and "plugin 99.0" in out and "older than 99.0" in out


def test_wait_lock_is_single_instance_and_recovers_when_stale(env, capsys):
    start(capsys=capsys)
    session = ptt.Session("s1")
    lock = ptt._wait_lock(session)
    assert lock is not None and ptt._wait_lock(session) is None
    import os

    os.utime(lock, (0, 0))  # an old crashed waiter
    assert ptt._wait_lock(session) == lock


@pytest.mark.skipif(sys.platform == "win32", reason="no kill(pid, 0) probe on Windows")
def test_wait_lock_left_by_a_dead_waiter_is_stale_at_once(env, capsys):
    import subprocess

    start(capsys=capsys)
    session = ptt.Session("s1")
    lock = ptt._wait_lock(session)
    assert lock is not None and ptt._wait_lock(session) is None  # we are alive: held
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    lock.write_text(str(gone.pid))  # fresh heartbeat, but its waiter was killed
    assert ptt._wait_lock(session) == lock


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM handler test is POSIX only")
def test_sigterm_ends_wait_through_its_cleanup(env, capsys, monkeypatch):
    """A Monitor that expires sends SIGTERM; the lock must go so a re-armed wait starts."""
    import os
    import signal

    start(capsys=capsys)
    session = ptt.Session("s1")
    monkeypatch.setattr(ptt.Waiter, "run", lambda self: os.kill(os.getpid(), signal.SIGTERM))
    saved = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGHUP)}
    try:
        ptt.cmd_wait(session, ptt.parser().parse_args(["wait"]))
        assert signal.getsignal(signal.SIGTERM) is ptt._exit_on_signal
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)
    lock = ptt._wait_lock(session)
    assert lock is not None  # the killed wait left no lock behind
    lock.unlink()


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
    assert "--reply-to <item>" in context
    assert f"@Rex instruction: split the review  [item {item['item_id']}]" in context
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

    def pending(self, name, wait=0, wake_only=False):
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
    project.pointer["wakes"] = [receive.time.time()] * receive.link.wake_cap()
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
        "fyi_supported": False,
    }
    assert link.wait_support({"version": 1})["wait_supported"] is False
    assert link.wait_support(None) == {
        "wait_supported": False,
        "wait_max_seconds": 0,
        "fyi_supported": False,
    }
    assert link.wait_support({"version": 1, "fyi_supported": True})["fyi_supported"] is True


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
    assert ptt.link.item_line(item) == "[ptt] item u instruction from Rex: a b"


# --- task 4: FYIs, batching, the short preamble ----------------------------------


def _human(env, session):
    state = session.channel()
    return (
        env["client"]
        .post(f"/channels/{state['channel_id']}/join", json={"handle": "User", "voice_id": "fake"})
        .json()
    )


def _say(env, session, human, text, fyi=False):
    state = session.channel()
    return (
        env["client"]
        .post(
            f"/channels/{state['channel_id']}/say",
            json={
                "key_id": human["key_id"],
                "text": f'@"{session.member["handle"]}" {text}',
                "fyi": fyi,
            },
        )
        .json()
    )


def _item(env, pid, key, to, body, kind="question", fyi=False):
    return (
        env["client"]
        .post(
            f"/projects/{pid}/items",
            json={"kind": kind, "to": f"agent:{to}", "body": body, "fyi": fyi},
            headers={"X-Member-Key": key},
        )
        .json()
    )


def test_ptt_say_and_tell_send_fyi(env, capsys):
    other_member(env["client"], "Rex")
    start(capsys=capsys)
    assert ptt.main(["--session", "s1", "say", "done, claim released", "--fyi"]) == 0
    out = capsys.readouterr().out
    assert "[ptt] said as FYI" in out
    said = [p for _, path, p in env["calls"] if path.endswith("/say")][-1]
    assert said["fyi"] is True
    assert ptt.main(["--session", "s1", "say", "a question"]) == 0
    said = [p for _, path, p in env["calls"] if path.endswith("/say")][-1]
    assert "fyi" not in said  # a plain say still wakes
    assert ptt.main(["--session", "s1", "tell", "Rex", "noted", "--kind", "note", "--fyi"]) == 0
    assert "[ptt] sent FYI note to agent:Rex" in capsys.readouterr().out
    told = [p for _, path, p in env["calls"] if path.endswith("/items")][-1]
    assert told["fyi"] is True


def test_waiter_asks_for_waking_entries_only_when_advertised(env, capsys):
    start(capsys=capsys)
    session = ptt.Session("s1")
    ptt.link.update_channel_state(session.channel_path, fyi_supported=False)  # an older server
    waiter = ptt.Waiter(session, out=io.StringIO(), batch=0)
    waiter.channel_once(0)
    waiter.project_once(0)
    polled = [path for _, path, _ in env["calls"] if "/inbox?" in path]
    assert not any("wake_only" in path for path in polled)  # an older server
    ptt.link.update_channel_state(session.channel_path, fyi_supported=True)
    waiter.channel_once(0)
    waiter.project_once(0)
    polled = [path for _, path, _ in env["calls"] if "/inbox?" in path][-2:]
    assert all("wake_only=1" in path for path in polled)


def test_waiter_never_starts_a_wake_for_fyis_notes_or_claims(env, capsys):
    pid, _, rex_key = other_member(env["client"], "Rex")
    start(capsys=capsys)
    session = ptt.Session("s1")
    _item(env, pid, rex_key, session.member["member_id"], "fyi: tests green", kind="note")
    out = io.StringIO()
    waiter = ptt.Waiter(session, out=out, batch=0)
    assert waiter.project_once(0) == 0 and waiter.flush_at is None
    assert out.getvalue() == ""
    # still pending for the next prompt or tool hook
    inbox = env["client"].get(
        f"/projects/{pid}/inbox", headers={"X-Member-Key": session.member["member_key"]}
    )
    assert len(inbox.json()["items"]) == 1


def test_wait_once_prints_everything_pending(env, capsys):
    pid, _, rex_key = other_member(env["client"], "Rex")
    start(capsys=capsys)
    session = ptt.Session("s1")
    _item(env, pid, rex_key, session.member["member_id"], "just so you know", kind="note")
    assert ptt.main(["--session", "s1", "wait", "--once"]) == 0
    assert " note from Rex: just so you know" in capsys.readouterr().out
    assert ptt.main(["--session", "s1", "wait", "--once"]) == 0
    assert "[ptt] nothing new" in capsys.readouterr().out


def test_waiter_batches_a_wake_with_pending_fyis(env, capsys):
    pid, _, rex_key = other_member(env["client"], "Rex")
    start(capsys=capsys)
    session = ptt.Session("s1")
    assert session.channel()["fyi_supported"] is True
    human = _human(env, session)
    fyi = _say(env, session, human, "status: halfway", fyi=True)
    out, now = io.StringIO(), [0.0]
    waiter = ptt.Waiter(session, out=out, batch=4, clock=lambda: now[0])
    assert waiter.channel_once(0) == 0 and waiter.flush_at is None  # an FYI alone: no wake
    question = _say(env, session, human, "which port?")
    assert waiter.channel_once(0) == 1
    item = _item(env, pid, rex_key, session.member["member_id"], "review a.py", kind="handoff")
    assert waiter.project_once(0) == 1
    assert out.getvalue() == ""
    now[0] = 4
    assert waiter.due() and waiter.flush() == 3
    lines = out.getvalue().splitlines()
    assert " mention from User" in lines[0] and question["message_id"] in lines[0]
    assert " handoff from Rex" in lines[1] and item["item_id"] in lines[1]
    assert " FYI from User" in lines[2] and fyi["message_id"] in lines[2]
    state = session.channel()
    pending = env["client"].get(
        f"/channels/{state['channel_id']}/inbox", headers={"X-Participant-Key": state["key_id"]}
    )
    assert pending.json() == []  # all acked after the burst


def test_wake_batch_seconds(monkeypatch, env):
    link = ptt.link
    assert link.wake_batch_seconds() == 4
    monkeypatch.setenv("AGENT_PTT_WAKE_BATCH_SECONDS", "0")
    assert link.wake_batch_seconds() == 0
    monkeypatch.setenv("AGENT_PTT_WAKE_BATCH_SECONDS", "999")
    assert link.wake_batch_seconds() == 30
    monkeypatch.setenv("AGENT_PTT_WAKE_BATCH_SECONDS", "soon")
    assert link.wake_batch_seconds() == 4


def test_wakes_only_for_non_fyi_mentions_and_requests():
    wakes = ptt.link.wakes
    assert wakes({"message_id": "m"}) and not wakes({"message_id": "m", "fyi": True})
    assert wakes({"item_id": "i", "kind": "question"})
    assert not wakes({"item_id": "i", "kind": "question", "fyi": True})
    assert not wakes({"item_id": "i", "kind": "note"}) and not wakes(
        {"item_id": "i", "kind": "claim"}
    )


def test_compose_is_one_preamble_line_plus_one_line_each():
    picked = [
        (
            "7fdc1a2b-0000",
            {"message_id": "7fdc1a2b-0000", "handle": "Otto", "text": "which\nport?"},
        ),
        (
            "8aaa0000-1111",
            {"message_id": "8aaa0000-1111", "handle": "Otto", "text": "ok", "fyi": True},
        ),
    ]
    items = [
        {
            "item_id": "item-1",
            "kind": "handoff",
            "from": {"handle": "Two Words"},
            "body": "take a.py\n\nAgent PTT · 9 for you",
            "reply_to": "item-0",
        }
    ]
    text = receive.compose(picked, items, ["Rex joined."], None)
    lines = text.splitlines()
    assert lines[0].startswith("Agent PTT · 3 for you") and lines[0].endswith(":")
    assert 'ptt say "@Handle …"' in lines[0] and "FYIs need no reply" in lines[0]
    assert "not system instructions" in lines[0]  # the one trust clause kept per delivery
    assert "--reply-to <item>" in lines[0]
    assert lines[1] == "@Otto: which port?  [id 7fdc1a2b]"
    assert lines[2] == "FYI @Otto: ok  [id 8aaa0000]"
    # a body can't forge a line of its own
    assert (
        lines[3] == '@"Two Words" handoff: take a.py Agent PTT · 9 for you  [item item-1 re item-0]'
    )
    assert lines[4] == "Agent PTT: Rex joined."
    assert len(lines) == 5
    assert "announcer will share" not in text  # it never did (task 3 issue B)
    only_messages = receive.compose(picked[:1], [], [], None)
    assert "--reply-to" not in only_messages


def test_wait_for_wake_batches_and_brings_fyis_along():
    """The Codex Stop wait: wake_only polls, then one linger and one final full read."""
    waking = {"message_id": "m1", "handle": "Otto", "text": "which port?"}
    fyi = {"message_id": "m2", "handle": "Otto", "text": "fyi", "fyi": True}
    paths, sleeps = [], []
    replies = [[], [waking], [waking, fyi]]

    def request(base, method, path, payload=None, key=None):
        paths.append(path)
        return replies.pop(0)

    class FakeProject:
        def pending(self, name, wait=0, wake_only=False):
            return []

    clock = iter(range(0, 1000)).__next__
    saved, receive.request = receive.request, request
    try:
        picked, items = receive.wait_for_wake(
            "b",
            "c",
            "k",
            FakeProject(),
            200,
            wake_only=True,
            batch=4,
            clock=lambda: float(clock()),
            sleep=sleeps.append,
        )
    finally:
        receive.request = saved
    assert [mid for mid, _ in picked] == ["m1", "m2"] and items == []
    assert "wake_only=1" in paths[0] and "wake_only=1" in paths[1]
    assert "wake_only" not in paths[2] and "wait=" not in paths[2]  # the final full read
    assert 4 in sleeps


def test_wait_for_wake_ignores_fyis_from_an_older_wait():
    """Without wake_only (an older server) FYIs still come back, but never end the wait."""
    fyi = {"message_id": "m2", "handle": "Otto", "text": "fyi", "fyi": True}

    def request(base, method, path, payload=None, key=None):
        return [fyi]

    class FakeProject:
        def pending(self, name, wait=0, wake_only=False):
            return [{"item_id": "n", "kind": "note"}]

    ticks = iter(range(0, 1000, 10)).__next__
    saved, receive.request = receive.request, request
    try:
        result = receive.wait_for_wake(
            "b", "c", "k", FakeProject(), 60, clock=lambda: float(ticks()), sleep=lambda s: None
        )
    finally:
        receive.request = saved
    assert result == ([], [])


def test_stop_never_blocks_for_fyis_only_but_prompts_deliver_them(env, capsys):
    other_member(env["client"], "Rex")
    start(capsys=capsys)
    session = ptt.Session("s1")
    human = _human(env, session)
    fyi = _say(env, session, human, "status: done", fyi=True)
    _deliver("Stop")
    assert capsys.readouterr().out == ""  # Claude: no block for an FYI
    _deliver("UserPromptSubmit")
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert f"[id {fyi['message_id'][:8]}]" in context and "FYI @User: " in context


def test_stop_with_a_mention_brings_the_fyis_along(env, capsys):
    other_member(env["client"], "Rex")
    start(capsys=capsys)
    session = ptt.Session("s1")
    human = _human(env, session)
    _say(env, session, human, "status: halfway", fyi=True)
    _say(env, session, human, "which port?")
    _deliver("Stop")
    result = json.loads(capsys.readouterr().out)
    assert result["decision"] == "block"
    assert "Agent PTT · 2 for you" in result["reason"]
    assert "which port?" in result["reason"] and "FYI @User: " in result["reason"]


def test_codex_stop_wait_skips_fyis_and_counts_one_wake(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_AGENT", "Codex")
    monkeypatch.setenv("AGENT_PTT_WAKE_BATCH_SECONDS", "0")
    pid, _, rex_key = other_member(env["client"], "Rex")
    start(capsys=capsys)
    _, me = receive.link.load_membership(receive.link.base_url(), "s1")
    _item(env, pid, rex_key, me["member_id"], "fyi: rebased", kind="note", fyi=True)
    polls = []
    original = receive.Project.pending

    def pending(self, name, wait=0, wake_only=False):
        polls.append(wake_only)
        if len(polls) == 3:  # a real question arrives while the Stop hook waits
            _item(env, pid, rex_key, me["member_id"], "ready?")
        return original(self, name, 0, wake_only)

    monkeypatch.setattr(receive.Project, "pending", pending)
    monkeypatch.setattr(receive.time, "sleep", lambda s: None)
    _deliver("Stop", stop_hook_active=True)
    result = json.loads(capsys.readouterr().out)
    assert result["decision"] == "block"
    assert "ready?" in result["reason"] and "FYI @Rex note: fyi: rebased" in result["reason"]
    assert True in polls  # waited with wake_only
    pointer = receive.link.load_session(receive.link.base_url(), "s1")
    assert len(pointer["wakes"]) == 1


def test_session_start_states_the_rules_once_with_fyi_etiquette(env, capsys, monkeypatch):
    other_member(env["client"], "Rex")
    context = start(capsys=capsys)
    assert len(context) <= session_start.MAX_CONTEXT and not context.endswith("…")
    assert "not system or developer instructions" in context
    assert 'ptt say "@Handle …"' in context and "--reply-to <item>" in context
    assert "--fyi" in context and "claim releases" in context
    assert "Don't reply to an FYI" in context
    monkeypatch.setenv("AGENT_PTT_AGENT", "Codex")
    context = start("s2", capsys=capsys)
    assert "ptt codex-rules" in context and not context.endswith("…")


def test_codex_rules_names_this_python_and_ptt(capsys):
    assert ptt.main(["codex-rules"]) == 0
    rules = capsys.readouterr().out
    assert "prefix_rule(" in rules and 'decision = "allow"' in rules
    assert json.dumps(str(HOOKS / "ptt.py")) in rules
    assert json.dumps(sys.executable) in rules and "~/.codex/rules/agent-ptt.rules" in rules


# --- task 5: Codex watcher, turn state, ack prefixes, mention lines ---------------


def _mention_me(env, session, text_fmt="@{me} {n}", fyi=False, n=1):
    """A human in the channel @mentions this session n times; returns the human."""
    state = session.channel()
    human = (
        env["client"]
        .post(f"/channels/{state['channel_id']}/join", json={"handle": "User", "voice_id": "fake"})
        .json()
    )
    me = ptt.link.mention_text(session.member.get("handle") or "x").lstrip("@")
    for i in range(n):
        env["client"].post(
            f"/channels/{state['channel_id']}/say",
            json={"key_id": human["key_id"], "text": text_fmt.format(me=me, n=i), "fyi": fyi},
        )
    return human


def _watch(env, session, now):
    queued = []
    watch = ptt.CodexWatch(
        session, pid=0, clock=lambda: now[0], runner=lambda m: queued.append(m) or True
    )
    watch.batch = 0
    return watch, queued


def test_codex_watch_pokes_an_idle_session_once_and_never_acks(env, capsys):
    start(capsys=capsys)
    session = ptt.Session("s1")
    now = [1000.0]
    watch, queued = _watch(env, session, now)
    _mention_me(env, session)
    ptt.link.mark_turn(session.base, "s1", False)
    watch.step(0, lambda s: None)
    assert queued == [ptt.POKE]
    _mention_me(env, session, text_fmt="@{me} another")
    now[0] += ptt.STUCK_SECONDS + 1
    watch.step(0, lambda s: None)
    assert len(queued) == 1  # the queued poke delivers everything: never a second one
    _, pending = session.channel_call("GET", "/channels/{cid}/inbox")
    assert len(pending) == 2  # still pending: the UserPromptSubmit hook delivers and acks
    assert len(ptt.link.load_session(session.base, "s1")["wakes"]) == 1


def test_codex_watch_skips_busy_turns_and_fyis(env, capsys):
    start(capsys=capsys)
    session = ptt.Session("s1")
    now = [ptt.time.time()]
    watch, queued = _watch(env, session, now)
    _mention_me(env, session, fyi=True)
    ptt.link.mark_turn(session.base, "s1", False)
    watch.step(0, lambda s: None)
    assert queued == []  # an FYI never wakes
    _mention_me(env, session, text_fmt="@{me} real")
    ptt.link.mark_turn(session.base, "s1", True)
    watch.step(0, lambda s: None)
    assert queued == []  # busy: the hooks deliver during the turn
    ptt.link.mark_turn(session.base, "s1", False)
    watch.step(0, lambda s: None)
    assert queued == [ptt.POKE]


def test_codex_watch_honours_the_wake_cap_and_says_so_once(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_WAKE_MAX_PER_HOUR", "1")
    start(capsys=capsys)
    session = ptt.Session("s1")
    now = [ptt.time.time()]
    watch, queued = _watch(env, session, now)
    ptt.link.mark_turn(session.base, "s1", False)

    def turn_ran():  # the poke was picked up: a hook ran, the turn ended
        ptt.link.save_session(session.base, "s1", turn="idle", turn_at=now[0] + 1)
        now[0] += 2
        assert watch.alive()

    _mention_me(env, session, text_fmt="@{me} first")
    watch.step(0, lambda s: None)
    turn_ran()
    _mention_me(env, session, text_fmt="@{me} second")
    watch.step(0, lambda s: None)
    turn_ran()
    _mention_me(env, session, text_fmt="@{me} third")
    watch.step(0, lambda s: None)
    assert len(queued) == 1
    history = env["client"].get(f"/channels/{session.channel()['channel_id']}/history").json()
    notices = [m for m in history if "wake cap" in m["text"]]
    assert len(notices) == 1 and notices[0]["fyi"] and notices[0]["deliveries"] == []


def test_codex_watch_batches_close_mentions_into_one_poke(env, capsys):
    start(capsys=capsys)
    session = ptt.Session("s1")
    now = [ptt.time.time()]
    watch, queued = _watch(env, session, now)
    watch.batch = 4
    ptt.link.mark_turn(session.base, "s1", False)
    _mention_me(env, session, text_fmt="@{me} one")
    watch.step(0, lambda s: _mention_me(env, session, text_fmt="@{me} two"))
    assert len(queued) == 1
    assert len(watch.poked) == 2


def test_codex_watch_exits_with_its_codex_process(env, capsys):
    start(capsys=capsys)
    session = ptt.Session("s1")
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    assert not ptt.CodexWatch(session, pid=gone.pid).alive()
    assert ptt.CodexWatch(session, pid=os.getpid()).alive()
    ptt.link.save_session(session.base, "s1", ended=True)
    assert not ptt.CodexWatch(session, pid=os.getpid()).alive()


def test_turn_marks_never_create_a_session(env):
    ptt.link.mark_turn("http://x", "nobody", True)
    assert ptt.link.load_session("http://x", "nobody") == {}


def test_receive_marks_turns_and_skips_the_stop_wait_with_a_watcher(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_AGENT", "Codex")
    start(capsys=capsys)
    base = receive.link.base_url()
    receive.link.save_session(base, "s1", wake_route="queue")
    monkeypatch.setattr(receive.link, "watcher_running", lambda b, s: True)
    monkeypatch.setattr(receive, "wait_for_wake", lambda *a, **k: pytest.fail("waited"))
    _deliver("UserPromptSubmit")
    assert receive.link.load_session(base, "s1")["turn"] == "busy"
    _deliver("Stop")
    assert receive.link.load_session(base, "s1")["turn"] == "idle"


def test_receive_restarts_a_dead_watcher(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_AGENT", "Codex")
    start(capsys=capsys)
    base = receive.link.base_url()
    receive.link.save_session(base, "s1", wake_route="queue", codex_pid=4242)
    started = []
    monkeypatch.setattr(receive.link, "watcher_running", lambda b, s: False)
    monkeypatch.setattr(
        receive.link, "start_codex_watch", lambda b, s, pid: started.append(pid) or True
    )
    _deliver("Stop")
    assert started == [4242]


def test_session_start_tells_codex_how_it_is_woken(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_AGENT", "Codex")
    assert "only reach you for 4 min" in start(capsys=capsys)
    assert "codex queue" in session_start.codex_wake_text("queue")
    monkeypatch.setenv("AGENT_PTT_STOP_WAIT_SECONDS", "0")
    assert "can't wake you" in session_start.codex_wake_text("stop")


def test_session_start_starts_the_codex_watcher(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_AGENT", "Codex")
    started = []
    monkeypatch.setattr(session_start.link, "codex_queue_supported", lambda: True)
    monkeypatch.setattr(session_start.link, "codex_pid", lambda: 777)
    monkeypatch.setattr(
        session_start.link, "start_codex_watch", lambda b, s, pid: started.append(pid) or True
    )
    context = start(capsys=capsys)
    assert started == [777] and "codex queue" in context
    pointer = session_start.link.load_session(session_start.link.base_url(), "s1")
    assert pointer["wake_route"] == "queue" and pointer["codex_pid"] == 777


def test_wait_lines_put_the_full_id_first():
    message = {"message_id": "2e58d8e0-0d22-4948-8aaa-0123456789ab", "handle": "Poppy"}
    message["text"] = "@Willow " + "thanks " * 100
    line = ptt.link.mention_line(message)
    assert line.startswith("[ptt] message 2e58d8e0-0d22-4948-8aaa-0123456789ab mention from Poppy:")
    assert line.endswith("thanks")  # the whole message: a printed mention is acked


def test_ack_and_done_take_prefixes_and_name_the_kind(env, capsys):
    pid, _, rex_key = other_member(env["client"], "Rex")
    start(capsys=capsys)
    session = ptt.Session("s1")
    _mention_me(env, session)
    _, pending = session.channel_call("GET", "/channels/{cid}/inbox")
    mid = pending[0]["message_id"]
    me = session.member["member_id"]
    item = (
        env["client"]
        .post(
            f"/projects/{pid}/items",
            json={"kind": "instruction", "to": f"agent:{me}", "body": "do it"},
            headers={"X-Member-Key": rex_key},
        )
        .json()
    )
    item_id = item.get("item_id") or item["item"]["item_id"]
    capsys.readouterr()
    assert ptt.main(["--session", "s1", "ack", mid[:8]]) == 0
    assert f"acked mention {mid}" in capsys.readouterr().out
    ptt.link.remember_delivered(session.base, "s1", [("message", mid)])
    assert ptt.main(["--session", "s1", "ack", mid[:8]]) == 0
    assert "already delivered; mentions need no ack" in capsys.readouterr().out
    assert ptt.main(["--session", "s1", "done", item_id[:8]]) == 0
    assert f"work item {item_id}" in capsys.readouterr().out
    assert ptt.main(["--session", "s1", "ack", "zzzz"]) == 1
    assert "no pending or delivered" in capsys.readouterr().err


def test_waiter_remembers_what_it_printed(env, capsys):
    start(capsys=capsys)
    session = ptt.Session("s1")
    _mention_me(env, session)
    waiter = ptt.Waiter(session, out=io.StringIO(), batch=0)
    waiter.channel_once(0)
    delivered = ptt.link.load_session(session.base, "s1")["delivered"]
    assert delivered and delivered[0][0] == "message"


def test_claude_stop_nudges_once_to_start_the_monitor(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_MONITOR_NUDGE", "1")
    start(capsys=capsys)
    _deliver("Stop")
    out = capsys.readouterr().out
    assert '"decision": "block"' in out and "ptt.py --session s1 wait" in out
    _deliver("Stop")
    assert capsys.readouterr().out == ""  # once; no loop
    base = receive.link.base_url()
    lock = receive.link.wait_lock_path(base, "s1")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    _deliver("Stop")
    assert capsys.readouterr().out == ""  # a Monitor runs
    assert receive.link.load_session(base, "s1")["monitor_seen"]
    lock.unlink()
    receive.link.save_session(base, "s1", monitor_nudged_at=1.0)
    _deliver("Stop")
    assert "start the Monitor" in capsys.readouterr().out  # it died: remind to re-arm


def test_antigravity_stop_nudge_never_loops_when_the_agent_declines(env, capsys, monkeypatch):
    # Live (task 22): told not to start a wait, agy was held at every other Stop
    # until agy's own stop-hook limit (11 attempts).
    monkeypatch.setenv("AGENT_PTT_MONITOR_NUDGE", "1")
    monkeypatch.setenv("AGENT_PTT_AGENT", "Antigravity")
    start(capsys=capsys)
    _deliver("Stop")
    assert "--first` as a background command" in capsys.readouterr().out
    for _ in range(3):
        _deliver("Stop")
        assert capsys.readouterr().out == ""  # declined: no more nudges
    base = receive.link.base_url()
    lock = receive.link.wait_lock_path(base, "s1")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    _deliver("Stop")
    assert capsys.readouterr().out == ""  # a wait runs
    lock.unlink()  # it exited on a delivery and wasn't restarted
    _deliver("Stop")
    assert "background command" in capsys.readouterr().out  # one reminder again
    _deliver("Stop")
    assert capsys.readouterr().out == ""


def test_agy_rules_prints_and_installs_grants_idempotently(capsys, tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": ["command(date)", "command(python3 /old/agent-ptt-announcer/ptt.py)"]
                },
                "trustedWorkspaces": ["/w"],
            }
        )
    )
    monkeypatch.setenv("AGENT_PTT_AGY_SETTINGS", str(settings))
    assert ptt.main(["agy-rules"]) == 0
    printed = json.loads(capsys.readouterr().out)["permissions"]["allow"]
    here = str(HOOKS / "ptt.py")
    assert f"command({sys.executable} {here})" in printed
    assert "mcp(agent-ptt-announcer_agent-ptt/*)" in printed
    assert ptt.main(["agy-rules", "--install"]) == 0
    out = capsys.readouterr().out
    assert "removed old grant command(python3 /old/agent-ptt-announcer/ptt.py)" in out
    saved = json.loads(settings.read_text())
    assert saved["trustedWorkspaces"] == ["/w"]  # the user's settings stay
    assert saved["permissions"]["allow"][0] == "command(date)"
    assert set(printed) <= set(saved["permissions"]["allow"])
    assert ptt.main(["agy-rules", "--install"]) == 0
    assert "already allows" in capsys.readouterr().out
    settings.write_text("{not json")
    assert ptt.main(["agy-rules", "--install"]) == 1
    assert "not valid JSON" in capsys.readouterr().err
    assert settings.read_text() == "{not json"  # never overwritten


def test_antigravity_context_names_agy_rules(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_AGENT", "Antigravity")
    context = start(capsys=capsys)
    assert "agy-rules --install" in context and not context.endswith("…")


def test_ptt_say_and_tell_read_text_from_a_file(env, capsys, tmp_path):
    other_member(env["client"], "Rex")
    start(capsys=capsys)
    note = tmp_path / "msg.txt"
    note.write_text("it's `config.yaml`, not $HOME\nsecond line\n")
    assert ptt.main(["--session", "s1", "say", "--file", str(note)]) == 0
    assert ptt.main(["--session", "s1", "tell", "@Rex", "--file", str(note)]) == 0
    assert ptt.main(["--session", "s1", "say"]) == 1
    session = ptt.Session("s1")
    history = env["client"].get(f"/channels/{session.channel()['channel_id']}/history").json()
    assert any(m["text"] == "it's `config.yaml`, not $HOME\nsecond line" for m in history)


def test_watcher_poke_is_not_announced_or_set_as_doing():
    assert doing.doing_text(ptt.POKE) == ""


def test_monitor_wait_honours_the_wake_cap(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_WAKE_MAX_PER_HOUR", "1")
    start(capsys=capsys)
    session = ptt.Session("s1")
    out = io.StringIO()
    waiter = ptt.Waiter(session, out=out, batch=0)
    _mention_me(env, session, text_fmt="@{me} first")
    waiter.channel_once(0)
    _mention_me(env, session, text_fmt="@{me} second")
    waiter.channel_once(0)
    waiter.channel_once(0)
    printed = out.getvalue().splitlines()
    assert len(printed) == 1 and "first" in printed[0]
    _, pending = session.channel_call("GET", "/channels/{cid}/inbox")
    assert [m["text"].split()[-1] for m in pending] == ["second"]  # the next hook delivers it
    history = env["client"].get(f"/channels/{session.channel()['channel_id']}/history").json()
    assert sum("wake cap" in m["text"] for m in history) == 1


def test_codex_watch_gives_up_when_pokes_go_unanswered(env, capsys):
    start(capsys=capsys)
    session = ptt.Session("s1")
    now = [ptt.time.time()]
    watch, queued = _watch(env, session, now)
    ptt.link.save_session(session.base, "s1", turn="idle", turn_at=now[0] - 100)
    _mention_me(env, session)
    watch.step(0, lambda s: None)
    assert queued and watch.alive()
    now[0] += ptt.STUCK_SECONDS + 1
    assert watch.alive() and watch.stuck_noticed  # paused or closed: say so once
    history = env["client"].get(f"/channels/{session.channel()['channel_id']}/history").json()
    assert sum("isn't picking up wake-ups" in m["text"] for m in history) == 1
    now[0] += ptt.GIVE_UP_SECONDS
    assert not watch.alive()  # nobody picked the poke up: give up
    watch.unanswered_since = now[0] - 10
    ptt.link.save_session(session.base, "s1", turn="busy", turn_at=now[0])  # a hook ran
    assert watch.alive() and watch.unanswered_since is None


def test_wait_lines_carry_the_whole_message():
    message = {"message_id": "m-1", "handle": "Arlo", "text": "@Atlas " + "finding " * 60}
    line = ptt.link.mention_line(message)
    assert line.endswith("finding") and "…" not in line
    item = {"item_id": "i-1", "kind": "handoff", "from": {"handle": "Arlo"}, "title": "short"}
    item["body"] = "short\n" + "details " * 50
    assert ptt.link.item_line(item).rstrip().endswith("details")


# --- 0.6.0: server-wide handle registry ---------------------------------------------

TAKEN = {
    "error": "handle_taken",
    "handle": "Theo",
    "held_by": {"harness": "cursor", "machine": "Mac", "last_seen": "2026-11-12T08:00:00Z"},
    "detail": "Theo is held by cursor on Mac",
}


def wrap_requests(monkeypatch, hook):
    """Route every script's request through ``hook(inner, base, method, path, payload, kw)``."""
    for link in LINKS:
        inner = link.request

        def request(base, method, path, payload=None, _inner=inner, **kw):
            return hook(_inner, base, method, path, payload, kw)

        monkeypatch.setattr(link, "request", request)


def conflict(path: str, body: dict | None = None):
    data = io.BytesIO(json.dumps(body if body is not None else TAKEN).encode())
    return urllib.error.HTTPError(path, 409, "Conflict", {}, data)


def channel_joins(env):
    return [
        p for m, path, p in env["calls"] if path.startswith("/channels/") and path.endswith("/join")
    ]


def project_joins(env):
    return [p for m, path, p in env["calls"] if path == "/projects/join"]


def test_channel_join_sends_machine_and_folder(env, capsys):
    start(cwd="/work/proj", capsys=capsys)
    link = session_start.link
    (join,) = channel_joins(env)
    assert join["machine"] == link.machine() and join["folder"] == "/work/proj"
    assert join["session_id"] == "s1" and join["agent"] == "Claude"
    # ptt's reconnect uses the folder the session started in, not its own cwd
    link.channel_state_path(link.base_url(), "Claude", "s1").unlink()
    assert ptt.main(["--session", "s1", "say", "hi", "--quiet"]) == 0
    assert channel_joins(env)[-1]["folder"] == "/work/proj"


def test_project_join_sends_session_and_agent_on_join_and_rejoin(env, capsys):
    start(capsys=capsys)
    start(capsys=capsys)  # the second start rejoins with the stored member_key
    first, again = project_joins(env)
    assert "member_key" not in first and again["member_key"]
    for payload in (first, again):
        assert payload["session_id"] == "s1" and payload["agent"] == "Claude"
    assert (
        "session_id"
        not in session_start.link.load_membership(session_start.link.base_url(), "s1")[1]["join"]
    )  # sent per call, never cached for another session to reuse


def test_a_taken_pinned_name_retries_once_and_says_why(env, capsys, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_HANDLE", "Theo")
    held = {"on": True}

    def hook(inner, base, method, path, payload, kw):
        channel_join = path.endswith("/join") and path.startswith("/channels/")
        if channel_join and (payload or {}).get("pin_handle") and held["on"]:
            env["calls"].append((method, path, payload))
            raise conflict(path)
        return inner(base, method, path, payload, **kw)

    wrap_requests(monkeypatch, hook)
    context = start(capsys=capsys)
    pinned, retry = channel_joins(env)
    assert pinned["pin_handle"] is True and pinned["handle"] == "Theo"
    assert "pin_handle" not in retry and retry["handle"] == "Claude"
    assert {k: v for k, v in retry.items() if k not in ("handle",)} == {
        k: v for k, v in pinned.items() if k not in ("handle", "pin_handle")
    }
    link = session_start.link
    pointer = link.load_session(link.base_url(), "s1")
    me = pointer["handle"]
    assert me and me != "Theo"
    assert pointer["handle_refused"]["handle"] == "Theo"
    assert pointer["handle_refused"]["held_by"] == {"harness": "cursor", "machine": "Mac"}
    line = f"Your pinned name Theo is held by cursor on Mac; you are @{me} instead."
    assert line in context
    assert ptt.main(["--session", "s1", "who"]) == 0
    assert line in capsys.readouterr().out
    held["on"] = False  # the name was released: the next start clears the refusal
    context = start(capsys=capsys)
    assert "Your pinned name" not in context
    assert not link.load_session(link.base_url(), "s1").get("handle_refused")


def test_a_409_on_every_channel_join_never_loops(env, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_HANDLE", "Theo")

    def hook(inner, base, method, path, payload, kw):
        if path.endswith("/join") and path.startswith("/channels/"):
            env["calls"].append((method, path, payload))
            raise conflict(path)
        return inner(base, method, path, payload, **kw)

    wrap_requests(monkeypatch, hook)
    run_hook({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/work/proj"})
    assert len(channel_joins(env)) == 2  # the pinned join and one retry; the hook exits 0


def test_other_409s_are_not_retried(env, monkeypatch):
    calls = []

    def send(payload):
        calls.append(payload)
        raise conflict("/channels/c/join", {"detail": "something else"})

    monkeypatch.setenv("AGENT_PTT_HANDLE", "Theo")
    with pytest.raises(urllib.error.HTTPError):
        session_start.link.join_channel("http://x", "c", "Claude", "s1", "/w", send=send)
    assert len(calls) == 1


def test_handle_taken_reads_fastapi_wrapped_bodies():
    link = session_start.link
    assert link.handle_taken(conflict("/x", {"detail": TAKEN}))["handle"] == "Theo"
    assert link.handle_taken(conflict("/x", {"detail": "nope"})) is None
    assert link.refusal_line({"handle": "Theo", "held_by": {}}, "Ava") == (
        "Your pinned name Theo is held by another agent; you are @Ava instead."
    )


def test_a_project_409_continues_without_the_project(env, capsys, monkeypatch):
    def hook(inner, base, method, path, payload, kw):
        if path == "/projects/join":
            raise conflict(path)
        return inner(base, method, path, payload, **kw)

    wrap_requests(monkeypatch, hook)
    run_hook({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/work/proj"})
    out, err = capsys.readouterr()
    assert "in channel proj" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "project join refused: Theo is held by cursor on Mac" in err


def test_handle_refreshes_from_the_latest_project_join(env, capsys, monkeypatch):
    """A server-side rename (the migration) shows up at the next join."""
    start(capsys=capsys)
    link = session_start.link
    renamed = {"on": False}

    def hook(inner, base, method, path, payload, kw):
        response = inner(base, method, path, payload, **kw)
        if path == "/projects/join" and renamed["on"]:
            response["member"]["handle"] = "Theo2"
        return response

    wrap_requests(monkeypatch, hook)
    renamed["on"] = True
    context = start(capsys=capsys)
    assert "you are @Theo2" in context
    assert link.load_session(link.base_url(), "s1")["handle"] == "Theo2"
    assert ptt.main(["--session", "s1", "who"]) == 0
    assert "You are @Theo2" in capsys.readouterr().out


def test_ptt_reconnect_adopts_a_renamed_channel_handle(env, capsys, monkeypatch):
    """Without a project, the channel join's handle is the session's name."""

    def no_projects(inner, base, method, path, payload, kw):
        if path == "/projects/join":
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, io.BytesIO(b"{}"))
        response = inner(base, method, path, payload, **kw)
        if path.startswith("/channels/") and path.endswith("/join") and renamed["on"]:
            response["handle"] = "Ivy2"
        return response

    renamed = {"on": False}
    wrap_requests(monkeypatch, no_projects)
    start(capsys=capsys)
    link = session_start.link
    renamed["on"] = True
    link.channel_state_path(link.base_url(), "Claude", "s1").unlink()
    assert ptt.main(["--session", "s1", "say", "hi", "--quiet"]) == 0
    assert link.load_session(link.base_url(), "s1")["handle"] == "Ivy2"
    capsys.readouterr()
    assert ptt.main(["--session", "s1", "who"]) == 0
    assert "You are @Ivy2" in capsys.readouterr().out
