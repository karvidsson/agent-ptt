"""Audio mixer — playback queue, speaker output, and WebSocket streaming.

Plays synthesized audio sequentially through speakers and broadcasts
to any connected WebSocket spectator listeners.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os

import numpy as np

logger = logging.getLogger(__name__)


class AudioMixer:
    """Per-channel audio playback queue with speaker output + WS streaming."""

    def __init__(self, sample_rate: int = 24000, channels: int = 1, muted: bool = False) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.muted = muted
        self._queue: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
        self._stream_listeners: list[asyncio.Queue[bytes]] = []
        self._running = False
        self._playback_task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the playback loop."""
        if not self._running:
            self._running = True
            self._playback_task = asyncio.create_task(self._playback_loop())

    def stop(self) -> None:
        """Stop the playback loop."""
        self._running = False
        if self._playback_task and not self._playback_task.done():
            self._playback_task.cancel()

    async def enqueue(self, audio_bytes: bytes, handle: str) -> None:
        """Add synthesized audio to the playback queue."""
        await self._queue.put((audio_bytes, handle))

    def register_stream_listener(self) -> asyncio.Queue[bytes]:
        """Register a new WebSocket spectator listener.

        Returns a queue that will receive audio chunks.
        """
        q: asyncio.Queue[bytes] = asyncio.Queue()
        self._stream_listeners.append(q)
        return q

    def unregister_stream_listener(self, q: asyncio.Queue[bytes]) -> None:
        """Remove a WebSocket spectator listener."""
        with contextlib.suppress(ValueError):
            self._stream_listeners.remove(q)

    async def _broadcast_to_listeners(self, audio_bytes: bytes) -> None:
        """Send audio chunk to all registered WebSocket stream listeners."""
        dead: list[asyncio.Queue[bytes]] = []
        for q in self._stream_listeners:
            try:
                q.put_nowait(audio_bytes)
            except asyncio.QueueFull:
                # Listener is too slow — drop it
                dead.append(q)
        for q in dead:
            self.unregister_stream_listener(q)

    async def _playback_loop(self) -> None:
        """Background loop: dequeue audio and play through speakers."""
        if self.muted:
            sd = None
            logger.info("Speaker playback muted; audio remains available to stream listeners")
        else:
            try:
                import sounddevice as sd
            except Exception as e:
                logger.warning(f"sounddevice unavailable, speaker output disabled: {e}")
                sd = None

        # Small silence gap between speakers (200ms)
        gap = np.zeros(int(self.sample_rate * 0.2), dtype=np.float32)

        while self._running:
            try:
                audio_bytes, handle = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            logger.info(f"🔊 Playing audio from [{handle}]")

            # Broadcast raw bytes to WebSocket listeners
            await self._broadcast_to_listeners(audio_bytes)

            # Play through speakers
            if sd is not None:
                try:
                    await asyncio.to_thread(self._play_audio, sd, audio_bytes)
                    # Small gap between speakers
                    await asyncio.to_thread(
                        sd.play, gap, samplerate=self.sample_rate, blocking=True
                    )
                except Exception as e:
                    logger.error(f"Speaker playback error: {e}")

    def _play_audio(self, sd, audio_bytes: bytes) -> None:
        """Play audio bytes through speakers (blocking, run in thread)."""
        try:
            # Try to decode as WAV first
            import wave

            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                frames = wf.readframes(wf.getnframes())
                sample_rate = wf.getframerate()
                n_channels = wf.getnchannels()
                sample_width = wf.getsampwidth()

                if sample_width == 2:
                    dtype = np.int16
                elif sample_width == 4:
                    dtype = np.int32
                else:
                    dtype = np.int16

                audio = np.frombuffer(frames, dtype=dtype)
                if n_channels > 1:
                    audio = audio.reshape(-1, n_channels)

                audio_float = audio.astype(np.float32) / np.iinfo(dtype).max
                sd.play(audio_float, samplerate=sample_rate, blocking=True)
                return
        except Exception:
            pass

        # Fallback: treat as raw MP3 bytes (edge-tts outputs MP3)
        # Use a temp file approach with sounddevice
        try:
            import tempfile
            from pathlib import Path

            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(audio_bytes)
                tmp_path = f.name

            # Use numpy + soundfile to decode
            try:
                import soundfile as sf

                data, sr = sf.read(tmp_path)
                sd.play(data, samplerate=sr, blocking=True)
            except ImportError:
                # If soundfile not available, try pydub
                try:
                    from pydub import AudioSegment

                    seg = AudioSegment.from_mp3(tmp_path)
                    samples = np.array(seg.get_array_of_samples(), dtype=np.float32)
                    samples = samples / (2**15)
                    if seg.channels > 1:
                        samples = samples.reshape(-1, seg.channels)
                    sd.play(samples, samplerate=seg.frame_rate, blocking=True)
                except Exception as e:
                    logger.error(f"Cannot decode audio: {e}")
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Audio decode error: {e}")


# ---------------------------------------------------------------------------
# Per-channel mixer registry
# ---------------------------------------------------------------------------

_mixers: dict[str, AudioMixer] = {}


def get_mixer(channel_id: str) -> AudioMixer:
    """Get or create an AudioMixer for a channel."""
    if channel_id not in _mixers:
        _mixers[channel_id] = AudioMixer(muted=os.environ.get("AGENT_PTT_MUTE") == "1")
        _mixers[channel_id].start()
    return _mixers[channel_id]


def remove_mixer(channel_id: str) -> None:
    """Stop and remove a channel's mixer."""
    mixer = _mixers.pop(channel_id, None)
    if mixer:
        mixer.stop()
