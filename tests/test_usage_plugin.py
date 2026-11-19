"""Token usage in the plugins (task 37): usage.py's readers and report, the Cursor
hook's counts, the opt-in statusline wrapper and install_plugins.py --statusline.

Fixtures are synthetic JSONL in the shapes probed on kbot (Claude Code 2.1.316,
codex-cli 0.166.0, cursor-agent 2026.11.19). Every fixture carries a SECRET text that
must never show up in state files or in the PATCH body.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugins/announcer/hooks"
SECRET = "SECRET-PROMPT-TEXT-do-not-leak"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


usage = load(HOOKS / "usage.py", "plugin_usage")
routing = load(HOOKS / "session_routing.py", "plugin_usage_routing")
statusline = load(ROOT / "plugins/announcer/statusline.py", "plugin_statusline")
installer = load(ROOT / "scripts/install_plugins.py", "usage_install_plugins")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "USAGE_DIR", tmp_path / "usage")
    monkeypatch.setattr(usage, "CC_LIMITS_FILE", tmp_path / "cc-ratelimits.json")
    for name in ("AGENT_PTT_HOOKS", "AGENT_PTT_USAGE", "CLAUDE_CODE_ENTRYPOINT", "AGENT_PTT_AGENT"):
        monkeypatch.delenv(name, raising=False)


def write_lines(path: Path, records: list[dict], mode: str = "w", partial: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode) as out:
        for record in records:
            out.write(json.dumps(record) + "\n")
        out.write(partial)


def assistant(msg_id: str, model="claude-opus-5-5", inp=2, read=100, write=10, out=5, think=0):
    return {
        "type": "assistant",
        "uuid": f"u-{msg_id}",
        "message": {
            "id": msg_id,
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": SECRET}],
            "usage": {
                "input_tokens": inp,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write,
                "cache_creation": {"ephemeral_5m_input_tokens": write},
                "output_tokens": out,
                "output_tokens_details": {"thinking_tokens": think},
            },
        },
    }


def user_line():
    return {"type": "user", "message": {"role": "user", "content": SECRET}}


# --- Claude reader -------------------------------------------------------------


def test_claude_dedups_repeated_lines_and_sums_per_model(tmp_path):
    path = tmp_path / "s1.jsonl"
    write_lines(
        path,
        [
            user_line(),
            assistant("m1", think=3),
            assistant("m1", think=3),  # same response written twice
            assistant("m1", think=3),
            assistant("m2", model="claude-haiku-5-5", inp=1, read=0, write=0, out=7),
            assistant("m3", model="<synthetic>", inp=0, read=0, write=0, out=0),
        ],
    )
    state: dict = {}
    by_model = usage.read_claude(str(path), state)
    assert by_model == {
        "claude-opus-5-5": {
            "input": 2,
            "cache_read": 100,
            "cache_write": 10,
            "output": 5,
            "reasoning": 3,
        },
        "claude-haiku-5-5": {
            "input": 1,
            "cache_read": 0,
            "cache_write": 0,
            "output": 7,
            "reasoning": 0,
        },
    }
    assert SECRET not in json.dumps(state)


def test_claude_reads_incrementally_and_dedups_across_runs(tmp_path):
    path = tmp_path / "s1.jsonl"
    write_lines(path, [assistant("m1")])
    state: dict = {}
    usage.read_claude(str(path), state)
    write_lines(path, [assistant("m1"), assistant("m2", out=1)], mode="a")
    by_model = usage.read_claude(str(path), state)
    assert by_model["claude-opus-5-5"]["output"] == 6  # m1 once, plus m2
    assert state["files"][str(path)]["offset"] == path.stat().st_size


def test_claude_partial_last_line_waits_for_the_newline(tmp_path):
    path = tmp_path / "s1.jsonl"
    whole = json.dumps(assistant("m2", out=40))
    write_lines(path, [assistant("m1")], partial=whole[:30])
    state: dict = {}
    assert usage.read_claude(str(path), state)["claude-opus-5-5"]["output"] == 5
    with path.open("a") as out:
        out.write(whole[30:] + "\n")
    assert usage.read_claude(str(path), state)["claude-opus-5-5"]["output"] == 45


def test_claude_rolls_subagents_into_the_session(tmp_path):
    path = tmp_path / "proj" / "sess.jsonl"
    write_lines(path, [assistant("m1")])
    write_lines(
        tmp_path / "proj" / "sess" / "subagents" / "agent-a1.jsonl",
        [
            assistant("s1", model="claude-haiku-5-5", out=11),
            assistant("s1", model="claude-haiku-5-5", out=11),
        ],
    )
    write_lines(tmp_path / "proj" / "sess" / "subagents" / "agent-a2.jsonl", [assistant("s2")])
    (tmp_path / "proj" / "sess" / "subagents" / "agent-a1.meta.json").write_text("{}")
    by_model = usage.read_claude(str(path), {})
    assert by_model["claude-opus-5-5"]["output"] == 10  # m1 + s2
    assert by_model["claude-haiku-5-5"]["output"] == 11


def test_claude_file_that_shrank_is_counted_again(tmp_path):
    path = tmp_path / "s1.jsonl"
    write_lines(path, [assistant("m1"), assistant("m2"), assistant("m3")])
    state: dict = {}
    assert usage.read_claude(str(path), state)["claude-opus-5-5"]["output"] == 15
    write_lines(path, [assistant("n1", out=2)])  # rewritten, shorter
    assert usage.read_claude(str(path), state)["claude-opus-5-5"]["output"] == 2


def test_claude_seen_tail_is_bounded(tmp_path):
    path = tmp_path / "s1.jsonl"
    write_lines(path, [assistant(f"m{i}", out=1) for i in range(usage.SEEN_TAIL + 30)])
    state: dict = {}
    by_model = usage.read_claude(str(path), state)
    assert by_model["claude-opus-5-5"]["output"] == usage.SEEN_TAIL + 30
    assert len(state["files"][str(path)]["seen"]) == usage.SEEN_TAIL


def test_claude_limits_from_a_fresh_statusline_file():
    usage.CC_LIMITS_FILE.write_text(
        json.dumps(
            {
                "rate_limits": {
                    "five_hour": {"used_percentage": 41, "resets_at": 1795557600},
                    "seven_day": {"used_percentage": 120, "resets_at": None},
                },
                "rate_limits_at": 1000.0,
            }
        )
    )
    limits = usage.claude_limits(now=1000.0 + 60)
    assert limits["source"] == "claude-statusline"
    assert limits["plan"] is None
    assert limits["windows"] == [
        {"name": "5h", "used_pct": 41.0, "resets_at": "2026-11-24T22:00:00Z"},
        {"name": "7d", "used_pct": 100.0, "resets_at": None},
    ]
    assert limits["reported_at"] == "1970-01-01T00:16:40Z"
    assert usage.claude_limits(now=1000.0 + 31 * 60) is None  # stale


# --- Codex reader --------------------------------------------------------------


def token_count(inp, cached, out, reasoning=0, write=0, limits=True):
    payload = {
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": inp,
                "cached_input_tokens": cached,
                "cache_write_input_tokens": write,
                "output_tokens": out,
                "reasoning_output_tokens": reasoning,
                "total_tokens": inp + out,
            },
            "last_token_usage": {"input_tokens": 1},
            "model_context_window": 258400,
        },
    }
    if limits:
        payload["rate_limits"] = {
            "limit_id": "codex",
            "primary": {"used_percent": 4.0, "window_minutes": 10080, "resets_at": 1794992199},
            "secondary": None,
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            "plan_type": "prolite",
            "rate_limit_reached_type": None,
        }
    return {"timestamp": "2026-11-13T04:37:24.986Z", "type": "event_msg", "payload": payload}


def turn_context(model):
    return {"type": "turn_context", "payload": {"model": model, "cwd": "/x"}}


def codex_message():
    return {
        "type": "response_item",
        "payload": {"type": "message", "content": [{"type": "output_text", "text": SECRET}]},
    }


def test_codex_takes_the_running_total_without_cached_input(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_lines(
        path,
        [
            turn_context("gpt-6-astra"),
            codex_message(),
            token_count(1000, 800, 20, reasoning=5),
            token_count(264126, 219392, 2112, reasoning=959),
        ],
    )
    state: dict = {}
    by_model = usage.read_codex(str(path), state)
    assert by_model == {
        "gpt-6-astra": {
            "input": 44734,
            "cache_read": 219392,
            "cache_write": 0,
            "output": 2112,
            "reasoning": 959,
        }
    }
    assert state["limits"] == {
        "source": "codex-rollout",
        "plan": "prolite",
        "windows": [
            {
                "name": "7d",
                "used_pct": 4.0,
                "resets_at": "2026-11-18T08:56:39Z",
                "window_minutes": 10080,
            }
        ],
        "reported_at": "2026-11-13T04:37:24.986Z",
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    }
    assert SECRET not in json.dumps(state)


def test_codex_credits_each_increase_to_the_current_model(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_lines(path, [turn_context("gpt-a"), token_count(100, 0, 10, limits=False)])
    state: dict = {}
    usage.read_codex(str(path), state)
    write_lines(
        path,
        [turn_context("gpt-b"), token_count(150, 20, 30, limits=False)],
        mode="a",
        partial='{"type": "event_msg", "payl',
    )
    by_model = usage.read_codex(str(path), state)
    assert by_model["gpt-a"] == {
        "input": 100,
        "cache_read": 0,
        "cache_write": 0,
        "output": 10,
        "reasoning": 0,
    }
    assert by_model["gpt-b"] == {
        "input": 30,
        "cache_read": 20,
        "cache_write": 0,
        "output": 20,
        "reasoning": 0,
    }
    assert "limits" not in state


def test_codex_rollout_that_shrank_starts_over(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_lines(path, [turn_context("m"), token_count(500, 0, 50), token_count(900, 0, 90)])
    state: dict = {}
    usage.read_codex(str(path), state)
    write_lines(path, [turn_context("m"), token_count(10, 0, 1)])
    assert usage.read_codex(str(path), state)["m"]["output"] == 1


def test_window_names():
    assert usage.window_name(300) == "5h"
    assert usage.window_name(10080) == "7d"
    assert usage.window_name(45) == "45m"


# --- Cursor --------------------------------------------------------------------


def cursor_payload(event, generation, **counts):
    return {
        "conversation_id": "conv-1",
        "generation_id": generation,
        "model": "default",
        "text": SECRET,
        "hook_event_name": event,
        "input_tokens": 13484,
        "output_tokens": 32,
        "cache_read_tokens": 12160,
        "cache_write_tokens": 0,
        **counts,
    }


def test_cursor_counts_each_generation_once():
    assert usage.record_cursor(cursor_payload("afterAgentResponse", "g1"))
    assert usage.record_cursor(cursor_payload("stop", "g1"))  # the same turn again
    assert usage.record_cursor(cursor_payload("stop", "g2", input_tokens=200, cache_read_tokens=0))
    state = usage.load_state("cursor", "conv-1")
    assert state["reader"]["by_model"] == {
        "default": {
            "input": 1324 + 200,
            "cache_read": 12160,
            "cache_write": 0,
            "output": 64,
            "reasoning": 0,
        }
    }
    assert SECRET not in usage.state_path("cursor", "conv-1").read_text()


def test_cursor_payload_without_counts_is_ignored():
    raw = {"conversation_id": "conv-1", "generation_id": "g1", "text": SECRET}
    assert not usage.record_cursor(raw)
    assert not usage.state_path("cursor", "conv-1").exists()


# --- report ----------------------------------------------------------------------


@pytest.fixture
def link(tmp_path):
    """A stand-in for project_link: one membership, a recorded PATCH."""
    sent: list[tuple] = []
    record = {"project_id": "p1", "member_key": "mk-secret"}

    def request(base, method, path, payload=None, **kwargs):
        if fake.fail:
            raise OSError("server down")
        sent.append((method, path, payload, kwargs.get("member_key")))
        return {}

    fake = types.SimpleNamespace(
        routing=routing,
        base_url=lambda: "http://ptt.test",
        harness=lambda agent: agent.lower() if agent.lower() in ("codex", "cursor") else "claude",
        load_membership=lambda base, session_id: (tmp_path / "m.json", dict(record)),
        request=request,
        sent=sent,
        fail=False,
    )
    return fake


def claude_event(path: Path, **extra) -> dict:
    event = {"hook_event_name": "Stop", "session_id": "sess-1", "transcript_path": str(path)}
    return {**event, **extra}


def test_report_sends_cumulative_counts_and_debounces(tmp_path, link):
    path = tmp_path / "sess-1.jsonl"
    write_lines(path, [user_line(), assistant("m1")])
    assert usage.report(claude_event(path), now=1000.0, link=link)
    method, where, body, key = link.sent[0]
    assert (method, where, key) == ("PATCH", "/projects/p1/members/me", "mk-secret")
    assert body == {
        "usage": {
            "session_id": "sess-1",
            "harness": "claude",
            "by_model": {
                "claude-opus-5-5": {
                    "input": 2,
                    "cache_read": 100,
                    "cache_write": 10,
                    "output": 5,
                    "reasoning": 0,
                }
            },
            "as_of": "1970-01-01T00:16:40Z",
        }
    }
    assert SECRET not in json.dumps(link.sent)

    assert not usage.report(claude_event(path), now=1100.0, link=link)  # unchanged
    write_lines(path, [assistant("m2")], mode="a")
    assert not usage.report(claude_event(path), now=1020.0, link=link)  # within 30 s
    assert usage.report(claude_event(path), now=1031.0, link=link)
    assert link.sent[-1][2]["usage"]["by_model"]["claude-opus-5-5"]["output"] == 10  # total
    write_lines(path, [assistant("m3")], mode="a")
    assert usage.report(claude_event(path), final=True, now=1032.0, link=link)  # SessionEnd
    assert len(link.sent) == 3


def test_report_includes_fresh_claude_limits(tmp_path, link):
    path = tmp_path / "sess-1.jsonl"
    write_lines(path, [assistant("m1")])
    usage.CC_LIMITS_FILE.write_text(
        json.dumps(
            {
                "rate_limits": {"seven_day": {"used_percentage": 15, "resets_at": 1795557600}},
                "rate_limits_at": 990.0,
            }
        )
    )
    assert usage.report(claude_event(path), now=1000.0, link=link)
    limits = link.sent[0][2]["usage"]["limits"]
    assert limits["source"] == "claude-statusline"
    assert limits["windows"][0]["name"] == "7d"


def test_report_failure_is_silent_and_retried(tmp_path, link):
    path = tmp_path / "sess-1.jsonl"
    write_lines(path, [assistant("m1")])
    link.fail = True
    assert not usage.report(claude_event(path), now=1000.0, link=link)
    link.fail = False
    assert usage.report(claude_event(path), now=1001.0, link=link)  # not marked sent


def test_report_skips_without_membership_subagents_and_opt_out(tmp_path, link, monkeypatch):
    path = tmp_path / "sess-1.jsonl"
    write_lines(path, [assistant("m1")])
    assert not usage.report(claude_event(path, agent_id="a1"), now=1000.0, link=link)
    sub = tmp_path / "sess-1" / "subagents" / "agent-a.jsonl"
    assert not usage.report(claude_event(sub), now=1000.0, link=link)
    monkeypatch.setenv("AGENT_PTT_USAGE", "0")
    assert not usage.report(claude_event(path), now=1000.0, link=link)
    monkeypatch.delenv("AGENT_PTT_USAGE")
    link.load_membership = lambda base, session_id: (None, {})
    assert not usage.report(claude_event(path), now=1000.0, link=link)
    assert link.sent == []


def test_report_codex_and_cursor(tmp_path, link, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    write_lines(path, [turn_context("gpt-6-astra"), token_count(1000, 800, 20)])
    monkeypatch.setenv("AGENT_PTT_AGENT", "Codex")
    assert usage.report(claude_event(path), now=1000.0, link=link)
    body = link.sent[-1][2]["usage"]
    assert body["harness"] == "codex"
    assert body["by_model"]["gpt-6-astra"]["input"] == 200
    assert body["limits"]["plan"] == "prolite"

    monkeypatch.setenv("AGENT_PTT_AGENT", "Cursor")
    usage.record_cursor(cursor_payload("stop", "g1", conversation_id="sess-1"))
    assert usage.report({"hook_event_name": "Stop", "session_id": "sess-1"}, now=1000, link=link)
    body = link.sent[-1][2]["usage"]
    assert body["harness"] == "cursor"
    assert body["by_model"]["default"]["output"] == 32
    assert SECRET not in json.dumps(link.sent)


def test_report_antigravity_sends_nothing(tmp_path, link, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_AGENT", "Antigravity")
    link.harness = lambda agent: "antigravity"
    assert not usage.report({"hook_event_name": "Stop", "session_id": "s"}, link=link)


def test_usage_script_never_prints(tmp_path):
    """The hook runner sees no output (nothing can reach the model's context)."""
    event = {"hook_event_name": "Stop", "session_id": "x", "transcript_path": str(tmp_path)}
    done = subprocess.run(
        [sys.executable, str(HOOKS / "usage.py")],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


# --- wiring ----------------------------------------------------------------------


def test_announce_reports_usage_on_stop_even_when_speech_is_off(monkeypatch):
    announce = load(HOOKS / "announce.py", "usage_announce")
    calls = []
    monkeypatch.setattr(announce, "report_usage", lambda event, final=False: calls.append(final))
    monkeypatch.setattr(announce, "_detach", lambda: None)
    monkeypatch.setenv("AGENT_PTT_ANNOUNCE", "0")
    monkeypatch.setenv("AGENT_PTT_FORK", "0")
    event = {"hook_event_name": "Stop", "session_id": "s1", "transcript_path": "/none"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    announce.main()
    assert calls == [False]
    sub = {**event, "transcript_path": "/p/s1/subagents/agent-a.jsonl"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(sub)))
    announce.main()
    assert calls == [False]  # a subagent's Stop is read with the session's
    end = {**event, "hook_event_name": "SessionEnd"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(end)))
    announce.main()
    assert calls == [False, True]


def test_hooks_run_usage_only_from_async_stop_paths():
    """No hook runs usage.py directly: it rides on announce.py's Stop/SessionEnd."""
    for config in ROOT.glob("plugins/*/hooks/hooks.json"):
        assert "usage.py" not in config.read_text()
    claude = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
    stop = [h for g in claude["Stop"] for h in g["hooks"] if "announce.py" in h["command"]]
    assert stop and all(h.get("async") for h in stop)


def test_cursor_hook_registers_after_agent_response_and_drops_text(monkeypatch, tmp_path):
    cursor = load(ROOT / "plugins/cursor-announcer/cursor_hook.py", "usage_cursor_hook")
    hooks = json.loads((ROOT / "plugins/cursor-announcer/hooks/hooks.json").read_text())
    assert "afterAgentResponse" in hooks["hooks"]
    seen = []
    monkeypatch.setattr(cursor, "STATE_DIR", tmp_path / "cursor")
    monkeypatch.setattr(cursor, "record_usage", lambda raw: seen.append(dict(raw)))
    cursor.update_state("conv-1", main=True)
    monkeypatch.setenv("AGENT_PTT_CURSOR_DEBUG", "1")
    monkeypatch.setattr(sys, "argv", ["cursor_hook.py", "afterAgentResponse"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(cursor_payload("x", "g1"))))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    cursor.main()
    assert seen and "text" not in seen[0] and seen[0]["output_tokens"] == 32
    assert SECRET not in (tmp_path / "cursor" / "debug.jsonl").read_text()


def test_cursor_stop_records_counts_before_announcing(monkeypatch):
    cursor = load(ROOT / "plugins/cursor-announcer/cursor_hook.py", "usage_cursor_stop")
    order = []
    monkeypatch.setattr(cursor, "record_usage", lambda raw: order.append("usage"))
    monkeypatch.setattr(cursor, "run", lambda script, event, *a, **k: order.append(script) or [])
    cursor.stop(cursor_payload("stop", "g1"))
    assert order[0] == "usage" and "workspace_dispatch.py" in order


# --- statusline wrapper --------------------------------------------------------


@pytest.fixture
def sl(monkeypatch, tmp_path):
    monkeypatch.setattr(statusline, "LIMITS_FILE", tmp_path / "cc-ratelimits.json")
    monkeypatch.setattr(statusline, "PREVIOUS_FILE", tmp_path / "statusline-previous.json")
    return tmp_path


def statusline_input(**extra):
    return {
        "session_id": "s1",
        "transcript_path": "/x",
        "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
        "workspace": {"current_dir": "/home/u/proj"},
        "cost": {"total_cost_usd": 0.25, "total_duration_ms": 10},
        **extra,
    }


def test_statusline_saves_limits_and_keeps_them_when_absent(sl):
    windows = {"seven_day": {"used_percentage": 15, "resets_at": 1795557600}}
    statusline.save(statusline_input(rate_limits=windows), now=100.0)
    saved = statusline.save(statusline_input(session_id="s2"), now=200.0)  # no rate_limits yet
    assert saved["rate_limits"] == windows
    assert saved["rate_limits_at"] == 100.0
    assert set(saved["sessions"]) == {"s1", "s2"}
    assert saved["sessions"]["s1"] == {"total_cost_usd": 0.25, "at": 100.0}


def run_statusline(sl, data: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "plugins/announcer/statusline.py")],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        env={"HOME": str(sl), "PATH": "/usr/bin:/bin"},
        check=False,
    )


