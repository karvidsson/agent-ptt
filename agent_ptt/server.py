"""FastAPI WebSocket server — REST endpoints + real-time agent communication."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent_ptt import channel as ch
from agent_ptt import mentions, workspace
from agent_ptt.audio import get_mixer, remove_mixer
from agent_ptt.auth import (
    WS_CLOSE_UNAUTHORIZED,
    require_api_key,
    require_delete_all_confirmation,
    websocket_authorized,
)
from agent_ptt.db import SessionLocal, get_db, init_db
from agent_ptt.mention_policy import mention_policy
from agent_ptt.models import (
    Message,
    MessageContext,
    PinnedVoiceDB,
    RevokedParticipantDB,
    SessionIdentityDB,
    VoiceProfile,
)
from agent_ptt.session_identity import get_session_identity
from agent_ptt.tts import get_backend
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
from agent_ptt.workspace_auth import hosted, public_origin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connected WebSocket clients per channel
# ---------------------------------------------------------------------------

_ws_clients: dict[str, list[WebSocket]] = {}
_audio_clients: dict[str, list[WebSocket]] = {}


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

        # Notices are text-only; actions are spoken in the third person.
        if msg.kind == "notice":
            continue
        spoken_text = f"{msg.handle} {msg.text}" if msg.kind == "action" else msg.text

        # Look up the participant's voice profile
        participant = ch.get_participant(msg.sender_key)
        with SessionLocal() as moderation_db:
            if moderation_db.get(RevokedParticipantDB, msg.sender_key):
                continue
        voice_id = participant.voice_id if participant else None

        # Resolve a stored voice profile from the DB; fall back to treating
        # the voice_id as a raw pocket-tts voice name
        voice = None
        if voice_id:
            with SessionLocal() as db:
                voice = get_voice_profile(voice_id, db)
        if voice is None:
            voice = VoiceProfile(
                voice_id=voice_id or "default",
                display_name=msg.handle,
                engine="pocket-tts",
                settings={"voice": voice_id or "alba"},
            )

        try:
            backend = get_backend(voice.engine)
            audio_bytes = await backend.synthesize(spoken_text, voice)
            with SessionLocal() as moderation_db:
                revoked = moderation_db.get(RevokedParticipantDB, msg.sender_key) is not None
            if not revoked:
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
    if hosted():
        public_origin()  # Validate deployment origin before accepting traffic.
    init_db()
    with SessionLocal() as db:
        restored = [] if hosted() else ch.restore_channels(db)
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


@app.get("/health/live", include_in_schema=False)
def health_live():
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
def health_ready(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}


@app.get("/")
async def root():
    """Redirect the bare root to the web UI."""
    return RedirectResponse(url="/workspace/" if hosted() else "/ui/")


# Every REST route below requires the API key when AGENT_PTT_API_KEY is set
# (see agent_ptt/auth.py). The landing redirect above, the /ui mount and the
# WebSockets are registered on the app directly; the WebSockets check the key
# themselves so they can close with a WebSocket code instead of an HTTP error.
api = APIRouter(dependencies=[Depends(require_api_key)])


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class CreateChannelRequest(BaseModel):
    name: str
    reuse_existing: bool = False


class JoinChannelRequest(BaseModel):
    handle: str
    voice_id: str | None = None
    session_id: str | None = Field(default=None, min_length=1, max_length=256)
    agent: str = Field(default="cli", min_length=1, max_length=64)
    receive_mentions: bool = False


class SayRequest(BaseModel):
    key_id: str
    text: str
    recipient_ids: list[str] | None = Field(default=None, max_length=20)
    # The detail behind the line; stored, never spoken, pulled via
    # GET /channels/{id}/messages/{message_id}.
    context: MessageContext | None = None


class InboxAckRequest(BaseModel):
    message_ids: list[str] = Field(min_length=1, max_length=20)


class CommandRequest(BaseModel):
    key_id: str
    name: str
    args: str = ""


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@api.post("/channels")
async def create_channel(req: CreateChannelRequest, db: Session = Depends(get_db)):
    """Create a channel, optionally reusing a same-name room atomically."""
    if req.reuse_existing:
        for existing in ch.list_channels(include_archived=True):
            if existing.name == req.name:
                if existing.archived:
                    raise HTTPException(409, "Channel is archived; reopen it to resume activity")
                return existing.model_dump()
    channel = ch.create_channel(req.name, db=db)
    _start_tts_worker(channel.channel_id)
    return channel.model_dump()


@api.get("/channels")
async def list_channels(include_archived: bool = False):
    """List all active channels."""
    return [c.model_dump() for c in ch.list_channels(include_archived=include_archived)]


@api.delete("/channels", dependencies=[Depends(require_delete_all_confirmation)])
async def delete_all_channels(db: Session = Depends(get_db)):
    """Delete every channel and stop its workers and audio mixers.

    Requires the header ``X-Confirm-Delete-All: yes`` on top of the API key.
    """
    channel_ids = [channel.channel_id for channel in ch.list_channels(include_archived=True)]
    for channel_id in channel_ids:
        for websocket in _ws_clients.pop(channel_id, []):
            with contextlib.suppress(Exception):
                await websocket.close(code=4004, reason="Channels cleared")
        _stop_tts_worker(channel_id)
        remove_mixer(channel_id)
        ch.delete_channel(channel_id, db=db)
    return {"deleted": len(channel_ids)}


@api.delete("/channels/{channel_id}")
async def delete_one_channel(channel_id: str, db: Session = Depends(get_db)):
    """Delete one channel when no agents are currently connected."""
    channel = ch.get_channel(channel_id)
    if channel is None:
        return JSONResponse({"error": "Channel not found"}, status_code=404)
    if channel.participants:
        return JSONResponse(
            {"error": "Channel has connected agents"},
            status_code=409,
        )

    for websocket in _ws_clients.pop(channel_id, []):
        with contextlib.suppress(Exception):
            await websocket.close(code=4004, reason="Channel deleted")
    _stop_tts_worker(channel_id)
    remove_mixer(channel_id)
    ch.delete_channel(channel_id, db=db)
    return {"deleted": channel_id}


@api.get("/channels/{channel_id}/participants/{key_id}")
async def participant_details(channel_id: str, key_id: str, db: Session = Depends(get_db)):
    """Read-only participant details for spectators and the roster inspector."""
    channel = ch.get_channel(channel_id)
    participant = channel.participants.get(key_id) if channel else None
    if participant is None:
        raise HTTPException(404, "Participant is no longer in this channel")
    identity = db.query(SessionIdentityDB).filter_by(handle=participant.handle).first()
    voice = get_voice_profile(participant.voice_id, db) if participant.voice_id else None
    pin = db.get(PinnedVoiceDB, participant.handle.lower())
    messages = [message for message in channel.messages if message.handle == participant.handle]
    context = next((message.context for message in reversed(messages) if message.context), None)
    return {
        "harness": identity.agent if identity else context.agent if context else None,
        "context": context.model_dump(exclude_none=True) if context else None,
        "participant": participant.model_dump(),
        "channel": {
            "channel_id": channel.channel_id,
            "name": channel.name,
            "archived": channel.archived,
        },
        "session": {
            "session_id": identity.session_id,
            "agent": identity.agent,
            "created_at": identity.created_at,
        }
        if identity
        else None,
        "voice": voice.model_dump() if voice else None,
        "voice_pin": {
            "handle": pin.handle,
            "voice_id": pin.voice_id,
            "source": pin.source,
            "created_at": pin.created_at,
        }
        if pin
        else None,
        "connections": [
            p.model_dump() for p in channel.participants.values() if p.handle == participant.handle
        ],
        "activity": {
            "message_count": len(messages),
            "last_message": _message_payload(messages[-1], with_context=True) if messages else None,
        },
    }


@api.post("/channels/{channel_id}/kick/{key_id}")
async def kick_participant(channel_id: str, key_id: str, db: Session = Depends(get_db)):
    channel = ch.get_channel(channel_id)
    participant = channel.participants.get(key_id) if channel else None
    if participant is None:
        raise HTTPException(404, "Participant not found in channel")
    # The UI groups duplicate connections under one handle; remove all of them.
    keys = [p.key_id for p in channel.participants.values() if p.handle == participant.handle]
    for removed_key in keys:
        ch.revoke_participant(channel_id, removed_key, db)
    for ws in list(_ws_clients.get(channel_id, [])):
        if ws.query_params.get("key") in keys:
            with contextlib.suppress(Exception):
                await ws.close(code=4003, reason="Removed from channel")
    return {"removed": keys}


@api.post("/channels/{channel_id}/archive")
async def archive_channel(channel_id: str, db: Session = Depends(get_db)):
    channel = ch.set_archived(channel_id, True, db)
    if channel is None:
        raise HTTPException(404, "Channel not found")
    _stop_tts_worker(channel_id)
    remove_mixer(channel_id)
    sockets = _ws_clients.pop(channel_id, []) + _audio_clients.pop(channel_id, [])
    for ws in sockets:
        with contextlib.suppress(Exception):
            await ws.close(code=4003, reason="Channel archived")
        queue = getattr(ws.state, "listener_queue", None)
        if queue is not None:
            queue.put_nowait(b"")
    return channel.model_dump()


@api.post("/channels/{channel_id}/reopen")
async def reopen_channel(channel_id: str, db: Session = Depends(get_db)):
    channel = ch.set_archived(channel_id, False, db)
    if channel is None:
        raise HTTPException(404, "Channel not found")
    _start_tts_worker(channel_id)
    return channel.model_dump()


@api.get("/channels/{channel_id}")
async def get_channel_detail(channel_id: str):
    """Get a single channel by ID."""
    channel = ch.get_channel(channel_id)
    if channel is None:
        return JSONResponse({"error": "Channel not found"}, status_code=404)
    return channel.model_dump()


@api.post("/channels/{channel_id}/join")
async def join_channel(
    channel_id: str,
    req: JoinChannelRequest,
    db: Session = Depends(get_db),
):
    """Join a channel and receive a participation key.

    Without an explicit voice_id, a deterministic voice is designed for
    the handle and pinned in the DB, so the handle always sounds the same.
    """
    if ch.get_channel(channel_id) is None:
        return JSONResponse({"error": "Channel not found"}, status_code=404)
    if ch.get_channel(channel_id).archived:
        raise HTTPException(409, "Channel is archived")
    if req.receive_mentions and not req.session_id:
        raise HTTPException(422, "Receiving mentions requires a stable session_id")
    handle = req.handle
    voice_id = req.voice_id
    designed = None
    if req.session_id:
        identity = await asyncio.to_thread(get_session_identity, req.session_id, req.agent, db)
        handle, voice_id = identity.handle, identity.voice_id
        # Retries and concurrent hooks should share one participant in this channel.
        channel = ch.get_channel(channel_id)
        if channel:
            for participant in channel.participants.values():
                if participant.handle == handle and participant.voice_id == voice_id:
                    if req.receive_mentions:
                        mentions.register_receiver(participant, req.session_id, req.agent, db)
                    return {
                        **participant.model_dump(),
                        "session_id": req.session_id,
                        "agent": req.agent,
                        **({"mention_policy": mention_policy()} if req.receive_mentions else {}),
                    }
    elif voice_id is None:
        engine = "pocket-tts"
        # Keep synchronous profile persistence off the event loop
        designed = await asyncio.to_thread(get_or_create_pinned_voice, req.handle, db, engine)
        voice_id = designed.voice_id

    key = ch.join_channel(channel_id, handle, voice_id, db=db)
    if key is None:
        return JSONResponse({"error": "Channel not found"}, status_code=404)

    if req.receive_mentions:
        mentions.register_receiver(key, req.session_id, req.agent, db)

    result = key.model_dump()
    if req.receive_mentions:
        result["mention_policy"] = mention_policy()
    if req.session_id:
        result.update(session_id=req.session_id, agent=req.agent)
    if designed is not None:
        result["designed_voice"] = designed.model_dump()
    return result


@api.post("/channels/{channel_id}/leave")
async def leave_channel(channel_id: str, key_id: str):
    """Leave a channel."""
    success = ch.leave_channel(key_id)
    if not success:
        return JSONResponse({"error": "Participant not found"}, status_code=404)
    return {"status": "left"}


@api.post("/channels/{channel_id}/say")
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

    if db.get(RevokedParticipantDB, req.key_id):
        raise HTTPException(403, "Participant was removed; join explicitly to return")
    participant = ch.get_participant(req.key_id)
    if participant is None or participant.channel_id != channel_id:
        return JSONResponse({"error": "Participant not found in channel"}, status_code=404)

    try:
        msg = await ch.send_message(
            req.key_id,
            req.text,
            db=db,
            recipient_ids=req.recipient_ids,
            context=req.context,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if msg is None:
        return JSONResponse({"error": "Participant not found in channel"}, status_code=404)

    await _broadcast_to_channel(channel_id, _message_event(msg))
    return _message_payload(msg, with_context=True)


def _message_payload(msg: Message, with_context: bool) -> dict:
    """A message for history and single-message responses.

    Always carries ``has_context`` and ``context_summary``; the full
    ``context`` only when asked for, so transcripts stay light.
    """
    payload = msg.model_dump()
    payload["has_context"] = msg.context is not None
    payload["context_summary"] = ch.context_summary(msg)
    if not with_context:
        payload.pop("context", None)
    return payload


@api.get("/channels/{channel_id}/history")
async def get_history(
    channel_id: str,
    with_context: bool = False,
    db: Session = Depends(get_db),
):
    """Get message history for a channel.

    Each entry carries ``has_context`` and ``context_summary``; pass
    ``with_context=1`` to include every message's full ``context``.
    """
    messages = ch.get_history(channel_id)
    mentions.attach_receipts(messages, channel_id, db)
    return [_message_payload(m, with_context=with_context) for m in messages]


@api.get("/channels/{channel_id}/messages/{message_id}")
async def get_message(channel_id: str, message_id: str, db: Session = Depends(get_db)):
    """One message with its full ``context`` — what an agent pulls when a
    spoken line makes it curious."""
    msg = ch.get_message(channel_id, message_id, db=db)
    if msg is None:
        return JSONResponse({"error": "Message not found"}, status_code=404)
    return _message_payload(msg, with_context=True)


@api.post("/channels/{channel_id}/command")
async def run_channel_command(
    channel_id: str,
    req: CommandRequest,
    db: Session = Depends(get_db),
):
    """Run an IRC-style command (names, whois, me, notice, topic, away, back).

    Returns ``{"ok": true, "name": ..., "result": {...}}``; 400 for an
    unknown command, 404 for a key that isn't in the channel or a whois
    miss. Side effects are broadcast to the channel's WebSocket clients.
    """
    if ch.get_channel(channel_id) is None:
        return JSONResponse({"error": "Channel not found"}, status_code=404)
    if db.get(RevokedParticipantDB, req.key_id):
        raise HTTPException(403, "Participant was removed; join explicitly to return")
    try:
        result = await ch.run_command(channel_id, req.key_id, req.name, req.args, db=db)
    except ch.CommandError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    name = req.name.strip().lower()
    await _broadcast_command(channel_id, name, req.args, result)
    return {"ok": True, "name": name, "result": result}


def inbox_recipient(channel_id: str, x_participant_key: str = Header()) -> str:
    participant = ch.get_participant(x_participant_key)
    if not participant or participant.channel_id != channel_id or not participant.mention_id:
        raise HTTPException(403, "Not a mention receiver in this channel")
    return participant.mention_id


@api.get("/channels/{channel_id}/inbox")
async def get_inbox(
    channel_id: str,
    recipient_id: str = Depends(inbox_recipient),
    limit: int = Query(default=6, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Read pending mentions without consuming them. Requires X-Participant-Key."""
    return mentions.pending(channel_id, recipient_id, db, limit)


