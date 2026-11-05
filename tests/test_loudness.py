"""Per-utterance loudness normalization: measurement, gain caps, and both server paths."""

import io
import math

import numpy as np
import pytest
import soundfile as sf

from agent_ptt import loudness
from agent_ptt import workspace_speech as speech
from agent_ptt.loudness import (
    DEFAULT_TARGET_LUFS,
    PEAK_CEILING_DBFS,
    integrated_loudness,
    normalize_loudness,
)
from agent_ptt.models import VoiceProfile
from tests.test_api import _create_channel, _join, _wait_for

RATE = 24000  # Pocket TTS sample rate


def _wav(samples, rate=RATE) -> bytes:
    out = io.BytesIO()
    sf.write(out, np.asarray(samples), rate, format="WAV", subtype="PCM_16")
    return out.getvalue()


def _read(wav: bytes):
    samples, rate = sf.read(io.BytesIO(wav), dtype="float64")
    return samples, rate


def _lufs(wav: bytes) -> float:
    samples, rate = _read(wav)
    return integrated_loudness(samples, rate)


def _peak_db(wav: bytes) -> float:
    samples, _ = _read(wav)
    return 20 * math.log10(np.max(np.abs(samples)))


def _sine(lufs: float, seconds: float = 2.0, freq: float = 997.0):
    t = np.arange(round(seconds * RATE)) / RATE
    tone = np.sin(2 * np.pi * freq * t)
    return tone * 10 ** ((lufs - integrated_loudness(tone, RATE)) / 20)


def _noise(lufs: float, seconds: float = 2.0, seed: int = 1):
    noise = np.random.default_rng(seed).uniform(-1, 1, round(seconds * RATE))
    return noise * 10 ** ((lufs - integrated_loudness(noise, RATE)) / 20)


def test_k_weighting_matches_bs1770_reference():
    # BS.1770: a full-scale 997 Hz sine in one channel reads -3.01 LUFS.
    t = np.arange(3 * 48000) / 48000
    assert integrated_loudness(np.sin(2 * np.pi * 997 * t), 48000) == pytest.approx(-3.01, abs=0.02)


@pytest.mark.parametrize(
    "samples",
    [_sine(-40), _sine(-6), _noise(-40), _noise(-12)],
    ids=["quiet-sine", "loud-sine", "quiet-noise", "loud-noise"],
)
def test_quiet_and_loud_audio_reach_target(samples):
    out = normalize_loudness(_wav(samples))
    assert _lufs(out) == pytest.approx(DEFAULT_TARGET_LUFS, abs=0.5)
    assert _peak_db(out) <= PEAK_CEILING_DBFS + 0.01


def test_output_keeps_mono_pcm16_and_sample_rate():
    out = normalize_loudness(_wav(_sine(-30)))
    info = sf.info(io.BytesIO(out))
    assert (info.format, info.subtype, info.channels, info.samplerate) == (
        "WAV",
        "PCM_16",
        1,
        RATE,
    )


def test_peaky_speech_is_limited_not_clipped():
    # Quiet tone with sharp transients ~20 dB above its loudness, like speech.
    samples = _sine(-40)
    samples[RATE // 2 :: RATE // 4] = 10 ** ((-40 + 20) / 20)
    out = normalize_loudness(_wav(samples))
    assert _lufs(out) == pytest.approx(DEFAULT_TARGET_LUFS, abs=0.5)
    assert _peak_db(out) <= PEAK_CEILING_DBFS + 0.01


def test_gain_is_capped_when_peaks_exceed_limiter_budget():
    # Peaks 30 dB above loudness: the limiter may only take 6 dB, so the clip
    # stays under the ceiling and falls short of the target instead.
    samples = _sine(-40)
    samples[RATE // 2] = 10 ** ((-40 + 30) / 20)
    out = normalize_loudness(_wav(samples))
    assert _peak_db(out) <= PEAK_CEILING_DBFS + 0.01
    assert _lufs(out) < DEFAULT_TARGET_LUFS - 1


def test_target_is_configurable(monkeypatch):
    monkeypatch.setenv("AGENT_PTT_TARGET_LUFS", "-23")
    assert _lufs(normalize_loudness(_wav(_sine(-35)))) == pytest.approx(-23, abs=0.5)


@pytest.mark.parametrize("value", ["off", "OFF", "none"])
def test_normalization_can_be_turned_off(monkeypatch, value):
    monkeypatch.setenv("AGENT_PTT_TARGET_LUFS", value)
    wav = _wav(_sine(-35))
    assert normalize_loudness(wav) is wav


@pytest.mark.parametrize("value", ["loud", "3", "nan"])
def test_invalid_target_falls_back_to_default(monkeypatch, value):
    monkeypatch.setenv("AGENT_PTT_TARGET_LUFS", value)
    assert loudness.target_lufs() == DEFAULT_TARGET_LUFS


@pytest.mark.parametrize(
    "samples",
    [np.zeros(RATE), _noise(-80)],
    ids=["silence", "near-silence"],
)
def test_silence_is_not_amplified(samples):
    wav = _wav(samples)
    assert normalize_loudness(wav) == wav


def test_max_gain_is_capped():
    out = normalize_loudness(_wav(_sine(-65)))
    assert _lufs(out) == pytest.approx(-65 + loudness.MAX_GAIN_DB, abs=0.5)


def test_clip_shorter_than_gating_block_does_not_raise():
    out = normalize_loudness(_wav(_sine(-30, seconds=0.05)))
    samples, rate = _read(out)
    assert rate == RATE and len(samples) == round(0.05 * RATE)
    assert _peak_db(out) <= PEAK_CEILING_DBFS + 0.01


@pytest.mark.parametrize("data", [b"", b"FAKE-WAV-BYTES", b"RIFF\x00\x00\x00\x00WAVEjunk"])
def test_unparsable_bytes_pass_through(data):
    assert normalize_loudness(data) == data


def test_empty_wav_passes_through():
    wav = _wav(np.zeros(0))
    assert normalize_loudness(wav) == wav


# ---------------------------------------------------------------------------
# Every playback path normalizes, whatever the backend
# ---------------------------------------------------------------------------


def test_channel_tts_worker_normalizes(client, fake_tts, fake_mixer, monkeypatch):
    quiet = _wav(_sine(-40))

    async def synthesize(text, voice_profile):
        return quiet

    monkeypatch.setattr(fake_tts, "synthesize", synthesize)
    channel_id = _create_channel(client)
    key_id = _join(client, channel_id)
    with client.websocket_connect(f"/channels/{channel_id}/ws?key={key_id}") as ws:
        ws.send_json({"type": "message", "text": "speak up"})
        ws.receive_json()
        assert _wait_for(lambda: fake_mixer.enqueued), "TTS worker never enqueued audio"

    audio, _handle = fake_mixer.enqueued[0]
    assert _lufs(audio) == pytest.approx(DEFAULT_TARGET_LUFS, abs=0.5)


async def test_workspace_speech_normalizes(monkeypatch):
    loud = _wav(_sine(-6))

    class Backend:
        async def synthesize(self, text, profile: VoiceProfile):
            return loud

    monkeypatch.setattr(speech, "get_backend", lambda engine: Backend())
    audio = await speech.synthesize("org", {"text": "hi", "voice": {"id": "alba"}})
    assert _lufs(audio) == pytest.approx(DEFAULT_TARGET_LUFS, abs=0.5)