def test_statusline_prints_a_minimal_line_without_a_previous_command(sl):
    data = statusline_input(rate_limits={"five_hour": {"used_percentage": 41.4}})
    done = run_statusline(sl, data)
    assert done.stdout == "Opus 5.5 · proj · 5h 41%\n"
    saved = json.loads((sl / ".agent-ptt" / "cc-ratelimits.json").read_text())
    assert saved["rate_limits"]["five_hour"]["used_percentage"] == 41.4


def test_statusline_chains_the_previous_command(sl):
    script = sl / "old.py"
    script.write_text("import sys, json; print('old:' + json.load(sys.stdin)['session_id'])")
    (sl / ".agent-ptt").mkdir()
    (sl / ".agent-ptt" / "statusline-previous.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": f"python3 {script}"}})
    )
    assert run_statusline(sl, statusline_input()).stdout == "old:s1\n"


# --- install_plugins.py --statusline -------------------------------------------


def test_install_statusline_chains_and_restores(tmp_path):
    settings = tmp_path / "claude" / "settings.json"
    settings.parent.mkdir()
    previous = {"type": "command", "command": "~/bin/my-line.sh", "padding": 0}
    settings.write_text(json.dumps({"model": "opus", "statusLine": previous}))
    state = tmp_path / "agent-ptt"
    root = Path("/repo")

    assert installer.install_statusline(root, settings, state) == "installed"
    current = json.loads(settings.read_text())
    assert current["model"] == "opus"
    assert current["statusLine"] == {
        "type": "command",
        "command": 'python3 "/repo/plugins/announcer/statusline.py"',
        "padding": 0,
    }
    saved = json.loads((state / "statusline-previous.json").read_text())
    assert saved == {"statusLine": previous}

    assert installer.install_statusline(root, settings, state) == "refreshed"
    saved = json.loads((state / "statusline-previous.json").read_text())
    assert saved == {"statusLine": previous}  # not overwritten by our own entry

    assert installer.restore_statusline(settings, state) == "restored"
    assert json.loads(settings.read_text()) == {"model": "opus", "statusLine": previous}
    assert not (state / "statusline-previous.json").exists()
    assert installer.restore_statusline(settings, state) == "not installed"


def test_install_statusline_without_a_previous_one(tmp_path):
    settings = tmp_path / "settings.json"
    state = tmp_path / "agent-ptt"
    assert installer.install_statusline(Path("/repo"), settings, state) == "installed"
    assert installer.restore_statusline(settings, state) == "removed"
    assert json.loads(settings.read_text()) == {}


def test_install_statusline_refuses_invalid_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{not json")
    with pytest.raises(ValueError):
        installer.install_statusline(Path("/repo"), settings, tmp_path / "s")
    assert settings.read_text() == "{not json"


def test_install_without_the_flag_leaves_settings_alone(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(installer, "install_statusline", lambda *a: called.append(a))
    monkeypatch.setattr(installer, "restore_statusline", lambda *a: called.append(a))
    monkeypatch.setattr(installer, "main_checkout", lambda: ROOT)
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    assert installer.main([]) == 0
    assert called == []
