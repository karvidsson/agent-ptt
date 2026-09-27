"""Installed dispatcher preserves local hooks and fails closed for hosted sessions."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "plugins/announcer/hooks/workspace_dispatch.py"
spec = importlib.util.spec_from_file_location("workspace_dispatch", PATH)
dispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch)


def test_local_hook_executes_same_target_with_stdin_untouched(monkeypatch):
    stream = io.StringIO('{"hook_event_name":"UserPromptSubmit","prompt":"Local task"}')
    monkeypatch.setattr(dispatch.sys, "stdin", stream)
    script = PATH.with_name("announce.py")
    monkeypatch.setattr(dispatch.sys, "argv", [str(PATH), str(script)])
    monkeypatch.setattr(dispatch, "load", lambda _: SimpleNamespace(enabled=lambda: False))
    calls = []
    monkeypatch.setattr(dispatch.os, "execv", lambda *args: calls.append(args))
    dispatch.main()
    assert calls == [(dispatch.sys.executable, [dispatch.sys.executable, str(script)])]
    assert stream.tell() == 0


def test_hosted_routes_authored_summary_and_never_executes_legacy(monkeypatch):
    sent = []
    workspace = SimpleNamespace(enabled=lambda: True, send=sent.append)
    helper = SimpleNamespace(_is_system_notification=lambda _: False, summarize_prompt=lambda p: p)
    monkeypatch.setattr(
        dispatch, "load", lambda n: workspace if n == "workspace_client" else helper
    )
    monkeypatch.setattr(dispatch.sys, "argv", [str(PATH), str(PATH.with_name("announce.py"))])
    monkeypatch.setattr(
        dispatch.sys,
        "stdin",
        io.StringIO(json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "Hosted task"})),
    )
    monkeypatch.delenv("AGENT_PTT_ANNOUNCE", raising=False)
    monkeypatch.setattr(dispatch.os, "execv", lambda *_: pytest.fail("legacy fallback"))
    dispatch.main()
    assert sent == ["Starting: Hosted task"]


def test_hosted_failure_does_not_fall_back_and_status_does_not_launch(monkeypatch):
    def fail(_):
        raise ValueError("Incomplete hosted config")

    reported = []
    workspace = SimpleNamespace(enabled=lambda: True, send=fail, report_presence=reported.append)
    helper = SimpleNamespace(_is_system_notification=lambda _: False, summarize_prompt=lambda p: p)
    monkeypatch.setattr(
        dispatch, "load", lambda n: workspace if n == "workspace_client" else helper
    )
    monkeypatch.setattr(dispatch.os, "execv", lambda *_: pytest.fail("legacy fallback"))
    monkeypatch.setattr(dispatch.sys, "argv", [str(PATH), str(PATH.with_name("announce.py"))])
    monkeypatch.setattr(
        dispatch.sys,
        "stdin",
        io.StringIO(json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "Hosted task"})),
    )
    monkeypatch.delenv("AGENT_PTT_ANNOUNCE", raising=False)
    with pytest.raises(ValueError, match="Incomplete"):
        dispatch.main()
    monkeypatch.setattr(dispatch.sys, "argv", [str(PATH), str(PATH.with_name("status.py"))])
    monkeypatch.setattr(dispatch.sys, "stdin", io.StringIO('{"hook_event_name":"Stop"}'))
    dispatch.main()
    assert reported == [{"hook_event_name": "Stop"}]


def test_dispatch_copies_and_configs():
    assert (
        PATH.read_bytes() == (ROOT / "plugins/codex-announcer/workspace_dispatch.py").read_bytes()
    )
    for name in (
        "plugins/announcer/hooks/hooks.json",
        "plugins/codex-announcer/hooks.json.template",
    ):
        config = json.loads((ROOT / name).read_text())
        for groups in config["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    if any(target in hook["command"] for target in ("announce.py", "status.py")):
                        assert "workspace_dispatch.py" in hook["command"]
