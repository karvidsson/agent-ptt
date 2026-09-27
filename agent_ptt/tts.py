"""Pluggable TTS backends — no external service dependency.

Pocket TTS is the sole built-in speech engine.
"""

from __future__ import annotations

import asyncio
import io
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict

from agent_ptt.models import VoiceProfile


class TTSBackend(ABC):
    """Abstract base for any TTS engine."""

    @abstractmethod
    async def synthesize(self, text: str, voice_profile: VoiceProfile) -> bytes:
        """Synthesize text to WAV audio bytes."""
        ...

    @abstractmethod
    async def list_voices(self) -> list[VoiceProfile]:
        """Return available voices for this engine."""
        ...

    @property
    @abstractmethod
    def engine_name(self) -> str:
        """Engine identifier string."""
        ...


POCKET_VOICES = ("alba", "marius", "javert", "jean", "fantine", "cosette", "eponine", "azelma")


class PocketTTSBackend(TTSBackend):
    """Local CPU synthesis; load once and serialize inference across channels."""

    def __init__(self):
        self._model = None
        self._voice_states = OrderedDict()
        self._lock = threading.Lock()

    @property
    def engine_name(self) -> str:
        return "pocket-tts"

    async def list_voices(self) -> list[VoiceProfile]:
        return [
            VoiceProfile(
                voice_id=name,
                display_name=name.title(),
                engine=self.engine_name,
                settings={"voice": name},
            )
            for name in POCKET_VOICES
        ]

    async def synthesize(self, text: str, voice_profile: VoiceProfile) -> bytes:
        voice = voice_profile.settings.get("voice", "alba")
        if not isinstance(voice, str) or not voice.strip():
            raise ValueError("Pocket TTS voice must be a non-empty voice name or audio path")
        return await asyncio.to_thread(self._synthesize, text, voice)

    def _synthesize(self, text: str, voice: str) -> bytes:
        import soundfile as sf
        from pocket_tts import TTSModel

        # A thread lock also protects inference if an async caller is cancelled.
        with self._lock:
            if self._model is None:
                self._model = TTSModel.load_model()
            if voice not in self._voice_states:
                self._voice_states[voice] = self._model.get_state_for_audio_prompt(voice)
                if len(self._voice_states) > 32:
                    self._voice_states.popitem(last=False)
            self._voice_states.move_to_end(voice)
            audio = self._model.generate_audio(self._voice_states[voice], text)
            output = io.BytesIO()
            sf.write(
                output,
                audio.detach().cpu().numpy(),
                self._model.sample_rate,
                format="WAV",
                subtype="PCM_16",
            )
            return output.getvalue()


# ---------------------------------------------------------------------------
# Engine registry
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, TTSBackend] = {
    "pocket-tts": PocketTTSBackend(),
}


def has_backend(engine: str) -> bool:
    """Check whether a TTS backend is registered."""
    return engine in _BACKENDS


def get_backend(engine: str = "pocket-tts") -> TTSBackend:
    """Get a TTS backend by engine name."""
    backend = _BACKENDS.get(engine)
    if backend is None:
        raise ValueError(f"Unknown TTS engine '{engine}'. Available: {list(_BACKENDS.keys())}")
    return backend


def register_backend(name: str, backend: TTSBackend) -> None:
    """Register a custom TTS backend."""
    _BACKENDS[name] = backend
