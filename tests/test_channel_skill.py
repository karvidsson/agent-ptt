"""Tests for the Agent PTT channel-management skill."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "plugins/voice/scripts/channel.py"
spec = importlib.util.spec_from_file_location("channel_skill", SCRIPT)
channel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(channel)


@pytest.fixture(autouse=True)
def isolated_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PTT_SESSION_ID", "s1")
    monkeypatch.setenv("AGENT_PTT_AGENT", "Codex")
    monkeypatch.setattr(channel.routing, "ROUTES_DIR", tmp_path / "routes")


def selected():
    return channel.routing.channel_name("s1", "Codex", channel.BASE_URL, "repo")


def _fake_server(existing):
    """A fake _request over a list of channels; POST /channels appends one."""
    channels = list(existing)
    posts = []

    def fake_request(method, path, payload=None):
        if method == "GET" and path == "/channels":
            return channels
        if method == "POST" and path == "/channels":
            posts.append(payload["name"])
            created = {"name": payload["name"], "channel_id": f"c{len(channels) + 1}"}
            channels.append(created)
            return created
        raise AssertionError(f"unexpected request {method} {path}")

    return fake_request, posts


def test_create_selects_new_channel(monkeypatch, tmp_path, capsys):
    fake_request, posts = _fake_server([])
    monkeypatch.setattr(channel, "_request", fake_request)

    assert channel.main(["create", "Release", "War", "Room"]) == 0
    assert posts == ["Release War Room"]
    assert selected() == "Release War Room"
    assert "Created and selected" in capsys.readouterr().out


def test_create_reuses_existing_channel(monkeypatch, tmp_path, capsys):
    fake_request, posts = _fake_server([{"name": "Release", "channel_id": "c1"}])
    monkeypatch.setattr(channel, "_request", fake_request)

    assert channel.main(["create", "Release"]) == 0
    assert posts == []
    assert "Selected: Release (c1)" in capsys.readouterr().out


def test_use_creates_missing_channel(monkeypatch, tmp_path, capsys):
    fake_request, posts = _fake_server([])
    monkeypatch.setattr(channel, "_request", fake_request)

    assert channel.main(["use", "Missing"]) == 0
    assert posts == ["Missing"]
    assert selected() == "Missing"
    assert "Created and selected" in capsys.readouterr().out


def test_selection_is_scoped_to_session_and_auto_clears_it(monkeypatch):
    fake_request, _ = _fake_server([])
    monkeypatch.setattr(channel, "_request", fake_request)
    monkeypatch.setenv("AGENT_PTT_CHANNEL", "inherited")
    assert channel.main(["use", "War Room"]) == 0
    assert selected() == "War Room"
    monkeypatch.delenv("AGENT_PTT_CHANNEL")
    assert channel.routing.channel_name("s2", "Codex", channel.BASE_URL, "repo") == "repo"
    assert channel.routing.channel_name("s1", "Claude", channel.BASE_URL, "repo") == "repo"
    assert channel.routing.channel_name("s1", "Codex", "http://other", "repo") == "repo"
    monkeypatch.setenv("AGENT_PTT_CHANNEL", "inherited")
    assert channel.main(["auto"]) == 0
    assert selected() == "repo"


def test_missing_session_never_sets_global_override(monkeypatch):
    for name in ("AGENT_PTT_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(channel, "_request", lambda *args: pytest.fail("must not create channel"))
    assert channel.main(["use", "Shared"]) == 2


def test_explicit_session_and_agent_flags(monkeypatch):
    fake_request, _ = _fake_server([])
    monkeypatch.setattr(channel, "_request", fake_request)
    assert channel.main(["use", "Shared", "--session-id", "s2", "--agent", "Claude"]) == 0
    assert channel.routing.channel_name("s2", "Claude", channel.BASE_URL, "repo") == "Shared"
    assert selected() == "repo"


def test_legacy_global_channel_is_ignored(monkeypatch, tmp_path):
    config = tmp_path / "announcer.env"
    config.write_text("AGENT_PTT_CHANNEL=Old global room\n")
    monkeypatch.setattr(channel.routing, "CONFIG_FILE", config)
    monkeypatch.delenv("AGENT_PTT_CHANNEL", raising=False)
    assert selected() == "repo"
    assert config.read_text() == "AGENT_PTT_CHANNEL=Old global room\n"


def test_bundled_routing_helpers_are_identical():
    plugins = SCRIPT.parents[2]
    expected = SCRIPT.with_name("session_routing.py").read_bytes()
    assert (plugins / "announcer/hooks/session_routing.py").read_bytes() == expected
    assert (plugins / "codex-announcer/session_routing.py").read_bytes() == expected
