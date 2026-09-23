"""FastAPI WebSocket server — REST endpoints + real-time agent communication."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agent_ptt import channel as ch
from agent_ptt.audio import get_mixer
from agent_ptt.db import SessionLocal, get_db, init_db
from agent_ptt.models import Message, VoiceProfile
from agent_ptt.tts import get_backend, has_backend
from agent_ptt.voicedesign import (
    get_or_create_pinned_voice,
    list_pinned_voices,
    redesign_pinned_voice,
)
from agent_ptt.voices import (
    delete_voice_profile,
    get_voice_profile,
    list_voice_profiles,
    save_voice_profile,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connected WebSocket clients per channel
# ---------------------------------------------------------------------------

_ws_clients: dict[str, list[WebSocket]] = {}


# ---------------------------------------------------------------------------
# TTS worker — consumes message queue, synthesizes, and enqueues audio
# ---------------------------------------------------------------------------

_tts_tasks: dict[str, asyncio.Task] = {}


async def _tts_worker(channel_id: str) -> None:
    """Background task that processes the TTS queue for a channel."""
    queue = ch.get_message_queue(channel_id)
    if queue is None:
        return

    mixer = get_mixer(channel_id)

    while True:
        try:
            msg: Message = await queue.get()
        except asyncio.CancelledError:
            break

        # Look up the participant's voice profile
        participant = ch.get_participant(msg.sender_key)
        voice_id = participant.voice_id if participant else None

        # Resolve a stored voice profile from the DB; fall back to treating
        # the voice_id as a raw edge-tts voice name
        voice = None
        if voice_id:
            with SessionLocal() as db:
                voice = get_voice_profile(voice_id, db)
        if voice is None:
            voice = VoiceProfile(
                voice_id=voice_id or "default",
                display_name=msg.handle,
                engine="edge-tts",
                settings={"voice": voice_id or "en-US-AriaNeural"},
            )

        try:
            backend = get_backend(voice.engine)
            audio_bytes = await backend.synthesize(msg.text, voice)
            await mixer.enqueue(audio_bytes, msg.handle)
        except Exception as e:
            logger.error(f"TTS error for [{msg.handle}]: {e}")


def _start_tts_worker(channel_id: str) -> None:
    """Start a TTS worker for a channel if not already running."""
    if channel_id not in _tts_tasks or _tts_tasks[channel_id].done():
        _tts_tasks[channel_id] = asyncio.create_task(_tts_worker(channel_id))


def _stop_tts_worker(channel_id: str) -> None:
    """Stop the TTS worker for a channel."""
    task = _tts_tasks.pop(channel_id, None)
    if task and not task.done():
        task.cancel()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the database and bring back the channels we left running."""
    init_db()
    with SessionLocal() as db:
        restored = ch.restore_channels(db)
    for channel in restored:
        _start_tts_worker(channel.channel_id)
    if restored:
        logger.info(f"🎙️  Restored {len(restored)} channel(s) from the database")
    logger.info("🎙️  Agent PTT server started")
    yield
    # Cleanup
    for channel_id in list(_tts_tasks.keys()):
        _stop_tts_worker(channel_id)
    logger.info("🎙️  Agent PTT server stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Agent PTT",
    description="Voice channels for AI agents",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Web UI — a single-page spectator/participant interface served at /ui
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def root():
    """Redirect the bare root to the web UI."""
    return RedirectResponse(url="/ui/")


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class CreateChannelRequest(BaseModel):
    name: str


class JoinChannelRequest(BaseModel):
    handle: str
    voice_id: str | None = None


class SayRequest(BaseModel):
    key_id: str
    text: str


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@app.post("/channels")
async def create_channel(req: CreateChannelRequest, db: Session = Depends(get_db)):
    """Create a new voice channel."""
    channel = ch.create_channel(req.name, db=db)
    _start_tts_worker(channel.channel_id)
    return channel.model_dump()


@app.get("/channels")
async def list_channels():
    """List all active channels."""
    return [c.model_dump() for c in ch.list_channels()]


@app.get("/channels/{channel_id}")
async def get_channel_detail(channel_id: str):
    """Get a single channel by ID."""
    channel = ch.get_channel(channel_id)
    if channel is None:
        return JSONResponse({"error": "Channel not found"}, status_code=404)
    return channel.model_dump()


@app.post("/channels/{channel_id}/join")
async def join_channel(
    channel_id: str,
    req: JoinChannelRequest,
    db: Session = Depends(get_db),
):
    """Join a channel and receive a participation key.

    Without an explicit voice_id, a deterministic voice is designed for
    the handle and pinned in the DB, so the handle always sounds the same.
    """
    voice_id = req.voice_id
    designed = None
    if voice_id is None:
        engine = "omnivoice" if has_backend("omnivoice") else "edge-tts"
        # In a thread: first LLM-based design can take seconds (model load)
        designed = await asyncio.to_thread(get_or_create_pinned_voice, req.handle, db, engine)
        voice_id = designed.voice_id

    key = ch.join_channel(channel_id, req.handle, voice_id, db=db)
    if key is None:
        return JSONResponse({"error": "Channel not found"}, status_code=404)

    result = key.model_dump()
    if designed is not None:
        result["designed_voice"] = designed.model_dump()
    return result


@app.post("/channels/{channel_id}/leave")
async def leave_channel(channel_id: str, key_id: str):
    """Leave a channel."""
    success = ch.leave_channel(key_id)
    if not success:
        return JSONResponse({"error": "Participant not found"}, status_code=404)
    return {"status": "left"}


@app.post("/channels/{channel_id}/say")
async def say_message(
    channel_id: str,
    req: SayRequest,
    db: Session = Depends(get_db),
):
    """Post a message via REST — same pipeline as the agent WebSocket.

    The message is added to history, persisted, queued for TTS playback,
    and broadcast to connected WebSocket agents.
    """
    if not req.text.strip():
        return JSONResponse({"error": "Text must not be empty"}, status_code=422)

    participant = ch.get_participant(req.key_id)
    if participant is None or participant.channel_id != channel_id:
        return JSONResponse({"error": "Participant not found in channel"}, status_code=404)

    msg = await ch.send_message(req.key_id, req.text, db=db)
    if msg is None:
        return JSONResponse({"error": "Participant not found in channel"}, status_code=404)

    await _broadcast_to_channel(
        channel_id,
        {
            "type": "message",
            "handle": msg.handle,
            "text": msg.text,
            "message_id": msg.message_id,
            "timestamp": msg.timestamp.isoformat(),
        },
    )
    return msg.model_dump()


@app.get("/channels/{channel_id}/history")
async def get_history(channel_id: str):
    """Get message history for a channel."""
    messages = ch.get_history(channel_id)
    return [m.model_dump() for m in messages]


@app.get("/voices")
async def list_voices(engine: str = "omnivoice"):
    """List designed voices from the active TTS engine."""
    backend = get_backend(engine)
    voices = await backend.list_voices()
    return [v.model_dump() for v in voices]


@app.post("/voices/profiles")
async def save_profile(profile: VoiceProfile, db: Session = Depends(get_db)):
    """Create or update a stored voice profile."""
    return save_voice_profile(profile, db).model_dump()


@app.get("/voices/profiles")
async def list_profiles(engine: str | None = None, db: Session = Depends(get_db)):
    """List stored voice profiles, optionally filtered by engine."""
    return [p.model_dump() for p in list_voice_profiles(db, engine=engine)]


@app.get("/voices/profiles/{voice_id}")
async def get_profile(voice_id: str, db: Session = Depends(get_db)):
    """Get a stored voice profile by ID."""
    profile = get_voice_profile(voice_id, db)
    if profile is None:
        return JSONResponse({"error": "Voice profile not found"}, status_code=404)
    return profile.model_dump()


@app.delete("/voices/profiles/{voice_id}")
async def delete_profile(voice_id: str, db: Session = Depends(get_db)):
    """Delete a stored voice profile."""
    if not delete_voice_profile(voice_id, db):
        return JSONResponse({"error": "Voice profile not found"}, status_code=404)
    return {"status": "deleted"}


@app.get("/voices/pinned")
async def get_pinned_voices(db: Session = Depends(get_db)):
    """List handles with auto-designed pinned voices."""
    return list_pinned_voices(db)


@app.post("/voices/pinned/{handle}/redesign")
async def redesign_voice(handle: str, db: Session = Depends(get_db)):
    """Design a fresh voice for a handle, replacing the existing pin."""
    engine = "omnivoice" if has_backend("omnivoice") else "edge-tts"
    profile = await asyncio.to_thread(redesign_pinned_voice, handle, db, engine)
    return profile.model_dump()


# ---------------------------------------------------------------------------
# WebSocket: agent communication
# ---------------------------------------------------------------------------


@app.websocket("/channels/{channel_id}/ws")
async def agent_websocket(
    websocket: WebSocket,
    channel_id: str,
    key: str | None = None,
):
    """Real-time bidirectional agent communication.

    Query param `key` is the participation key ID.
    Agents send: {"type": "message", "text": "..."}
    Server broadcasts: {"type": "message", "handle": "...", "text": "...", "timestamp": "..."}
    """
    channel = ch.get_channel(channel_id)
    if channel is None:
        await websocket.close(code=4004, reason="Channel not found")
        return

    if key and key not in channel.participants:
        await websocket.close(code=4001, reason="Invalid participation key")
        return

    await websocket.accept()

    # Register this WebSocket
    if channel_id not in _ws_clients:
        _ws_clients[channel_id] = []
    _ws_clients[channel_id].append(websocket)

    participant = ch.get_participant(key) if key else None
    handle = participant.handle if participant else "anonymous"

    # Announce join
    await _broadcast_to_channel(
        channel_id,
        {
            "type": "system",
            "text": f"{handle} joined the channel",
        },
        exclude=websocket,
    )

    # Get a DB session for message persistence
    db = next(get_db())

    try:
        while True:
            data = await websocket.receive_json()

            if data.get("type") == "message" and key:
                text = data.get("text", "")
                if text.strip():
                    msg = await ch.send_message(key, text, db=db)
                    if msg:
                        await _broadcast_to_channel(
                            channel_id,
                            {
                                "type": "message",
                                "handle": msg.handle,
                                "text": msg.text,
                                "message_id": msg.message_id,
                                "timestamp": msg.timestamp.isoformat(),
                            },
                        )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        db.close()
        if channel_id in _ws_clients:
            with contextlib.suppress(ValueError):
                _ws_clients[channel_id].remove(websocket)

        # Announce leave
        await _broadcast_to_channel(
            channel_id,
            {
                "type": "system",
                "text": f"{handle} left the channel",
            },
        )


async def _broadcast_to_channel(
    channel_id: str,
    data: dict,
    exclude: WebSocket | None = None,
) -> None:
    """Broadcast a JSON message to all WebSocket clients in a channel."""
    clients = _ws_clients.get(channel_id, [])
    dead: list[WebSocket] = []

    for ws in clients:
        if ws is exclude:
            continue
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)

    for ws in dead:
        with contextlib.suppress(ValueError):
            clients.remove(ws)


# ---------------------------------------------------------------------------
# WebSocket: spectator audio stream
# ---------------------------------------------------------------------------


@app.websocket("/channels/{channel_id}/audio")
async def spectator_audio_stream(websocket: WebSocket, channel_id: str):
    """Read-only audio stream for spectators.

    Delivers raw audio bytes as binary WebSocket frames.
    No auth required — anyone with the channel ID can listen.
    """
    channel = ch.get_channel(channel_id)
    if channel is None:
        await websocket.close(code=4004, reason="Channel not found")
        return

    await websocket.accept()

    mixer = get_mixer(channel_id)
    listener_queue = mixer.register_stream_listener()

    try:
        while True:
            audio_bytes = await listener_queue.get()
            await websocket.send_bytes(audio_bytes)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Audio stream error: {e}")
    finally:
        mixer.unregister_stream_listener(listener_queue)


# ---------------------------------------------------------------------------
# Static web UI mount — kept last so it never shadows API/WebSocket routes
# ---------------------------------------------------------------------------

app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")
