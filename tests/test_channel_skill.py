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


def test_create_selects_channel(monkeypatch, tmp_path, capsys):
    config = tmp_path / "announcer.env"
    monkeypatch.setattr(channel, "CONFIG_FILE", config)
    monkeypatch.setattr(
        channel,
        "_request",
        lambda method, path, payload=None: {"name": payload["name"], "channel_id": "c1"},
    )

    assert channel.main(["create", "Release", "War", "Room"]) == 0
    assert "AGENT_PTT_CHANNEL=Release War Room" in config.read_text()
    assert "Created and selected" in capsys.readouterr().out


def test_use_rejects_unknown_channel(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(channel, "CONFIG_FILE", tmp_path / "announcer.env")
    monkeypatch.setattr(channel, "_request", lambda *args, **kwargs: [])

    assert channel.main(["use", "Missing"]) == 1
    assert "Channel not found" in capsys.readouterr().err
