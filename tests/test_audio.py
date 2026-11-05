"""AudioMixer queueing, spectator fan-out, speaker mode and pacing (no real playback)."""

import asyncio
import builtins
import io
import sys
import types
import wave

import pytest

from agent_ptt import audio
from agent_ptt.audio import (
    AudioMixer,
    _clip_duration,
    _mixers,
    get_mixer,
    remove_mixer,
    speakers_enabled,
)


def test_register_and_unregister_listener():
    mixer = AudioMixer()
    q = mixer.register_stream_listener()
    assert q in mixer._stream_listeners
    mixer.unregister_stream_listener(q)
    assert q not in mixer._stream_listeners
    # Unregistering twice is a no-op
    mixer.unregister_stream_listener(q)


async def test_broadcast_fans_out_to_all_listeners():
    mixer = AudioMixer()
    q1 = mixer.register_stream_listener()
    q2 = mixer.register_stream_listener()

    await mixer._broadcast_to_listeners(b"chunk")

    assert q1.get_nowait() == b"chunk"
    assert q2.get_nowait() == b"chunk"


async def test_enqueue():
    mixer = AudioMixer()
    await mixer.enqueue(b"audio", "Claude")
    assert mixer._queue.get_nowait() == (b"audio", "Claude")


async def test_get_mixer_caches_per_channel():
    mixer = get_mixer("chan-1")
    assert get_mixer("chan-1") is mixer
    assert get_mixer("chan-2") is not mixer


async def test_remove_mixer():
    mixer = get_mixer("chan-1")
    remove_mixer("chan-1")
    assert "chan-1" not in _mixers
    assert mixer._running is False
    # Removing a missing channel is a no-op
    remove_mixer("chan-1")


# ---------------------------------------------------------------------------
# Speaker mode (muted by default) and listener pacing
# ---------------------------------------------------------------------------


def _wav(seconds: float, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


@pytest.fixture
def fake_sounddevice(monkeypatch):
    """A stand-in `sounddevice` module that records play() calls."""
    module = types.ModuleType("sounddevice")
    module.calls = []
    module.play = lambda data, samplerate=None, blocking=False: module.calls.append(
        (len(data), samplerate)
    )
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return module


@pytest.fixture
def import_log(monkeypatch):
    """Record every module name imported while the test runs."""
    names: list[str] = []
    real_import = builtins.__import__

    def recording_import(name, *args, **kwargs):
        names.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", recording_import)
    return names


@pytest.fixture
def pace_sleeps(monkeypatch):
    """Replace the muted-mode pacing wait; records requested seconds."""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(audio, "_pace_sleep", fake_sleep)
    return waits


async def _run_mixer(mixer: AudioMixer, clips: list[tuple[bytes, str]]) -> list[bytes]:
    """Feed clips through a running mixer and return what one listener got."""
    listener = mixer.register_stream_listener()
    mixer.start()
    try:
        for clip, handle in clips:
            await mixer.enqueue(clip, handle)
        received = [await asyncio.wait_for(listener.get(), timeout=2) for _ in clips]
        # Let the loop finish pacing/playing the last clip.
        for _ in range(20):
            if mixer._queue.empty():
                await asyncio.sleep(0.01)
        return received
    finally:
        mixer.stop()


@pytest.mark.parametrize(
    ("env", "flag", "expected"),
    [
        ({}, None, False),
        ({"AGENT_PTT_SPEAKERS": "1"}, None, True),
        ({"AGENT_PTT_SPEAKERS": "true"}, None, True),
        ({"AGENT_PTT_SPEAKERS": "ON"}, None, True),
        ({"AGENT_PTT_SPEAKERS": "0"}, None, False),
        ({"AGENT_PTT_SPEAKERS": "1", "AGENT_PTT_MUTE": "1"}, None, False),
        ({"AGENT_PTT_MUTE": "1"}, True, True),
        ({"AGENT_PTT_SPEAKERS": "1"}, False, False),
        ({}, True, True),
    ],
)
def test_speakers_enabled_precedence(monkeypatch, env, flag, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert speakers_enabled(flag) is expected


def test_speaker_mode_message(monkeypatch):
    assert audio.speaker_mode_message().startswith("Host speaker playback: off")
    monkeypatch.setenv("AGENT_PTT_SPEAKERS", "1")
    assert audio.speaker_mode_message().startswith("Host speaker playback: on")


def test_clip_duration():
    assert _clip_duration(_wav(0.5)) == pytest.approx(0.5)
    assert _clip_duration(b"not a wav") is None


async def test_get_mixer_is_muted_by_default():
    assert get_mixer("chan-default").muted is True


async def test_get_mixer_honors_speaker_env(monkeypatch):
    monkeypatch.setenv("AGENT_PTT_SPEAKERS", "1")
    assert get_mixer("chan-loud").muted is False
    monkeypatch.setenv("AGENT_PTT_MUTE", "1")
    assert get_mixer("chan-forced-quiet").muted is True


async def test_muted_never_touches_sounddevice(fake_sounddevice, import_log, pace_sleeps):
    clips = [(_wav(0.5), "A"), (_wav(0.25), "B"), (b"undecodable", "C")]
    received = await _run_mixer(AudioMixer(muted=True), clips)

    assert received == [clip for clip, _ in clips]
    assert "sounddevice" not in import_log
    assert fake_sounddevice.calls == []
    # Paced as playback would have been: duration + 200 ms gap; no wait when undecodable.
    assert pace_sleeps == [pytest.approx(0.7), pytest.approx(0.45)]


async def test_speakers_play_and_stream_in_order(fake_sounddevice, pace_sleeps):
    clips = [(_wav(0.5), "A"), (_wav(0.25), "B")]
    received = await _run_mixer(AudioMixer(muted=False), clips)

    assert received == [clip for clip, _ in clips]
    # Each clip is played, then the 200 ms gap at the mixer's rate.
    gap = (int(24000 * audio.GAP_SECONDS), 24000)
    assert fake_sounddevice.calls == [(12000, 24000), gap, (6000, 24000), gap]
    # Real playback provides the pacing; no extra sleep.
    assert pace_sleeps == []


async def test_speakers_opt_in_via_env_plays(monkeypatch, fake_sounddevice, pace_sleeps):
    monkeypatch.setenv("AGENT_PTT_SPEAKERS", "1")
    mixer = get_mixer("chan-env")
    listener = mixer.register_stream_listener()
    clip = _wav(0.1)
    await mixer.enqueue(clip, "A")
    assert await asyncio.wait_for(listener.get(), timeout=2) == clip
    for _ in range(50):
        if len(fake_sounddevice.calls) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(fake_sounddevice.calls) == 2
