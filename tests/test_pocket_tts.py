"""Pocket integration without model downloads or audio hardware."""

import asyncio
import io
import sys
import threading
import time
import wave
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from agent_ptt.models import PinnedVoiceDB, VoiceProfile
from agent_ptt.tts import POCKET_VOICES, PocketTTSBackend
from agent_ptt.voicedesign import get_or_create_pinned_voice
from agent_ptt.voices import get_voice_profile, migrate_legacy_voice_profiles, save_voice_profile


@pytest.fixture
def pocket_model(monkeypatch):
    tensor = Mock()
    tensor.detach.return_value.cpu.return_value.numpy.return_value = np.array(
        [0, 0.25, -0.25], dtype=np.float32
    )
    model = SimpleNamespace(
        sample_rate=24000,
        get_state_for_audio_prompt=Mock(side_effect=lambda voice: {"voice": voice}),
        generate_audio=Mock(return_value=tensor),
    )
    loader = Mock(return_value=model)
    monkeypatch.setitem(
        sys.modules, "pocket_tts", SimpleNamespace(TTSModel=SimpleNamespace(load_model=loader))
    )
    return model, loader


async def test_catalog_does_not_load_model(pocket_model):
    profiles = await PocketTTSBackend().list_voices()
    assert [p.voice_id for p in profiles] == list(POCKET_VOICES)
    pocket_model[1].assert_not_called()


async def test_synthesis_caches_model_and_voice_and_emits_pcm_wav(pocket_model):
    model, loader = pocket_model
    backend = PocketTTSBackend()
    profile = VoiceProfile(display_name="Default")
    for _ in range(2):
        wav = await backend.synthesize("Hello", profile)
        with wave.open(io.BytesIO(wav)) as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (
                1,
                2,
                24000,
            )
            assert audio.getnframes() == 3
    loader.assert_called_once_with()
    model.get_state_for_audio_prompt.assert_called_once_with("alba")
    model.generate_audio.assert_called_with({"voice": "alba"}, "Hello")


async def test_reference_audio_is_passed_to_pocket(pocket_model):
    await PocketTTSBackend().synthesize(
        "Hi", VoiceProfile(display_name="Clone", settings={"voice": "/tmp/reference.wav"})
    )
    pocket_model[0].get_state_for_audio_prompt.assert_called_once_with("/tmp/reference.wav")


async def test_parallel_channels_serialize_inference_off_event_loop(pocket_model):
    model, loader = pocket_model
    original = model.generate_audio.return_value
    active = 0
    peak = 0
    main_thread = threading.get_ident()

    def generate(*args):
        nonlocal active, peak
        assert threading.get_ident() != main_thread
        active += 1
        peak = max(peak, active)
        time.sleep(0.01)
        active -= 1
        return original

    model.generate_audio.side_effect = generate
    backend = PocketTTSBackend()
    profile = VoiceProfile(display_name="Default")
    await asyncio.gather(*(backend.synthesize("hello", profile) for _ in range(5)))
    assert peak == 1
    loader.assert_called_once()


async def test_failed_voice_load_can_be_retried(pocket_model):
    model, _ = pocket_model
    model.get_state_for_audio_prompt.side_effect = [RuntimeError("download failed"), {}]
    backend = PocketTTSBackend()
    profile = VoiceProfile(display_name="Default")
    with pytest.raises(RuntimeError, match="download failed"):
        await backend.synthesize("hello", profile)
    assert await backend.synthesize("hello", profile)


@pytest.mark.parametrize("engine", ["edge-tts", "system", "omnivoice"])
def test_legacy_migration_preserves_profile_and_pin(db_session, engine):
    save_voice_profile(
        VoiceProfile(
            voice_id="old",
            display_name="Old",
            engine=engine,
            settings={"voice": "legacy", "rate": 200},
        ),
        db_session,
    )
    db_session.add(PinnedVoiceDB(handle="alice", voice_id="old", source="hash"))
    db_session.commit()
    save_voice_profile(
        VoiceProfile(
            voice_id="omni", display_name="Omni", engine="omnivoice", settings={"instruct": "male"}
        ),
        db_session,
    )
    migrate_legacy_voice_profiles(db_session)
    migrated = get_or_create_pinned_voice("alice", db_session)
    assert migrated.voice_id == "old"
    assert migrated.engine == "pocket-tts"
    assert migrated.settings["voice"] in POCKET_VOICES
    assert "rate" not in migrated.settings
    migrate_legacy_voice_profiles(db_session)
    assert get_voice_profile("old", db_session).settings == migrated.settings
    assert get_voice_profile("omni", db_session).engine == "pocket-tts"
