"""`agent-ptt server start` speaker-mode flags; uvicorn is faked, nothing binds."""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

import agent_ptt.cli as cli
from agent_ptt.audio import speakers_enabled

runner = CliRunner()


@pytest.fixture
def fake_uvicorn(monkeypatch):
    import uvicorn

    runs: list[dict] = []

    def fake_run(app, **kwargs):
        # What the server process would resolve once it starts.
        runs.append({"app": app, "speakers": speakers_enabled(), **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return runs


@pytest.mark.parametrize(
    ("args", "env", "expected"),
    [
        ([], {}, False),
        (["--speakers"], {}, True),
        (["--no-speakers"], {}, False),
        (["--mute"], {}, False),
        (["--speakers", "--mute"], {}, False),
        ([], {"AGENT_PTT_SPEAKERS": "1"}, True),
        (["--no-speakers"], {"AGENT_PTT_SPEAKERS": "1"}, False),
        ([], {"AGENT_PTT_SPEAKERS": "1", "AGENT_PTT_MUTE": "1"}, False),
        (["--speakers"], {"AGENT_PTT_MUTE": "1"}, True),
    ],
)
def test_server_start_speaker_mode(monkeypatch, fake_uvicorn, args, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    result = runner.invoke(cli.app, ["server", "start", *args])

    assert result.exit_code == 0, result.output
    assert fake_uvicorn[0]["speakers"] is expected
    assert fake_uvicorn[0]["app"] == "agent_ptt.server:app"


def test_apply_speaker_mode_pins_env():
    assert cli.apply_speaker_mode(None) is False
    assert os.environ["AGENT_PTT_MUTE"] == "1"
    assert cli.apply_speaker_mode(True) is True
    assert os.environ["AGENT_PTT_SPEAKERS"] == "1"
    assert "AGENT_PTT_MUTE" not in os.environ


def test_mute_flag_is_hidden_but_speakers_documented():
    result = runner.invoke(cli.app, ["server", "start", "--help"])
    assert "--speakers" in result.output
    assert "--mute" not in result.output


def test_server_logs_speaker_mode_at_startup(caplog, fake_tts, fake_mixer):
    from fastapi.testclient import TestClient

    from agent_ptt.server import app

    with caplog.at_level("INFO", logger="agent_ptt.server"), TestClient(app):
        pass
    assert any("Host speaker playback: off" in r.getMessage() for r in caplog.records)
