"""Pocket TTS voice cloning and preview CLI.

httpx and playback are faked — no server, network, or speakers needed.
"""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import agent_ptt.cli as cli
from agent_ptt.models import VoiceProfile
from tests.conftest import FAKE_AUDIO, FakeTTSBackend

runner = CliRunner()


def _flat(output: str) -> str:
    return " ".join(output.split())


@pytest.fixture
def fake_post(monkeypatch):
    """Capture httpx.post payloads and reply 200."""
    calls = []

    def post(url, json=None, **kwargs):
        calls.append({"url": url, "json": json})
        return SimpleNamespace(status_code=200, json=lambda: json, text="")

    monkeypatch.setattr(cli.httpx, "post", post)
    return calls


@pytest.fixture
def fake_profile_get(monkeypatch):
    """Serve a stored Pocket TTS profile over fake httpx.get."""
    profile = VoiceProfile(
        voice_id="aussie-agent",
        display_name="Aussie Agent",
        engine="pocket-tts",
        settings={"voice": "alba"},
    )

    def get(url, **kwargs):
        if url.endswith("/voices/profiles/aussie-agent"):
            return SimpleNamespace(status_code=200, json=lambda: profile.model_dump(mode="json"))
        return SimpleNamespace(status_code=404, json=lambda: {}, text="not found")

    monkeypatch.setattr(cli.httpx, "get", get)
    return profile


def test_preview_synthesizes_and_plays(monkeypatch, fake_profile_get):
    backend = FakeTTSBackend()
    played = []
    monkeypatch.setattr("agent_ptt.tts.get_backend", lambda engine: backend)
    monkeypatch.setattr(cli, "_play_audio_bytes", played.append)

    result = runner.invoke(cli.app, ["voice", "preview", "aussie-agent", "--text", "G'day!"])

    assert result.exit_code == 0, result.output
    text, profile = backend.calls[0]
    assert text == "G'day!"
    assert profile.settings == {"voice": "alba"}
    assert played == [FAKE_AUDIO]


def test_preview_missing_profile(monkeypatch, fake_profile_get):
    result = runner.invoke(cli.app, ["voice", "preview", "nonexistent"])
    assert result.exit_code == 1


def test_preview_engine_not_installed(monkeypatch, fake_profile_get):
    def raise_unknown(engine):
        raise ValueError("Unknown TTS engine")

    monkeypatch.setattr("agent_ptt.tts.get_backend", raise_unknown)
    result = runner.invoke(cli.app, ["voice", "preview", "aussie-agent"])
    assert result.exit_code == 1
    assert "uv sync" in _flat(result.output)


def test_clone_defaults_to_pocket_without_transcript(fake_post, tmp_path):
    ref = tmp_path / "sample.wav"
    ref.write_bytes(b"RIFF fake wav")
    result = runner.invoke(cli.app, ["voice", "clone", "--reference", str(ref), "--name", "Pocket"])
    assert result.exit_code == 0, result.output
    assert fake_post[0]["json"]["engine"] == "pocket-tts"
    assert fake_post[0]["json"]["settings"] == {"voice": str(ref.resolve())}


def test_description_design_command_removed(fake_post):
    result = runner.invoke(cli.app, ["voice", "design", "--name", "Old voice"])
    assert result.exit_code != 0
    assert not fake_post


def test_removed_engine_rejected_by_api(client):
    response = client.post(
        "/voices/profiles",
        json={
            "voice_id": "unsupported",
            "display_name": "Unsupported",
            "engine": "omnivoice",
            "settings": {"instruct": "male"},
        },
    )
    assert response.status_code == 400
    assert client.get("/voices?engine=omnivoice").status_code == 400