@api.post("/channels/{channel_id}/inbox/ack")
async def ack_inbox(
    channel_id: str,
    req: InboxAckRequest,
    recipient_id: str = Depends(inbox_recipient),
    db: Session = Depends(get_db),
):
    """Acknowledge handoff to the agent integration, not completion of its task."""
    try:
        mentions.acknowledge(channel_id, recipient_id, req.message_ids, db)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    await _broadcast_to_channel(channel_id, {"type": "delivery", "message_ids": req.message_ids})
    return {"status": "delivered"}


@api.get("/voices")
async def list_voices(engine: str = "pocket-tts"):
    """List designed voices from the active TTS engine."""
    if engine != "pocket-tts":
        raise HTTPException(400, "Only Pocket TTS is supported")
    backend = get_backend(engine)
    voices = await backend.list_voices()
    return [v.model_dump() for v in voices]


@api.post("/voices/profiles")
async def save_profile(profile: VoiceProfile, db: Session = Depends(get_db)):
    """Create or update a stored voice profile."""
    if profile.engine != "pocket-tts":
        raise HTTPException(400, "Only Pocket TTS is supported")
    return save_voice_profile(profile, db).model_dump()


@api.get("/voices/profiles")
async def list_profiles(engine: str | None = None, db: Session = Depends(get_db)):
    """List stored voice profiles, optionally filtered by engine."""
    return [p.model_dump() for p in list_voice_profiles(db, engine=engine)]


