"""Project, inbox and work-item CLI commands (agent_ptt/project_cli.py).

httpx is faked — no server needed. A `FakeServer` answers `httpx.request`
calls by (method, path) and records them. Git runs for real in temp repos
(with the user's git config disabled) so remote/branch/toplevel detection is
exercised as it is in use.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import agent_ptt.cli as cli
import agent_ptt.project_cli as pcli

runner = CliRunner()

BASE = cli.DEFAULT_BASE_URL
PROJECT = "11111111-1111-4111-8111-111111111111"
CHANNEL = "22222222-2222-4222-8222-222222222222"
MEMBER = "33333333-3333-4333-8333-333333333333"
KEY = "secret-member-key-0123456789"
OTHER_KEY = "secret-member-key-other-9876"
ITEM = "abcdef12-0000-4000-8000-000000000001"
ITEM2 = "abcdef99-0000-4000-8000-000000000002"
ITEM3 = "12345678-0000-4000-8000-000000000003"


class FakeServer:
    def __init__(self):
        self.calls: list[dict] = []
        self.routes: dict[tuple[str, str], object] = {}
        self.fail_next = 0  # raise ConnectError this many times
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def on(self, method: str, path: str, body=None, status: int = 200):
        """Route a response; `body` may be a callable(call) -> (status, body)."""
        self.routes[(method, path)] = body if callable(body) else (lambda _c: (status, body))

    def __call__(self, method, url, headers=None, json=None, params=None, timeout=None):
        assert url.startswith(BASE), url
        call = {
            "method": method,
            "path": url[len(BASE) :],
            "headers": headers or {},
            "json": json,
            "params": params,
            "timeout": timeout,
        }
        self.calls.append(call)
        if self.fail_next:
            self.fail_next -= 1
            raise httpx.ConnectError("connection refused")
        handler = self.routes.get((method, call["path"]))
        if handler is None:
            return httpx.Response(404, json={"detail": f"no route {method} {call['path']}"})
        status, body = handler(call)
        return httpx.Response(status, json=body)

    def last(self, method: str | None = None, path: str | None = None) -> dict:
        for call in reversed(self.calls):
            if (method is None or call["method"] == method) and (
                path is None or call["path"] == path
            ):
                return call
        raise AssertionError(f"no call {method} {path}")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True, capture_output=True
    )


def _repo(path: Path, remote: str | None = None, commit: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    if remote:
        _git(path, "remote", "add", "origin", remote)
    if commit:
        _git(
            path,
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        )
    return path


def _item(item_id=ITEM, **over) -> dict:
    data = {
        "item_id": item_id,
        "project_id": PROJECT,
        "project_name": "repo",
        "kind": "instruction",
        "to": "role:reviewer",
        "from": {"member_id": "m-finn", "handle": "Finn", "harness": "claude"},
        "title": "Review the login fix",
        "body": "Review the login fix\nIt touches auth.py.",
        "context": None,
        "reply_to": None,
        "state": "delivered",
        "assignee": None,
        "lease_expires_at": None,
        "created_at": "2026-11-09T10:00:00Z",
        "updated_at": "2026-11-09T10:01:00Z",
        "last_event": {"action": "created", "by": None, "at": "2026-11-09T10:00:00Z"},
        "channel_id": CHANNEL,
        "notice_message_id": None,
        "open_questions": 0,
    }
    data.update(over)
    return data


def _join_response(call, key=KEY, member_id=MEMBER, name="repo"):
    body = call["json"]
    return 200, {
        "project": {"project_id": PROJECT, "name": name, "remote": None, "channel_id": CHANNEL},
        "member": {
            "member_id": member_id,
            "handle": body["handle"],
            "roles": body.get("roles", []),
            "mode": body.get("mode", "monitor"),
            "harness": body.get("harness"),
        },
        "member_key": key,
    }


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Temp session/members files, a clean env, git config isolated, a fake server."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cli, "SESSION_DIR", home)
    monkeypatch.setattr(cli, "SESSION_FILE", home / "session.json")
    monkeypatch.setattr(pcli, "MEMBERS_FILE", home / "members.json")
    for name in list(os.environ):
        if name.startswith(("CODEX_", "AGENT_PTT_")) or name == "CLAUDECODE":
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    server = FakeServer()
    monkeypatch.setattr(pcli.httpx, "request", server)
    monkeypatch.setattr(pcli.platform, "node", lambda: "testbox")
    monkeypatch.setattr(pcli, "_sleep", server.sleep)
    server.on("POST", "/projects/join", _join_response)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    server.home = home
    server.work = work
    return server


def run(*args: str):
    result = runner.invoke(cli.app, list(args))
    assert KEY not in result.output
    assert OTHER_KEY not in result.output
    return result


def _members(server) -> dict:
    return json.loads((server.home / "members.json").read_text())


@pytest.fixture
def joined(env):
    assert run("project", "join", "--handle", "Rex").exit_code == 0
    return env


# ---------------------------------------------------------------------------
# project join
# ---------------------------------------------------------------------------


def _cli_session(env) -> str:
    """The stable CLI session ID the join created in session.json."""
    return json.loads((env.home / "session.json").read_text())["session_id"]


def test_join_uses_git_remote_and_stores_member(env, monkeypatch):
    repo = _repo(env.work / "repo", remote="git@github.com:Owner/Repo.git")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDECODE", "1")

    result = run("project", "join", "--handle", "Rex", "--role", "reviewer", "--role", "dev")

    assert result.exit_code == 0, result.output
    body = env.last("POST", "/projects/join")["json"]
    assert body == {
        "handle": "Rex",
        "remote": "git@github.com:Owner/Repo.git",
        "roles": ["reviewer", "dev"],
        "harness": "claude",
        "machine": "testbox",
        "session_id": _cli_session(env),
        "agent": "cli",
    }
    assert "repo" in result.output and MEMBER in result.output and CHANNEL in result.output
    state = _members(env)
    entry = state[f"{BASE}|{repo.resolve()}"]
    assert entry == {
        "project_id": PROJECT,
        "project_name": "repo",
        "channel_id": CHANNEL,
        "member_id": MEMBER,
        "member_key": KEY,
        "handle": "Rex",
    }
    mode = stat.S_IMODE((env.home / "members.json").stat().st_mode)
    assert mode == 0o600


def test_join_outside_git_uses_folder_name_and_detects_codex(env, monkeypatch):
    folder = env.work / "my-folder"
    folder.mkdir()
    monkeypatch.chdir(folder)
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")

    result = run("project", "join", "--handle", "Cody", "--mode", "hooks")

    assert result.exit_code == 0, result.output
    body = env.last("POST", "/projects/join")["json"]
    assert body == {
        "handle": "Cody",
        "name": "my-folder",
        "mode": "hooks",
        "harness": "codex",
        "machine": "testbox",
        "session_id": _cli_session(env),
        "agent": "cli",
    }


def test_join_by_project_id_or_name_argument(env):
    run("project", "join", PROJECT, "--handle", "Rex", "--harness", "script")
    assert env.last("POST", "/projects/join")["json"] == {
        "handle": "Rex",
        "project_id": PROJECT,
        "harness": "script",
        "machine": "testbox",
        "session_id": _cli_session(env),
        "agent": "cli",
    }
    run("project", "join", "agent-ptt", "--handle", "Rex")
    body = env.last("POST", "/projects/join")["json"]
    assert body["name"] == "agent-ptt" and body["member_key"] == KEY


def test_rejoin_sends_stored_key_and_handle(joined):
    result = run("project", "join", "--role", "reviewer")
    assert result.exit_code == 0, result.output
    body = joined.last("POST", "/projects/join")["json"]
    assert body["member_key"] == KEY
    assert body["handle"] == "Rex"
    assert body["roles"] == ["reviewer"]


def test_rejoin_with_rejected_key_joins_fresh(joined):
    def reject_stored(call):
        if call["json"].get("member_key"):
            return 401, {"detail": "Unknown member key"}
        return _join_response(call, key=OTHER_KEY, member_id="m-new")

    joined.on("POST", "/projects/join", reject_stored)
    result = run("project", "join")
    assert result.exit_code == 0, result.output
    assert "new member" in result.output
    entry = next(iter(_members(joined).values()))
    assert entry["member_key"] == OTHER_KEY and entry["member_id"] == "m-new"


def test_first_join_needs_handle(env):
    result = run("project", "join")
    assert result.exit_code == 1
    assert "--handle" in result.output


def test_join_json_omits_member_key(env):
    result = run("project", "join", "--handle", "Rex", "--json")
    data = json.loads(result.output)
    assert "member_key" not in data
    assert data["member"]["member_id"] == MEMBER


def test_two_worktrees_are_two_members(env, monkeypatch):
    remote = "https://github.com/owner/repo"
    one = _repo(env.work / "one", remote=remote)
    two = _repo(env.work / "two", remote=remote)
    (one / "sub").mkdir()

    monkeypatch.chdir(one / "sub")  # a subdirectory still keys by the toplevel
    run("project", "join", "--handle", "Rex")
    monkeypatch.chdir(two)
    env.on("POST", "/projects/join", lambda c: _join_response(c, key=OTHER_KEY, member_id="m2"))
    run("project", "join", "--handle", "Finn")
    assert "member_key" not in env.last("POST", "/projects/join")["json"]

    state = _members(env)
    assert set(state) == {f"{BASE}|{one.resolve()}", f"{BASE}|{two.resolve()}"}

    env.on("GET", f"/projects/{PROJECT}/inbox", {"items": [], "cursor": 0})
    monkeypatch.chdir(one)
    run("inbox")
    assert env.last()["headers"]["X-Member-Key"] == KEY
    monkeypatch.chdir(two)
    run("inbox")
    assert env.last()["headers"]["X-Member-Key"] == OTHER_KEY


def test_env_override_without_join(env, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_MEMBER_KEY", OTHER_KEY)
    monkeypatch.setenv("AGENT_PTT_PROJECT_ID", "p-env")
    env.on("GET", "/projects/p-env/inbox", {"items": [], "cursor": 4})

    result = run("inbox")

    assert result.exit_code == 0, result.output
    assert env.last()["headers"]["X-Member-Key"] == OTHER_KEY
    run("inbox")
    assert env.last()["params"]["after"] == 4  # cursor kept for the env identity


def test_not_joined_error(env):
    result = run("inbox")
    assert result.exit_code == 1
    assert "agent-ptt project join" in result.output
    assert env.calls == []


def test_shared_api_key_header(joined, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_API_KEY", "shared")
    joined.on("GET", "/projects", {"projects": []})
    run("project", "list")
    assert joined.last()["headers"]["Authorization"] == "Bearer shared"


# ---------------------------------------------------------------------------
# project list / show
# ---------------------------------------------------------------------------


def test_project_list_and_show(joined):
    joined.on(
        "GET",
        "/projects",
        {
            "projects": [
                {
                    "project_id": PROJECT,
                    "name": "repo",
                    "remote": "github.com/o/repo",
                    "channel_id": CHANNEL,
                    "member_count": 2,
                    "open_items": 3,
                }
            ]
        },
    )
    result = run("project", "list")
    assert "repo" in result.output and "open 3" in result.output

    joined.on(
        "GET",
        f"/projects/{PROJECT}",
        {
            "project_id": PROJECT,
            "name": "repo",
            "remote": "github.com/o/repo",
            "channel_id": CHANNEL,
            "members": [
                {
                    "member_id": MEMBER,
                    "handle": "Rex",
                    "roles": ["reviewer"],
                    "mode": "monitor",
                    "harness": "codex",
                    "last_seen": "2026-11-09T10:00:00Z",
                    "listening": True,
                },
            ],
            "resource_claims": [
                _item(ITEM3, kind="claim", context={"resource": "branch:fix-login"})
            ],
        },
    )
    result = run("project", "show")
    assert result.exit_code == 0, result.output
    assert "Rex" in result.output and "reviewer" in result.output and "listening" in result.output
    assert "branch:fix-login" in result.output


# ---------------------------------------------------------------------------
# inbox
# ---------------------------------------------------------------------------


def test_inbox_prints_compactly_and_stores_cursor(joined):
    handoff = _item(
        ITEM,
        kind="handoff",
        context={"summary": "Login fixed", "branch": "fix-login", "repo": "r"},
        delivery={"seq": 7, "state": "delivered"},
    )
    question = _item(ITEM2, kind="question", title="Which DB?", reply_to=ITEM3)
    joined.on("GET", f"/projects/{PROJECT}/inbox", {"items": [handoff, question], "cursor": 9})

    result = run("inbox", "--wait", "20")

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("abcdef12  handoff  Finn → role:reviewer  [delivered]")
    assert "Review the login fix" in lines[0]
    assert lines[1].strip() == "Login fixed · fix-login"
    assert lines[2].startswith("abcdef99  question") and lines[2].endswith("↳ 12345678")
    call = joined.last()
    assert call["params"] == {"wait": 20, "after": 0}
    assert call["timeout"] == 30
    assert call["headers"]["X-Member-Key"] == KEY
    assert next(iter(_members(joined).values()))["cursor"] == 9

    run("inbox")
    assert joined.last()["params"] == {"wait": 0, "after": 9}
    run("inbox", "--after", "3")
    assert joined.last()["params"]["after"] == 3


def test_inbox_json_and_empty(joined):
    joined.on("GET", f"/projects/{PROJECT}/inbox", {"items": [], "cursor": 0})
    assert "Inbox empty" in run("inbox").output
    data = json.loads(run("inbox", "--json").output)
    assert data == {"items": [], "cursor": 0}


def test_rejoin_keeps_cursor(joined):
    joined.on("GET", f"/projects/{PROJECT}/inbox", {"items": [], "cursor": 5})
    run("inbox")
    run("project", "join")
    assert next(iter(_members(joined).values()))["cursor"] == 5


# ---------------------------------------------------------------------------
# send / ask / answer
# ---------------------------------------------------------------------------


def _echo_item(call):
    body = call["json"]
    return 201, _item(
        "feedbeef-0000-4000-8000-000000000009",
        kind=body["kind"],
        to=body.get("to", "agent:x"),
        title=body.get("title") or body["body"],
        reply_to=body.get("reply_to"),
        context=body.get("context"),
    )


def test_send_handoff_fills_git_context_and_client_id(env, monkeypatch):
    repo = _repo(env.work / "repo", remote="git@github.com:o/repo.git", commit=True)
    monkeypatch.chdir(repo)
    run("project", "join", "--handle", "Finn")
    env.on("POST", f"/projects/{PROJECT}/items", _echo_item)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()

    result = run(
        "send",
        "Please review",
        "--kind",
        "handoff",
        "--to",
        "role:reviewer",
        "--files",
        "a.py, b.py",
        "--summary",
        "Fixed login",
        "--link",
        "http://x/1",
    )

    assert result.exit_code == 0, result.output
    assert result.output.startswith("Sent feedbeef  handoff")
    body = env.last("POST")["json"]
    assert body["kind"] == "handoff" and body["to"] == "role:reviewer"
    assert body["context"] == {
        "repo": "git@github.com:o/repo.git",
        "branch": "main",
        "commit": head,
        "files": ["a.py", "b.py"],
        "summary": "Fixed login",
        "links": ["http://x/1"],
    }
    assert len(body["client_id"]) == 36
    assert env.last("POST")["headers"]["X-Member-Key"] == KEY


def test_send_explicit_context_and_client_id(joined):
    joined.on("POST", f"/projects/{PROJECT}/items", _echo_item)
    run(
        "send",
        "Hand over",
        "--kind",
        "handoff",
        "--to",
        "project",
        "--repo",
        "R",
        "--branch",
        "B",
        "--commit",
        "C",
        "--client-id",
        "cid-1",
        "--title",
        "T",
    )
    body = joined.last("POST")["json"]
    assert body["client_id"] == "cid-1" and body["title"] == "T"
    assert body["context"] == {"repo": "R", "branch": "B", "commit": "C"}


def test_send_note_has_no_context_and_claim_has_resource(joined):
    joined.on("POST", f"/projects/{PROJECT}/items", _echo_item)
    run("send", "FYI", "--kind", "note", "--to", "project")
    assert "context" not in joined.last("POST")["json"]
    run("send", "Taking it", "--kind", "claim", "--to", "project", "--resource", "branch:x")
    assert joined.last("POST")["json"]["context"] == {"resource": "branch:x"}


def test_send_fyi_flag(joined):
    joined.on("POST", f"/projects/{PROJECT}/items", _echo_item)
    run("send", "Released branch:x", "--kind", "note", "--to", "project")
    assert "fyi" not in joined.last("POST")["json"]
    result = run("send", "Released branch:x", "--kind", "note", "--to", "project", "--fyi")
    assert result.exit_code == 0, result.output
    assert joined.last("POST")["json"]["fyi"] is True


def test_send_retries_once_with_same_client_id(joined):
    joined.on("POST", f"/projects/{PROJECT}/items", _echo_item)
    joined.fail_next = 1
    result = run("send", "Hi", "--kind", "note", "--to", "project")
    assert result.exit_code == 0, result.output
    posts = [c for c in joined.calls if c["method"] == "POST" and c["path"].endswith("/items")]
    assert len(posts) == 2
    assert posts[0]["json"]["client_id"] == posts[1]["json"]["client_id"]


def test_server_unreachable(joined):
    joined.fail_next = 5
    result = run("send", "Hi", "--kind", "note", "--to", "project")
    assert result.exit_code == 1
    assert "cannot reach the server" in result.output


def _work_items(*items):
    return {"items": list(items), "server_time": "2026-11-09T10:05:00Z"}


def test_ask_and_answer_request_shapes(joined):
    joined.on("GET", "/work-items", _work_items(_item(ITEM), _item(ITEM3)))
    joined.on("POST", f"/projects/{PROJECT}/items", _echo_item)

    result = run("ask", "abcdef12", "Which branch?")
    assert result.exit_code == 0, result.output
    assert joined.last("GET")["params"] == {"project_id": PROJECT}
    body = joined.last("POST")["json"]
    assert {k: body[k] for k in ("kind", "body", "reply_to")} == {
        "kind": "question",
        "body": "Which branch?",
        "reply_to": ITEM,
    }
    assert "to" not in body

    run("answer", ITEM3, "main", "--to", "agent:Finn")
    body = joined.last("POST")["json"]
    assert body["kind"] == "answer" and body["reply_to"] == ITEM3 and body["to"] == "agent:Finn"
    assert "client_id" in body


# ---------------------------------------------------------------------------
# Item-ID prefixes
# ---------------------------------------------------------------------------


def test_prefix_ambiguous_and_too_short(joined):
    joined.on("GET", "/work-items", _work_items(_item(ITEM), _item(ITEM2)))
    joined.on("GET", f"/projects/{PROJECT}/inbox", {"items": [], "cursor": 0})
    result = run("claim", "abcdef")
    assert result.exit_code == 1
    assert "ambiguous" in result.output and "abcdef12" in result.output
    assert "abcdef99" in result.output

    result = run("claim", "abc")
    assert result.exit_code == 1 and "at least 6" in result.output

    result = run("claim", "999999")
    assert result.exit_code == 1 and "no item matches" in result.output


def test_prefix_falls_back_to_inbox(joined):
    joined.on("GET", "/work-items", _work_items())
    joined.on("GET", f"/projects/{PROJECT}/inbox", {"items": [_item(ITEM)], "cursor": 1})
    joined.on("POST", f"/projects/{PROJECT}/items/{ITEM}/ack", _item(ITEM, state="acked"))
    result = run("ack", "ABCDEF12")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("Acked: abcdef12")


def test_full_uuid_skips_lookup(joined):
    joined.on("POST", f"/projects/{PROJECT}/items/{ITEM}/claim", _item(ITEM, state="claimed"))
    joined.calls.clear()
    run("claim", ITEM)
    assert [c["method"] for c in joined.calls] == ["POST"]


# ---------------------------------------------------------------------------
# State changes
# ---------------------------------------------------------------------------


def test_claim_conflict_names_the_holder(joined):
    joined.on(
        "POST",
        f"/projects/{PROJECT}/items/{ITEM}/claim",
        {"detail": "Already claimed", "assignee": {"member_id": "m-rex", "handle": "Rex"}},
        status=409,
    )
    result = run("claim", ITEM)
    assert result.exit_code == 1
    assert "already claimed by Rex" in result.output


def test_unauthorized_and_forbidden(joined):
    path = f"/projects/{PROJECT}/items/{ITEM}/done"
    joined.on("POST", path, {"detail": "Unknown member key"}, status=401)
    result = run("done", ITEM)
    assert result.exit_code == 1
    assert "unauthorized" in result.output and "project join" in result.output

    joined.on("POST", path, {"detail": "Not your item"}, status=403)
    result = run("done", ITEM)
    assert result.exit_code == 1 and "forbidden: Not your item" in result.output

    joined.on("POST", path, {"detail": "Item already declined"}, status=409)
    result = run("done", ITEM)
    assert result.exit_code == 1 and "Item already declined" in result.output


@pytest.mark.parametrize(
    ("args", "suffix", "payload", "label"),
    [
        (["ack"], "ack", None, "Acked"),
        (["claim"], "claim", None, "Claimed"),
        (["release", "--note", "busy"], "release", {"note": "busy"}, "Released"),
        (["done", "--note", "merged"], "done", {"note": "merged"}, "Done"),
        (["decline"], "decline", {}, "Declined"),
        (["review"], "state", {"state": "review"}, "In review"),
        (["resume", "--note", "back"], "state", {"state": "claimed", "note": "back"}, "Resumed"),
    ],
)
def test_state_commands(joined, args, suffix, payload, label):
    path = f"/projects/{PROJECT}/items/{ITEM}/{suffix}"
    joined.on("POST", path, _item(ITEM, state="claimed", assignee={"handle": "Rex"}))
    result = run(args[0], ITEM, *args[1:])
    assert result.exit_code == 0, result.output
    call = joined.last("POST", path)
    assert call["json"] == payload
    assert call["headers"]["X-Member-Key"] == KEY
    assert result.output.startswith(f"{label}: abcdef12")
    assert "(by Rex)" in result.output


def test_state_command_json(joined):
    joined.on("POST", f"/projects/{PROJECT}/items/{ITEM}/done", _item(ITEM, state="done"))
    data = json.loads(run("done", ITEM, "--json").output)
    assert data["state"] == "done" and data["item_id"] == ITEM


# ---------------------------------------------------------------------------
# item thread
# ---------------------------------------------------------------------------


def test_item_prints_thread_and_events(joined):
    parent = _item(ITEM, state="waiting", assignee={"handle": "Rex"}, open_questions=1)
    question = _item(ITEM2, kind="question", title="Which DB?", body="Which DB?", reply_to=ITEM)
    joined.on("GET", "/work-items", _work_items(parent, question))
    joined.on(
        "GET",
        f"/projects/{PROJECT}/items/{ITEM}",
        {
            "item": parent,
            "thread": [parent, question],
            "events": [
                {
                    "action": "claimed",
                    "by": {"member_id": MEMBER, "handle": "Rex"},
                    "at": "2026-11-09T10:02:00Z",
                    "from_state": "acked",
                    "to_state": "claimed",
                    "note": None,
                },
                {
                    "action": "lease_expired",
                    "by": None,
                    "at": "2026-11-09T10:40:00Z",
                    "from_state": "claimed",
                    "to_state": "pending",
                    "note": "30 min",
                },
            ],
        },
    )

    result = run("item", "abcdef12")

    assert result.exit_code == 0, result.output
    out = result.output
    assert "▶ abcdef12  instruction" in out and "?1" in out
    assert "It touches auth.py." in out  # body shown in the thread
    assert "  abcdef99  question" in out and "↳ abcdef12" in out
    assert "2026-11-09 10:02  claimed  by Rex  (acked → claimed)" in out
    assert "lease_expired  by server  (claimed → pending)  — 30 min" in out
    assert "X-Member-Key" not in joined.last()["headers"]  # shared-auth route

    data = json.loads(run("item", ITEM, "--json").output)
    assert [t["item_id"] for t in data["thread"]] == [ITEM, ITEM2]


def test_json_output_redacts_secret_fields(joined):
    leaky = {"items": [_item(ITEM, member_key=KEY)], "cursor": 1}
    joined.on("GET", f"/projects/{PROJECT}/inbox", leaky)
    data = json.loads(run("inbox", "--json").output)
    assert "member_key" not in data["items"][0]


# ---------------------------------------------------------------------------
# Task 1: machine, doing, resume-handle, roster, presence, inbox --follow
# ---------------------------------------------------------------------------


def _member_entry(handle, member_id, **over) -> dict:
    data = {
        "member_id": member_id,
        "handle": handle,
        "roles": [],
        "mode": "hooks",
        "harness": "claude",
        "machine": None,
        "doing": None,
        "presence": "idle",
        "mention": f"@{handle}",
        "last_seen": "2026-11-10T10:00:00Z",
        "listening": False,
    }
    data.update(over)
    return data


def test_join_without_remote_in_a_worktree_uses_main_worktree_name(env, monkeypatch):
    main = _repo(env.work / "litium-brain", commit=True)
    _git(main, "worktree", "add", "-q", "-b", "side", str(env.work / "wt-side"))
    monkeypatch.chdir(env.work / "wt-side")

    assert run("project", "join", "--handle", "Theo").exit_code == 0
    assert env.last("POST", "/projects/join")["json"]["name"] == "litium-brain"
    # The membership is still keyed by this worktree, not the main one.
    assert f"{BASE}|{(env.work / 'wt-side').resolve()}" in _members(env)


def test_join_sends_machine_doing_and_resume_handle(env):
    result = run(
        "project",
        "join",
        "--handle",
        "Ember",
        "--machine",
        "krille-mac",
        "--doing",
        "  reviewing the login fix ",
        "--resume-handle",
    )
    assert result.exit_code == 0, result.output
    body = env.last("POST", "/projects/join")["json"]
    assert body["machine"] == "krille-mac"
    assert body["doing"] == "reviewing the login fix"
    assert body["resume_handle"] is True


def test_join_prints_the_roster_with_mentions(env):
    env.on(
        "GET",
        f"/projects/{PROJECT}",
        {
            "project_id": PROJECT,
            "name": "repo",
            "members": [
                _member_entry("Rex", MEMBER),
                _member_entry(
                    "Poppy Seed",
                    "m-poppy",
                    roles=["lead"],
                    machine="mac-mini",
                    doing="writing docs",
                    presence="listening",
                    mention='@"Poppy Seed"',
                ),
            ],
        },
    )
    result = run("project", "join", "--handle", "Rex")
    assert result.exit_code == 0, result.output
    assert "Also here (1):" in result.output
    line = next(ln for ln in result.output.splitlines() if "Poppy Seed" in ln)
    assert '@"Poppy Seed"' in line and "lead" in line and "mac-mini" in line
    assert "listening" in line and "writing docs" in line
    assert "  Rex  " not in result.output  # yourself is not listed


def test_join_roster_is_best_effort(env):
    # No GET route: the join still succeeds, and says nothing about members.
    result = run("project", "join", "--handle", "Rex")
    assert result.exit_code == 0, result.output
    assert "Also here" not in result.output


def test_show_prints_machine_doing_presence_and_mention(joined):
    joined.on(
        "GET",
        f"/projects/{PROJECT}",
        {
            "project_id": PROJECT,
            "name": "repo",
            "channel_id": CHANNEL,
            "members": [
                _member_entry(
                    "Rex", MEMBER, machine="kbot", doing="fixing auth", presence="active"
                ),
                _member_entry("Ember Two", "m-ember", presence="away", mention='@"Ember Two"'),
            ],
        },
    )
    result = run("project", "show")
    assert result.exit_code == 0, result.output
    rex = next(ln for ln in result.output.splitlines() if ln.strip().startswith("Rex"))
    assert "@Rex" in rex and "on kbot" in rex and "active" in rex and "fixing auth" in rex
    ember = next(ln for ln in result.output.splitlines() if "Ember Two" in ln)
    assert '@"Ember Two"' in ember and "away" in ember


def test_project_doing_patches_members_me(joined):
    joined.on(
        "PATCH",
        f"/projects/{PROJECT}/members/me",
        lambda c: (200, _member_entry("Rex", MEMBER, doing=c["json"]["doing"] or None)),
    )
    result = run("project", "doing", "reviewing PR 12")
    assert result.exit_code == 0, result.output
    call = joined.last("PATCH")
    assert call["json"] == {"doing": "reviewing PR 12"}
    assert call["headers"]["X-Member-Key"] == KEY
    assert "Doing: reviewing PR 12" in result.output

    result = run("project", "doing", "")
    assert joined.last("PATCH")["json"] == {"doing": ""}
    assert "Cleared" in result.output

    run("project", "doing", "x" * 250)
    assert joined.last("PATCH")["json"] == {"doing": "x" * 200}


def test_project_doing_on_an_old_server(joined):
    # No PATCH route: FakeServer's 404 carries a different detail, so emulate FastAPI's.
    joined.on("PATCH", f"/projects/{PROJECT}/members/me", {"detail": "Not Found"}, status=404)
    result = run("project", "doing", "x")
    assert result.exit_code == 1
    assert "does not support" in result.output


def _inbox_script(server, responses):
    """Answer inbox reads from `responses` in order, then stop the loop with Ctrl-C."""
    queue = list(responses)

    def handler(call):
        if not queue:
            raise KeyboardInterrupt
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    server.on("GET", f"/projects/{PROJECT}/inbox", handler)


def test_inbox_follow_prints_one_line_per_new_item(joined):
    first = _item(ITEM, delivery={"seq": 3, "state": "delivered"})
    second = _item(
        ITEM2, kind="question", title="Which DB?\nmore", delivery={"seq": 4, "state": "delivered"}
    )
    _inbox_script(
        joined,
        [
            (200, {"items": [first], "cursor": 3}),
            (200, {"items": [first, second], "cursor": 4}),  # first is unacked, comes back
            (200, {"items": [first, second], "cursor": 4}),
        ],
    )
    result = run("inbox", "--follow")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"[ptt] item {ITEM} instruction from Finn: Review the login fix",
        f"[ptt] item {ITEM2} question from Finn: Which DB? more",
    ]
    reads = [c for c in joined.calls if c["path"].endswith("/inbox")]
    assert [c["params"] for c in reads] == [
        {"wait": 0, "after": 0},
        {"wait": 30, "after": 3},
        {"wait": 30, "after": 4},
        {"wait": 30, "after": 4},
    ]
    assert reads[1]["timeout"] == 40
    assert next(iter(_members(joined).values()))["cursor"] == 4
    assert not any(c["path"].endswith("/ack") for c in joined.calls)


def test_inbox_follow_ack_acks_each_printed_item(joined):
    joined.on("POST", f"/projects/{PROJECT}/items/{ITEM}/ack", _item(ITEM, state="acked"))
    _inbox_script(joined, [(200, {"items": [_item(ITEM, delivery={"seq": 1})], "cursor": 1})])
    result = run("inbox", "--follow", "--ack")
    assert result.exit_code == 0, result.output
    acks = [c for c in joined.calls if c["path"].endswith("/ack")]
    assert len(acks) == 1 and acks[0]["headers"]["X-Member-Key"] == KEY


def test_inbox_follow_backs_off_on_errors(joined):
    _inbox_script(
        joined,
        [
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            (500, {"detail": "boom"}),
            (200, {"items": [_item(ITEM, delivery={"seq": 2})], "cursor": 2}),
            httpx.ConnectError("refused"),
        ],
    )
    result = run("inbox", "--follow")
    assert result.exit_code == 0, result.output
    assert joined.sleeps == [1, 2, 5, 1]  # reset after a good read
    assert f"[ptt] item {ITEM} " in result.output
    assert "retrying in 1s" in result.output


def test_inbox_follow_stops_when_the_key_is_rejected(joined):
    _inbox_script(joined, [(401, {"detail": "Unknown member key"})])
    result = run("inbox", "--follow")
    assert result.exit_code == 1
    assert "agent-ptt project join" in result.output


def test_inbox_follow_json_lines_and_ack_needs_follow(joined):
    _inbox_script(joined, [(200, {"items": [_item(ITEM, delivery={"seq": 1})], "cursor": 1})])
    result = run("inbox", "--follow", "--json")
    lines = result.output.splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["item_id"] == ITEM

    result = run("inbox", "--ack")
    assert result.exit_code == 1 and "--follow" in result.output
