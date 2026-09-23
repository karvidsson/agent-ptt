"""OmniVoice local neural TTS — runs the model in-process, no cloud calls.

Optional extra: install with `uv sync --extra omnivoice`. Torch and the
omnivoice package are imported inside functions so this module stays
importable (and testable) without the heavy dependencies.
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from collections.abc import Callable
from typing import Any

import numpy as np

from agent_ptt.models import VoiceProfile
from agent_ptt.tts import TTSBackend

logger = logging.getLogger(__name__)

DEFAULT_CHECKPOINT = "k2-fsa/OmniVoice"

# Curated instruct-based voice presets. Instructs are comma-separated
# items from the model's validated English vocabulary (gender, age,
# accent, pitch, whisper).
ARCHETYPES = [
    ("narrator", "Epic Narrator", "male, middle-aged, american accent, low pitch"),
    ("podcaster", "Friendly Podcaster", "male, young adult, american accent, moderate pitch"),
    ("newscaster", "News Anchor", "female, middle-aged, american accent, moderate pitch"),
    ("storyteller", "Warm Storyteller", "female, middle-aged, british accent, low pitch"),
    ("assistant", "AI Assistant", "female, young adult, american accent, high pitch"),
    ("professor", "Distinguished Professor", "male, elderly, british accent, low pitch"),
]


def get_best_device() -> str:
    """Auto-detect the best compute device: CUDA > MPS (Apple Silicon) > CPU."""
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def get_dtype(device: str):
    """fp16 for GPU speed, fp32 for CPU (fp16 is slower/unsupported on CPU)."""
    import torch

    return torch.float16 if device != "cpu" else torch.float32


def tensor_to_wav(audio, sample_rate: int) -> bytes:
    """Encode audio (float samples in [-1, 1]) as 16-bit mono WAV bytes.

    Accepts a torch.Tensor or a numpy array — omnivoice 0.1.5 returns
    numpy arrays from generate(). Uses numpy + stdlib wave instead of
    torchaudio so the base install can run this.
    """
    if hasattr(audio, "detach"):  # torch.Tensor (possibly on GPU / fp16)
        audio = audio.detach().cpu().float().numpy()
    samples = np.asarray(audio, dtype=np.float32).squeeze()
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def _default_model_factory(checkpoint: str):
    """Load the OmniVoice model (downloads ~2.4 GB from HF Hub on first run)."""
    from omnivoice.models.omnivoice import OmniVoice

    device = get_best_device()
    dtype = get_dtype(device)
    logger.info(f"Loading OmniVoice model {checkpoint} (device={device}, dtype={dtype})")
    model = OmniVoice.from_pretrained(
        checkpoint,
        device_map=device,
        dtype=dtype,
        load_asr=False,  # ASR only needed for cloning without ref_text
    )
    logger.info("OmniVoice model loaded")
    return model


class OmniVoiceTTSBackend(TTSBackend):
    """Local neural TTS using the OmniVoice model.

    Lazy-loads the model on first synthesis. Voice profiles drive
    generation through settings: instruct (voice design), ref_audio +
    ref_text (cloning), language, speed.
    """

    def __init__(
        self,
        checkpoint: str = DEFAULT_CHECKPOINT,
        model_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.checkpoint = checkpoint
        self._model_factory = model_factory or _default_model_factory
        self._model: Any = None
        self._load_lock = asyncio.Lock()

    @property
    def engine_name(self) -> str:
        return "omnivoice"

    async def _get_model(self):
        if self._model is None:
            async with self._load_lock:
                if self._model is None:
                    self._model = await asyncio.to_thread(self._model_factory, self.checkpoint)
        return self._model

    async def synthesize(self, text: str, voice_profile: VoiceProfile) -> bytes:
        model = await self._get_model()
        settings = voice_profile.settings

        kwargs: dict[str, Any] = {"text": text}
        if instruct := settings.get("instruct"):
            kwargs["instruct"] = instruct
        if ref_audio := settings.get("ref_audio"):
            kwargs["ref_audio"] = ref_audio
            if ref_text := settings.get("ref_text"):
                kwargs["ref_text"] = ref_text
        if language := settings.get("language"):
            kwargs["language"] = language
        if speed := settings.get("speed"):
            kwargs["speed"] = float(speed)
        # Avoid stochastic position sampling so one pinned profile keeps the
        # same voice character across separate announcements.
        kwargs["position_temperature"] = 0.0

        logger.info(f"omnivoice: generating {len(text)} chars (instruct={instruct!r})")
        audios = await asyncio.to_thread(model.generate, **kwargs)
        sample_rate = getattr(model, "sampling_rate", 24000)
        logger.info(f"omnivoice: generated {getattr(audios[0], 'shape', '?')} samples")
        return tensor_to_wav(audios[0], sample_rate)

    async def list_voices(self) -> list[VoiceProfile]:
        """Return the built-in instruct-based archetypes."""
        return [
            VoiceProfile(
                voice_id=voice_id,
                display_name=name,
                engine="omnivoice",
                settings={"instruct": instruct},
            )
            for voice_id, name, instruct in ARCHETYPES
        ]
