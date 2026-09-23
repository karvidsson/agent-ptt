"""Tests for the Agent PTT channel-management skill."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "plugins/voice/scripts/channel.py"
spec = importlib.util.spec_from_file_location("channel_skill", SCRIPT)
channel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(channel)


def test_save_channel_preserves_other_settings(tmp_path, monkeypatch):
    config = tmp_path / "announcer.env"
    config.write_text("AGENT_PTT_URL=http://localhost:8770\nAGENT_PTT_CHANNEL=Old\n")
    monkeypatch.setattr(channel, "CONFIG_FILE", config)

    channel._save_channel("New Channel")

    assert config.read_text() == (
        "AGENT_PTT_URL=http://localhost:8770\nAGENT_PTT_CHANNEL=New Channel\n"
    )


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
    config = tmp_path / "announcer.env"
    monkeypatch.setattr(channel, "CONFIG_FILE", config)
    fake_request, posts = _fake_server([])
    monkeypatch.setattr(channel, "_request", fake_request)

    assert channel.main(["create", "Release", "War", "Room"]) == 0
    assert posts == ["Release War Room"]
    assert "AGENT_PTT_CHANNEL=Release War Room" in config.read_text()
    assert "Created and selected" in capsys.readouterr().out


def test_create_reuses_existing_channel(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(channel, "CONFIG_FILE", tmp_path / "announcer.env")
    fake_request, posts = _fake_server([{"name": "Release", "channel_id": "c1"}])
    monkeypatch.setattr(channel, "_request", fake_request)

    assert channel.main(["create", "Release"]) == 0
    assert posts == []
    assert "Selected: Release (c1)" in capsys.readouterr().out


def test_use_creates_missing_channel(monkeypatch, tmp_path, capsys):
    config = tmp_path / "announcer.env"
    monkeypatch.setattr(channel, "CONFIG_FILE", config)
    fake_request, posts = _fake_server([])
    monkeypatch.setattr(channel, "_request", fake_request)

    assert channel.main(["use", "Missing"]) == 0
    assert posts == ["Missing"]
    assert "AGENT_PTT_CHANNEL=Missing" in config.read_text()
    assert "Created and selected" in capsys.readouterr().out