@api.get("/voices/profiles/{voice_id}")
async def get_profile(voice_id: str, db: Session = Depends(get_db)):
    """Get a stored voice profile by ID."""
    profile = get_voice_profile(voice_id, db)
    if profile is None:
        return JSONResponse({"error": "Voice profile not found"}, status_code=404)
    return profile.model_dump()


@api.delete("/voices/profiles/{voice_id}")
async def delete_profile(voice_id: str, db: Session = Depends(get_db)):
    """Delete a stored voice profile."""
    if not delete_voice_profile(voice_id, db):
        return JSONResponse({"error": "Voice profile not found"}, status_code=404)
    return {"status": "deleted"}


@api.get("/voices/pinned")
async def get_pinned_voices(db: Session = Depends(get_db)):
    """List handles with auto-designed pinned voices."""
    return list_pinned_voices(db)


@api.post("/voices/pinned/{handle}/redesign")
async def redesign_voice(handle: str, db: Session = Depends(get_db)):
    """Design a fresh voice for a handle, replacing the existing pin."""
    engine = "pocket-tts"
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
    Agents send: {"type": "message", "text": "...", "context": {...}?} or
    {"type": "command", "name": "...", "args": "..."}.
    Server broadcasts: {"type": "message", "handle": "...", "text": "...",
    "kind": "...", "timestamp": "...", "has_context": bool,
    "context_summary": {...} | null}; commands are answered on the sending
    socket with {"type": "result", ...} or {"type": "error", ...}. An invalid
    message (bad mention, malformed context) gets an error frame on the
    sending socket and the connection stays open.
    """
    if not websocket_authorized(websocket):
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="Unauthorized")
        return

    channel = ch.get_channel(channel_id)
    if channel is None:
        await websocket.close(code=4004, reason="Channel not found")
        return

    if channel.archived:
        await websocket.close(code=4003, reason="Channel archived")
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
                    try:
                        req = SayRequest(
                            key_id=key,
                            text=text,
                            recipient_ids=data.get("recipient_ids"),
                            context=data.get("context"),
                        )
                        msg = await ch.send_message(
                            key,
                            req.text,
                            db=db,
                            recipient_ids=req.recipient_ids,
                            context=req.context,
                        )
                    except ValueError as exc:
                        # Pydantic's ValidationError is a ValueError too.
                        detail = str(exc)
                        await websocket.send_json(
                            {"type": "error", "error": detail, "detail": detail}
                        )
                        continue
                    if msg:
                        await _broadcast_to_channel(channel_id, _message_event(msg))
            elif data.get("type") == "command" and key:
                name = str(data.get("name") or "")
                args = str(data.get("args") or "")
                try:
                    result = await ch.run_command(channel_id, key, name, args, db=db)
                except ch.CommandError as exc:
                    await websocket.send_json({"type": "error", "error": exc.message})
                    continue
                await websocket.send_json({"type": "result", "name": name, "result": result})
                await _broadcast_command(channel_id, name, args, result)
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


def _message_event(msg: Message) -> dict:
    """The ``{"type": "message"}`` event broadcast to agent WebSockets."""
    return {
        "type": "message",
        "handle": msg.handle,
        "text": msg.text,
        "kind": msg.kind,
        "message_id": msg.message_id,
        "timestamp": msg.timestamp.isoformat(),
        "deliveries": msg.deliveries,
        "has_context": msg.context is not None,
        "context_summary": ch.context_summary(msg),
    }


async def _broadcast_command(channel_id: str, name: str, args: str, result: dict) -> None:
    """Broadcast a command's side effect, per the IRC-commands contract."""
    if name in ("me", "notice"):
        msg = Message.model_validate(result["message"])
        await _broadcast_to_channel(channel_id, _message_event(msg))
    elif name == "topic" and args.strip():
        await _broadcast_to_channel(
            channel_id,
            {"type": "topic", "topic": result["topic"], "handle": result["topic_set_by"]},
        )
    elif name == "away":
        participant = result["participant"]
        await _broadcast_to_channel(
            channel_id,
            {
                "type": "presence",
                "handle": participant["handle"],
                "state": "away",
                "reason": participant["away_reason"],
            },
        )
    elif name == "back":
        participant = result["participant"]
        await _broadcast_to_channel(
            channel_id,
            {"type": "presence", "handle": participant["handle"], "state": "active"},
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
    Needs the API key when AGENT_PTT_API_KEY is set (header or `api_key`
    query param); otherwise anyone with the channel ID can listen.
    """
    if not websocket_authorized(websocket):
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="Unauthorized")
        return

    channel = ch.get_channel(channel_id)
    if channel is None:
        await websocket.close(code=4004, reason="Channel not found")
        return

    if channel.archived:
        await websocket.close(code=4003, reason="Channel archived")
        return
    await websocket.accept()
    _audio_clients.setdefault(channel_id, []).append(websocket)

    mixer = get_mixer(channel_id)
    listener_queue = mixer.register_stream_listener()
    websocket.state.listener_queue = listener_queue

    try:
        while True:
            audio_bytes = await listener_queue.get()
            if not audio_bytes:
                break
            await websocket.send_bytes(audio_bytes)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Audio stream error: {e}")
    finally:
        mixer.unregister_stream_listener(listener_queue)
        with contextlib.suppress(ValueError):
            _audio_clients.get(channel_id, []).remove(websocket)


# ---------------------------------------------------------------------------
# Static web UI mount — kept last so it never shadows API/WebSocket routes
# ---------------------------------------------------------------------------

app.include_router(api)
app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")


# Hosted organization routes use their own identity and tenant boundary.
workspace.install(app)
