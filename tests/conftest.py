"""Shared fixtures — isolated DB, clean in-memory state, fake TTS/audio."""

import asyncio
import os
import tempfile

# Must be set before any agent_ptt import: db.py builds its engine at import time.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="agent-ptt-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_DIR}/test.db"

import pytest
from fastapi.testclient import TestClient

from agent_ptt import audio, channel, server
from agent_ptt.db import SessionLocal, init_db
from agent_ptt.models import Base, VoiceProfile
from agent_ptt.server import app
from agent_ptt.tts import TTSBackend

FAKE_AUDIO = b"FAKE-WAV-BYTES"


class FakeTTSBackend(TTSBackend):
    """No-network TTS backend returning fixed bytes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, VoiceProfile]] = []

    @property
    def engine_name(self) -> str:
        return "fake"

    async def synthesize(self, text: str, voice_profile: VoiceProfile) -> bytes:
        self.calls.append((text, voice_profile))
        return FAKE_AUDIO

    async def list_voices(self) -> list[VoiceProfile]:
        return [VoiceProfile(voice_id="fake-voice", display_name="Fake", engine="fake")]


class FakeMixer:
    """Stands in for AudioMixer — records enqueues, no speaker playback."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[bytes, str]] = []
        self._stream_listeners: list[asyncio.Queue[bytes]] = []

    async def enqueue(self, audio_bytes: bytes, handle: str) -> None:
        self.enqueued.append((audio_bytes, handle))
        for q in self._stream_listeners:
            q.put_nowait(audio_bytes)

    def register_stream_listener(self) -> asyncio.Queue[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue()
        self._stream_listeners.append(q)
        return q

    def unregister_stream_listener(self, q: asyncio.Queue[bytes]) -> None:
        if q in self._stream_listeners:
            self._stream_listeners.remove(q)

    def stop(self) -> None:
        pass


@pytest.fixture(autouse=True)
def clean_state():
    """Reset in-memory registries and persisted rows between tests.

    Channels are persisted now, so clearing memory alone would leak them
    into the next test through the restore-on-startup path.
    """
    yield
    channel._channels.clear()
    channel._message_queues.clear()
    server._ws_clients.clear()
    for task in server._tts_tasks.values():
        task.cancel()
    server._tts_tasks.clear()
    for mixer in audio._mixers.values():
        mixer.stop()
    audio._mixers.clear()
    init_db()
    with SessionLocal() as cleanup:
        for table in reversed(Base.metadata.sorted_tables):
            cleanup.execute(table.delete())
        cleanup.commit()


@pytest.fixture
def db_session():
    """A real SQLAlchemy session against the temp test DB, emptied on teardown."""
    init_db()
    session = SessionLocal()
    yield session
    session.close()
    cleanup = SessionLocal()
    for table in reversed(Base.metadata.sorted_tables):
        cleanup.execute(table.delete())
    cleanup.commit()
    cleanup.close()


@pytest.fixture
def fake_tts(monkeypatch) -> FakeTTSBackend:
    backend = FakeTTSBackend()
    monkeypatch.setattr(server, "get_backend", lambda engine="pocket-tts": backend)
    return backend


@pytest.fixture
def fake_mixer(monkeypatch) -> FakeMixer:
    mixer = FakeMixer()
    monkeypatch.setattr(server, "get_mixer", lambda channel_id: mixer)
    return mixer


@pytest.fixture
def client(fake_tts, fake_mixer):
    """TestClient with TTS and audio playback faked out."""
    with TestClient(app) as test_client:
        yield test_client
