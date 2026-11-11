"""ptt_mcp.py: the ptt commands as a stdio MCP server, against the real API through the
same fake transport as test_ptt_plugin.py."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugins/announcer/hooks"
COPIES = (ROOT / "plugins/codex-announcer", ROOT / "plugins/antigravity-announcer")


def load():
    spec = importlib.util.spec_from_file_location("plugin_ptt_mcp", HOOKS / "ptt_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mcp = load()
link = mcp.link


@pytest.fixture
def env(client, monkeypatch, tmp_path):
    """project_link talks to the TestClient; state lives in tmp_path; cwd is a repo folder."""
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

    assert mcp.starter.link is link  # one project_link for ptt and session_start
    monkeypatch.setattr(link, "request", request)
    monkeypatch.setattr(link, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(link, "INBOX_DIR", tmp_path / "inboxes")
    monkeypatch.setattr(link, "project_identity", lambda cwd=None: {"name": "proj"})
    monkeypatch.setattr(link.routing, "CONFIG_FILE", tmp_path / "config")
    monkeypatch.setattr(link.routing, "ROUTES_DIR", tmp_path / "routes")
    monkeypatch.setattr(link.routing, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(link.routing, "project_name", lambda cwd=None: "proj")
    for name in (
        "AGENT_PTT_HANDLE",
        "AGENT_PTT_ROLES",
        "AGENT_PTT_WAKE",
        "AGENT_PTT_URL",
        "AGENT_PTT_API_KEY",
        "AGENT_PTT_CHANNEL",
        "AGENT_PTT_PROJECT",
        "AGENT_PTT_HARNESS",
        "AGENT_PTT_MCP_MODE",
        "AGENT_PTT_MCP",
        "AGENT_PTT_CWD",
        "AGENT_PTT_MODE",
        "AGENT_PTT_RECEIVE",
        *link.SESSION_ENV,
        "AGENT_PTT_WORKSPACE_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_PTT_AGENT", "Claude")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    return {"client": client, "calls": calls, "tmp": tmp_path, "repo": repo}


def hook_start(session="s1", cwd=None) -> str:
    """Run the real SessionStart hook; returns the injected context."""
    event = {"hook_event_name": "SessionStart", "session_id": session, "cwd": str(cwd)}
    out = io.StringIO()
    saved, sys.stdin, sys.stdout = (sys.stdin, sys.stdout), io.StringIO(json.dumps(event)), out
    try:
        mcp.starter.main()
    finally:
        sys.stdin, sys.stdout = saved
    text = out.getvalue()
    return json.loads(text)["hookSpecificOutput"]["additionalContext"] if text else ""


def rpc(server, method, params=None, msg_id=1):
    message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def call(server, name, **arguments):
    """(text, isError) of one tools/call."""
    result = rpc(server, "tools/call", {"name": name, "arguments": arguments})["result"]
    assert [c["type"] for c in result["content"]] == ["text"]
    return result["content"][0]["text"], result["isError"]


def other_member(client, handle="Rex"):
    joined = client.post("/projects/join", json={"name": "proj", "handle": handle}).json()
    return joined["project"]["project_id"], joined["member"]["member_id"], joined["member_key"]


# --- protocol ----------------------------------------------------------------------


def test_initialize_negotiates_the_protocol_version():
    server = mcp.Server()
    result = rpc(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    assert result["result"]["protocolVersion"] == "2025-06-18"
    assert result["result"]["serverInfo"]["name"] == "agent-ptt"
    assert result["result"]["capabilities"] == {"tools": {"listChanged": False}}
    newer = rpc(server, "initialize", {"protocolVersion": "2099-01-01"})["result"]
    assert newer["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]
    assert "2024-11-05" in mcp.PROTOCOL_VERSIONS


def test_notifications_ping_and_unknown_methods():
    server = mcp.Server()
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert rpc(server, "ping", msg_id=7) == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert rpc(server, "resources/list")["error"]["code"] == -32601
    assert rpc(server, "tools/call", {"name": "wait"})["error"]["code"] == -32602
    assert server.handle(["batch"])["error"]["code"] == -32600


def test_tools_list_has_every_tool_with_an_optional_session():
    tools = rpc(mcp.Server(), "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["say", "tell", "who", "inbox", "ack", "done"]
    for tool in tools:
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and "session" in schema["properties"]
        assert "session" not in schema.get("required", [])
    inbox = next(t for t in tools if t["name"] == "inbox")["description"]
    assert "ptt wait" in inbox and "Never blocks" in inbox and "standalone" in inbox
    say = next(t for t in tools if t["name"] == "say")["description"]
    assert "fyi=true only for status" in say


def test_serve_writes_only_protocol_lines():
    stdin = io.BytesIO(
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n\nnot json\n'
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
    )
    stdout = io.BytesIO()
    mcp.Server().serve(stdin, stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines == [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
    ]


def test_runs_as_a_subprocess_with_no_session(tmp_path):
    env = {
        k: v for k, v in os.environ.items() if k not in link.SESSION_ENV and "AGENT_PTT" not in k
    }
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), AGENT_PTT_URL="http://127.0.0.1:9")
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "x"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "who"}},
    ]
    done = subprocess.run(
        [sys.executable, str(HOOKS / "ptt_mcp.py")],
        input="".join(json.dumps(m) + "\n" for m in lines),
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=30,
        check=True,
    )
    answers = [json.loads(line) for line in done.stdout.splitlines()]
    assert [a["id"] for a in answers] == [1, 2, 3]
    who = answers[2]["result"]
    assert who["isError"] is True
    assert who["content"][0]["text"].startswith("ptt: No Agent PTT session found")


# --- hooked sessions -----------------------------------------------------------------


def test_tools_return_what_the_cli_prints(env):
    pid, _, rex_key = other_member(env["client"], "Rex")
    hook_start("s1", env["repo"])
    server = mcp.Server()

    text, failed = call(server, "say", text="hello @Rex", quiet=True, session="s1")
    assert not failed and text.startswith("[ptt] said (message ")
    said = [p for m, path, p in env["calls"] if path.endswith("/say")][-1]
    assert said["text"] == "hello @Rex" and said["speak"] is False

    text, failed = call(server, "tell", to="@Rex", text="review the fix", kind="question")
    assert not failed and "sent question to agent:Rex" in text

    text, failed = call(server, "who")
    assert not failed and "@Rex" in text and "Session s1 (hooked" in text

    me = link.load_membership(link.base_url(), "s1")[1]
    sent = env["client"].post(
        f"/projects/{pid}/items",
        json={"kind": "instruction", "to": f"agent:{me['member_id']}", "body": "run tests"},
        headers={"X-Member-Key": rex_key},
    )
    item_id = sent.json()["item_id"]
    text, failed = call(server, "inbox")
    assert not failed and f"[ptt] item {item_id} instruction from Rex: run tests" in text
    text, failed = call(server, "inbox")  # one read, acknowledges nothing
    assert item_id in text

    text, failed = call(server, "ack", id=item_id[:8])
    assert not failed and text == f"[ptt] acked work item {item_id}"
    # acked, it's no longer pending: the full id still finishes it (as with the CLI)
    text, failed = call(server, "done", id=item_id, note="green")
    assert not failed and text.startswith(f"[ptt] work item {item_id} done")
    item = env["client"].get(f"/projects/{pid}/items/{item_id}").json()["item"]
    assert item["state"] == "done"
    text, _ = call(server, "inbox")
    assert text == "[ptt] inbox empty"


def test_session_comes_from_the_argument_then_the_env_then_the_folder(env, monkeypatch):
    hook_start("a1", env["repo"])
    hook_start("b2", env["repo"])
    server = mcp.Server()
    assert server.resolve(None) == "b2"  # the most recent hooked session for this folder
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "a1")  # what Claude Code gives MCP servers
    assert server.resolve(None) == "a1"
    assert server.resolve("b2") == "b2"
    link.save_session(link.base_url(), "a1", ended=True)  # /clear ended it: don't use it
    assert server.resolve(None) == "b2"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "unknown-here")
    assert server.resolve(None) == "b2"


def test_errors_are_tool_results_with_the_cli_message(env, monkeypatch):
    hook_start("s1", env["repo"])
    server = mcp.Server()
    text, failed = call(server, "say", text="  ", session="s1")
    assert failed and text == "ptt: nothing to send: give the text or --file <path>"
    text, failed = call(server, "done", id="zzzz", session="s1")
    assert failed and "no pending or delivered mention or work item starts with zzzz" in text
    text, failed = call(server, "tell", to="Rex", text="x", kind="bogus", session="s1")
    assert failed and text.startswith("ptt: kind must be one of")
    text, failed = call(server, "who", session="nope")
    assert failed and text == "ptt: Session nope has no Agent PTT state on this machine."

    def refused(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(link, "request", refused)
    text, failed = call(server, "say", text="hi", session="s1")
    assert failed and text == (
        f"ptt: server unreachable at {link.base_url()}: <urlopen error connection refused>"
    )

    def forbidden(base, method, path, *args, **kwargs):
        raise urllib.error.HTTPError(path, 500, "x", {}, io.BytesIO(b'{"detail": "boom"}'))

    monkeypatch.setattr(link, "request", forbidden)
    text, failed = call(server, "tell", to="Rex", text="x", session="s1")
    assert failed and text == "ptt: tell failed: boom"


def test_a_bug_is_an_error_result_and_the_server_keeps_serving(env, monkeypatch):
    hook_start("s1", env["repo"])

    def broken(session, args):
        raise KeyError("oops")

    monkeypatch.setitem(mcp.ptt.COMMANDS, "who", broken)
    server = mcp.Server()
    text, failed = call(server, "who", session="s1")
    assert failed and text.startswith("ptt: internal error: KeyError")
    assert rpc(server, "ping")["result"] == {}


def test_hooked_mode_never_joins_by_itself(env, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_MCP_MODE", "hooked")
    monkeypatch.setenv("AGENT_PTT_HANDLE", "Pinned")
    text, failed = call(mcp.Server(), "who")
    assert failed and "No Agent PTT session found" in text
    assert not any(path.endswith("/join") for _, path, _ in env["calls"])


def test_session_start_mentions_the_mcp_tools_only_when_shipped(env, monkeypatch, tmp_path):
    context = hook_start("s1", env["repo"])
    assert (HOOKS.parent / ".mcp.json").is_file()
    assert "MCP tools agent-ptt say/tell/who/inbox/ack/done also work; pass session=s1." in (
        context
    )
    assert len(context) <= mcp.starter.MAX_CONTEXT
    monkeypatch.setenv("AGENT_PTT_MCP", "0")
    assert "MCP tools" not in hook_start("s2", env["repo"])


# --- standalone (hookless) sessions --------------------------------------------------


def test_standalone_joins_on_first_use_with_a_stable_session(env, monkeypatch):
    pid, _, rex_key = other_member(env["client"], "Rex")
    monkeypatch.setenv("AGENT_PTT_HANDLE", "Cursy")
    monkeypatch.setenv("AGENT_PTT_HARNESS", "cursor")
    server = mcp.Server()
    text, failed = call(server, "who")
    assert not failed and "You are @Cursy in proj." in text and "@Rex" in text
    sid = mcp.standalone_id(link.base_url(), str(env["repo"].resolve()))
    assert sid.startswith("mcp-") and f"Session {sid} (standalone MCP" in text
    assert f"{sid} wait" in text and "nothing wakes you" in text

    pointer = link.load_session(link.base_url(), sid)
    assert pointer["standalone"] is True and pointer["agent"] == "cursor"
    _, record = link.load_membership(link.base_url(), sid)
    assert record["join"]["mode"] == "monitor" and record["join"]["harness"] == "cursor"
    assert record["handle"] == "Cursy"
    joins = sum(path.endswith("/join") for _, path, _ in env["calls"])

    text, failed = call(server, "say", text="@Rex hello from cursor")
    assert not failed and "[ptt] said" in text
    assert sum(path.endswith("/join") for _, path, _ in env["calls"]) == joins  # once a process

    # A restarted harness in the same folder gets the same session and member back
    again = mcp.Server()
    assert again.resolve(None) == sid
    assert link.load_membership(link.base_url(), sid)[1]["member_id"] == record["member_id"]

    # Rex's work item reaches the standalone session; `ptt wait --once` works for it too
    env["client"].post(
        f"/projects/{pid}/items",
        json={"kind": "question", "to": "agent:Cursy", "body": "ready?"},
        headers={"X-Member-Key": rex_key},
    )
    text, failed = call(again, "inbox")
    assert not failed and "question from Rex: ready?" in text
    out = io.StringIO()
    with pytest.MonkeyPatch.context() as m:
        m.setattr(sys, "stdout", out)
        assert mcp.ptt.main(["--session", sid, "wait", "--once"]) == 0
    assert "question from Rex: ready?" in out.getvalue()

    # A hooked lookup by folder never borrows the standalone session
    assert link.find_session(link.base_url(), str(env["repo"])) is None


def test_standalone_honours_project_and_channel_overrides(env, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_MCP_MODE", "standalone")
    monkeypatch.setenv("AGENT_PTT_PROJECT", "elsewhere")
    monkeypatch.setenv("AGENT_PTT_CHANNEL", "side-room")
    text, failed = call(mcp.Server(), "who")
    assert not failed and "in elsewhere." in text
    joins = [p for _, path, p in env["calls"] if path == "/projects/join"]
    assert joins[-1]["name"] == "elsewhere" and joins[-1]["harness"] == "mcp"
    channels = [p for _, path, p in env["calls"] if path == "/channels"]
    assert channels[-1]["name"] == "side-room"


def test_auto_mode_with_a_handle_does_not_borrow_a_hooked_session(env, monkeypatch):
    hook_start("s1", env["repo"])
    monkeypatch.setenv("AGENT_PTT_HANDLE", "Cursy")
    assert mcp.Server().resolve(None).startswith("mcp-")
    monkeypatch.delenv("AGENT_PTT_HANDLE")
    assert mcp.Server().resolve(None) == "s1"


# --- packaging -----------------------------------------------------------------------


def test_copies_and_mcp_configs():
    for plugin in COPIES:
        assert (plugin / "ptt_mcp.py").read_bytes() == (HOOKS / "ptt_mcp.py").read_bytes()
    claude = json.loads((HOOKS.parent / ".mcp.json").read_text())["mcpServers"]["agent-ptt"]
    assert claude["command"] == "python3"
    assert claude["args"] == ["${CLAUDE_PLUGIN_ROOT}/hooks/ptt_mcp.py"]
    assert claude["env"]["AGENT_PTT_MCP_MODE"] == "hooked"
    agy_dir, codex_dir = ROOT / "plugins/antigravity-announcer", ROOT / "plugins/codex-announcer"
    agy = json.loads((agy_dir / "mcp_config.json").read_text())["mcpServers"]["agent-ptt"]
    # Antigravity expands ${PLUGIN_ROOT} in args, never in command
    assert agy["command"] == "python3" and agy["args"] == ["${PLUGIN_ROOT}/ptt_mcp.py"]
    manifest = json.loads((codex_dir / ".codex-plugin/plugin.json").read_text())
    codex = json.loads((codex_dir / manifest["mcpServers"]).read_text())["mcpServers"]
    # Codex plugins expand no root variable: a relative cwd is joined to the plugin root
    assert codex["agent-ptt"]["args"] == ["./ptt_mcp.py"] and codex["agent-ptt"]["cwd"] == "."
    for config in (agy, codex["agent-ptt"]):
        assert config["env"]["AGENT_PTT_MCP_MODE"] == "hooked"


def test_ptt_mcp_is_portable():
    text = (HOOKS / "ptt_mcp.py").read_text()
    assert "kbot" not in text and "~/Projects" not in text and "/home/" not in text
    assert "import fcntl" not in text
