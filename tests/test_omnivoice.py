"""OmniVoice backend — WAV encoding, device selection, and synthesis wiring.

All tests run without torch or the omnivoice package installed: tensors
are duck-typed fakes and torch is monkeypatched into sys.modules.
"""

import io
import sys
import types
import wave

import numpy as np
import pytest

from agent_ptt.engines.omnivoice import (
    ARCHETYPES,
    OmniVoiceTTSBackend,
    get_best_device,
    get_dtype,
    tensor_to_wav,
)
from agent_ptt.models import VoiceProfile


class FakeTensor:
    """Duck-types the torch.Tensor surface tensor_to_wav relies on."""

    def __init__(self, array):
        self._array = np.asarray(array, dtype=np.float32)

    def detach(self):
        return self

    def cpu(self):
        return self

    def float(self):
        return self

    def numpy(self):
        return self._array


class FakeModel:
    sampling_rate = 24000

    def __init__(self):
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return [FakeTensor(np.zeros(240))]


def _fake_torch(cuda=False, mps=False):
    torch = types.SimpleNamespace()
    torch.cuda = types.SimpleNamespace(is_available=lambda: cuda)
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps))
    torch.float16 = "float16"
    torch.float32 = "float32"
    return torch


def _profile(**settings) -> VoiceProfile:
    return VoiceProfile(voice_id="test", display_name="Test", engine="omnivoice", settings=settings)


# ---------------------------------------------------------------------------
# tensor_to_wav
# ---------------------------------------------------------------------------


def test_tensor_to_wav_produces_valid_wav():
    samples = np.sin(np.linspace(0, 4 * np.pi, 480)).astype(np.float32) * 0.5
    wav_bytes = tensor_to_wav(FakeTensor(samples), 24000)

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getframerate() == 24000
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        decoded = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)

    assert len(decoded) == 480
    np.testing.assert_allclose(decoded / 32767.0, samples, atol=1e-4)


def test_tensor_to_wav_clips_out_of_range():
    wav_bytes = tensor_to_wav(FakeTensor([2.0, -2.0]), 24000)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        decoded = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    assert decoded.tolist() == [32767, -32767]


def test_tensor_to_wav_squeezes_batch_dim():
    wav_bytes = tensor_to_wav(FakeTensor([[0.0, 0.5, -0.5]]), 24000)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnframes() == 3


def test_tensor_to_wav_accepts_numpy_array():
    """omnivoice 0.1.5 returns numpy arrays from generate(), not tensors."""
    samples = np.array([0.0, 0.25, -0.25], dtype=np.float32)
    wav_bytes = tensor_to_wav(samples, 24000)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        decoded = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    np.testing.assert_allclose(decoded / 32767.0, samples, atol=1e-4)


# ---------------------------------------------------------------------------
# Device / dtype selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cuda", "mps", "expected"),
    [(True, True, "cuda"), (False, True, "mps"), (False, False, "cpu")],
)
def test_get_best_device(monkeypatch, cuda, mps, expected):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda=cuda, mps=mps))
    assert get_best_device() == expected


def test_get_dtype(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch())
    assert get_dtype("cuda") == "float16"
    assert get_dtype("mps") == "float16"
    assert get_dtype("cpu") == "float32"


# ---------------------------------------------------------------------------
# Synthesis wiring
# ---------------------------------------------------------------------------


async def test_synthesize_instruct_mode():
    model = FakeModel()
    backend = OmniVoiceTTSBackend(model_factory=lambda checkpoint: model)

    wav_bytes = await backend.synthesize("hello", _profile(instruct="[gender:female]"))

    assert model.calls == [
        {"text": "hello", "instruct": "[gender:female]", "position_temperature": 0.0}
    ]
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getframerate() == 24000


async def test_synthesize_clone_mode():
    model = FakeModel()
    backend = OmniVoiceTTSBackend(model_factory=lambda checkpoint: model)

    await backend.synthesize(
        "hello",
        _profile(ref_audio="/tmp/ref.wav", ref_text="reference transcript"),
    )

    assert model.calls == [
        {
            "text": "hello",
            "ref_audio": "/tmp/ref.wav",
            "ref_text": "reference transcript",
            "position_temperature": 0.0,
        }
    ]


async def test_synthesize_ref_text_ignored_without_ref_audio():
    model = FakeModel()
    backend = OmniVoiceTTSBackend(model_factory=lambda checkpoint: model)

    await backend.synthesize("hello", _profile(ref_text="orphan transcript"))

    assert model.calls == [{"text": "hello", "position_temperature": 0.0}]


async def test_synthesize_language_and_speed():
    model = FakeModel()
    backend = OmniVoiceTTSBackend(model_factory=lambda checkpoint: model)

    await backend.synthesize("hello", _profile(instruct="[x]", language="en", speed="1.2"))

    assert model.calls == [
        {
            "text": "hello",
            "instruct": "[x]",
            "language": "en",
            "speed": 1.2,
            "position_temperature": 0.0,
        }
    ]


async def test_model_loads_once():
    model = FakeModel()
    loads = []

    def factory(checkpoint):
        loads.append(checkpoint)
        return model

    backend = OmniVoiceTTSBackend(checkpoint="my/checkpoint", model_factory=factory)
    await backend.synthesize("one", _profile())
    await backend.synthesize("two", _profile())

    assert loads == ["my/checkpoint"]
    assert len(model.calls) == 2


# ---------------------------------------------------------------------------
# Archetypes
# ---------------------------------------------------------------------------


async def test_list_voices_returns_archetypes():
    backend = OmniVoiceTTSBackend(model_factory=lambda checkpoint: FakeModel())
    voices = await backend.list_voices()

    assert len(voices) == len(ARCHETYPES) == 6
    assert {v.voice_id for v in voices} == {
        "narrator",
        "podcaster",
        "newscaster",
        "storyteller",
        "assistant",
        "professor",
    }
    assert all(v.engine == "omnivoice" and v.settings.get("instruct") for v in voices)


def test_engine_name():
    assert OmniVoiceTTSBackend(model_factory=lambda c: None).engine_name == "omnivoice"
