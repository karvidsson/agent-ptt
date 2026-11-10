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


def test_local_hook_receives_same_event_after_transport_selection(monkeypatch):
    stream = io.StringIO('{"hook_event_name":"UserPromptSubmit","prompt":"Local task"}')
    monkeypatch.setattr(dispatch.sys, "stdin", stream)
    script = PATH.with_name("announce.py")
    monkeypatch.setattr(dispatch.sys, "argv", [str(PATH), str(script)])
    monkeypatch.setattr(
        dispatch,
        "load",
        lambda _: SimpleNamespace(
            routing=SimpleNamespace(bind_event=lambda event: None), enabled=lambda: False
        ),
    )
    calls = []
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *args, **kw: calls.append((args, kw)))
    dispatch.main()
    assert calls[0][0] == ([dispatch.sys.executable, str(script)],)
    assert json.loads(calls[0][1]["input"])["prompt"] == "Local task"


def test_hosted_routes_authored_summary_and_never_executes_legacy(monkeypatch):
    sent = []
    workspace = SimpleNamespace(
        routing=SimpleNamespace(bind_event=lambda event: None),
        enabled=lambda: True,
        polling_turn=lambda event: False,
        send=sent.append,
    )
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
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **k: pytest.fail("legacy fallback"))
    dispatch.main()
    assert sent == ["Starting: Hosted task"]


def test_hosted_failure_does_not_fall_back_and_status_does_not_launch(monkeypatch):
    def fail(_):
        raise ValueError("Incomplete hosted config")

    reported = []
    workspace = SimpleNamespace(
        routing=SimpleNamespace(bind_event=lambda event: None),
        enabled=lambda: True,
        polling_turn=lambda event: False,
        send=fail,
        report_presence=reported.append,
    )
    helper = SimpleNamespace(_is_system_notification=lambda _: False, summarize_prompt=lambda p: p)
    monkeypatch.setattr(
        dispatch, "load", lambda n: workspace if n == "workspace_client" else helper
    )
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **k: pytest.fail("legacy fallback"))
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
        "plugins/codex-announcer/hooks/hooks.json",
    ):
        config = json.loads((ROOT / name).read_text())
        for groups in config["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    if any(target in hook["command"] for target in ("announce.py", "status.py")):
                        assert "workspace_dispatch.py" in hook["command"]


@pytest.mark.parametrize("name", ["UserPromptSubmit", "Stop"])
def test_polling_never_posts_automatic_announcements(monkeypatch, name):
    workspace = SimpleNamespace(
        routing=SimpleNamespace(bind_event=lambda event: None),
        enabled=lambda: True,
        polling_turn=lambda event: True,
        send=lambda text: pytest.fail("Polling must not send chat"),
    )
    monkeypatch.setattr(
        dispatch,
        "load",
        lambda n: (
            workspace if n == "workspace_client" else pytest.fail("Polling must not summarize")
        ),
    )
    monkeypatch.setattr(dispatch.sys, "argv", [str(PATH), str(PATH.with_name("announce.py"))])
    monkeypatch.setattr(dispatch.sys, "stdin", io.StringIO(json.dumps({"hook_event_name": name})))
    dispatch.main()
