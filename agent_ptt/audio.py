"""Audio mixer — playback queue, optional speaker output, and WebSocket streaming.

Broadcasts synthesized audio sequentially to connected WebSocket spectator
listeners. Playing it on the host's own speakers is opt-in (see
``speakers_enabled``); muted, the mixer never touches the audio device but
keeps the same pacing for listeners.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import wave

logger = logging.getLogger(__name__)

# Silence between consecutive clips, on the speakers and for listeners alike.
GAP_SECONDS = 0.2

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def speakers_enabled(flag: bool | None = None) -> bool:
    """Whether the server plays audio on the host's speakers.

    Off by default. Precedence: an explicit ``flag`` (the CLI's
    ``--speakers/--no-speakers``) wins; otherwise ``AGENT_PTT_MUTE`` set
    truthy forces mute; otherwise ``AGENT_PTT_SPEAKERS`` truthy opts in.
    """
    if flag is not None:
        return flag
    if _env_flag("AGENT_PTT_MUTE"):
        return False
    return _env_flag("AGENT_PTT_SPEAKERS")


def speaker_mode_message() -> str:
    """One-line description of the speaker mode, logged at server startup."""
    if speakers_enabled():
        return "Host speaker playback: on (disable with --no-speakers or AGENT_PTT_MUTE=1)"
    return "Host speaker playback: off (muted; enable with --speakers or AGENT_PTT_SPEAKERS=1)"


def _clip_duration(audio_bytes: bytes) -> float | None:
    """Duration in seconds of a WAV clip, or None if it can't be decoded."""
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            rate = wf.getframerate()
            return wf.getnframes() / rate if rate else None
    except Exception:
        return None


# Indirection so tests can patch the pacing wait without sleeping.
_pace_sleep = asyncio.sleep


class AudioMixer:
    """Per-channel audio queue: WS streaming plus optional speaker output."""

    def __init__(self, sample_rate: int = 24000, channels: int = 1, muted: bool = True) -> None:
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
        """Background loop: dequeue audio, stream it, and pace (or play) it."""
        sd = None
        gap = None
        if self.muted:
            logger.debug("Mixer muted; audio goes to stream listeners only")
        else:
            try:
                import numpy as np
                import sounddevice as sd

                gap = np.zeros(int(self.sample_rate * GAP_SECONDS), dtype=np.float32)
            except Exception as e:
                logger.warning(f"sounddevice unavailable, speaker output disabled: {e}")
                sd = None

        while self._running:
            try:
                audio_bytes, handle = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            # Broadcast raw bytes to WebSocket listeners
            await self._broadcast_to_listeners(audio_bytes)

            if sd is not None:
                logger.info(f"🔊 Playing audio from [{handle}]")
                try:
                    await asyncio.to_thread(self._play_audio, sd, audio_bytes)
                    # Small gap between speakers
                    await asyncio.to_thread(
                        sd.play, gap, samplerate=self.sample_rate, blocking=True
                    )
                except Exception as e:
                    logger.error(f"Speaker playback error: {e}")
            else:
                # No speakers: wait as long as playback would have, so listeners
                # get clips spaced exactly as before (duration + gap).
                logger.info(f"📡 Streaming audio from [{handle}]")
                duration = _clip_duration(audio_bytes)
                if duration is not None:
                    await _pace_sleep(duration + GAP_SECONDS)

    def _play_audio(self, sd, audio_bytes: bytes) -> None:
        """Play audio bytes through speakers (blocking, run in thread)."""
        import numpy as np

        try:
            # Try to decode as WAV first
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

        # Fallback: treat as raw MP3 bytes (custom backends may output MP3)
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
        _mixers[channel_id] = AudioMixer(muted=not speakers_enabled())
        _mixers[channel_id].start()
    return _mixers[channel_id]


def remove_mixer(channel_id: str) -> None:
    """Stop and remove a channel's mixer."""
    mixer = _mixers.pop(channel_id, None)
    if mixer:
        mixer.stop()
