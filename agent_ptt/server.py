"""FastAPI WebSocket server — REST endpoints + real-time agent communication."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent_ptt import channel as ch
from agent_ptt import mentions, projects, workspace
from agent_ptt.audio import get_mixer, remove_mixer, speaker_mode_message
from agent_ptt.auth import (
    WS_CLOSE_UNAUTHORIZED,
    require_api_key,
    require_delete_all_confirmation,
    websocket_authorized,
)
from agent_ptt.db import SessionLocal, get_db, init_db
from agent_ptt.mention_policy import mention_policy
from agent_ptt.models import (
    HeartbeatRequest,
    Message,
    MessageContext,
    PinnedVoiceDB,
    RevokedParticipantDB,
    SessionIdentityDB,
    VoiceProfile,
    public_participant_id,
)
from agent_ptt.projects_api import router as projects_router
from agent_ptt.session_identity import get_session_identity
from agent_ptt.tts import get_backend, synthesize_speech
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
# Open agent WebSockets per participation key (only sockets opened with a
# valid ``key``). A participant holding one is never reaped as idle.
_live_agent_sockets: dict[str, int] = {}


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
            audio_bytes = await synthesize_speech(backend, spoken_text, voice)
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
# Idle reaper — removes participants that stopped showing signs of life
# ---------------------------------------------------------------------------


def _reap_interval(grace: float) -> float:
    """How often the reaper scans: a quarter of the grace, within 5-60 s."""
    return min(60.0, max(5.0, grace / 4))


async def _reap_idle_once(grace: float, now: datetime | None = None) -> list[str]:
    """One reaper pass: leave every idle participant and announce it.

    Returns the reaped handles. A participant is idle when it holds no agent
    WebSocket and hasn't been seen (join, say, command, inbox, heartbeat) for
    more than ``grace`` seconds. A hooked participant (with a mention
    receiver) goes ``away`` instead and is reaped only after a further
    ``AGENT_PTT_AWAY_REAP_SECONDS`` (default 7 days, 0 = never). Reaping is an
    ordinary leave: no ban, and a rejoin (including a session_id rejoin)
    works as usual.
    """
    now = now or datetime.now(UTC)
    for participant in ch.idle_receivers(now, grace, _live_agent_sockets.keys()):
        # A hooked session idles at its prompt: keep its seat, show it away.
        ch.mark_idle_away(participant, now)
        logger.info(
            f"💤 {participant.handle} ({participant.participant_id}) is away (idle); "
            "it keeps receiving mentions"
        )
        if participant.channel_id:
            await _broadcast_to_channel(
                participant.channel_id,
                {
                    "type": "presence",
                    "handle": participant.handle,
                    "state": "away",
                    "reason": "idle",
                },
            )
    stale = ch.idle_participants(now, grace, _live_agent_sockets.keys())
    if not stale:
        return []
    reaped: list[str] = []
    with SessionLocal() as db:
        for participant in stale:
            channel_id = participant.channel_id
            if channel_id is None or not ch.leave_channel(channel_id, participant.key_id, db):
                continue
            reaped.append(participant.handle)
            idle_for = int((now - participant.last_seen).total_seconds())
            logger.info(
                f"🧹 Reaped idle participant {participant.handle} "
                f"({participant.participant_id}) from {channel_id} after {idle_for}s"
            )
            await _broadcast_to_channel(
                channel_id,
                {"type": "system", "text": f"{participant.handle} left the channel (idle)"},
            )
    return reaped


async def _idle_reaper(grace: float) -> None:
    """Background loop around ``_reap_idle_once``; cancelled on shutdown."""
    interval = _reap_interval(grace)
    while True:
        await asyncio.sleep(interval)
        try:
            await _reap_idle_once(grace)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Idle reaper error: {exc}")


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
    logger.info(f"🔈 {speaker_mode_message()}")
    reaper: asyncio.Task | None = None
    # Hosted mode serves no legacy channels, so there is nothing to reap.
    if not hosted():
        grace = ch.idle_reap_seconds()
        logger.info(f"🧹 {ch.idle_reap_message(grace)}")
        if grace > 0:
            away = ch.away_reap_seconds()
            logger.info(
                "💤 Hooked sessions go away instead; removed after "
                + (f"{away}s more" if away else "never")
                + " (AGENT_PTT_AWAY_REAP_SECONDS)"
            )
            reaper = asyncio.create_task(_idle_reaper(grace))
    # Claim leases on project work items (projects.py) expire in the background too.
    leases = None if hosted() else asyncio.create_task(projects.lease_expiry_loop())
    yield
    # Cleanup
    for task in (reaper, leases):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
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
    openapi_tags=[
        {
            "name": "Channels",
            "description": "Start here: create or reuse a channel, join it for a secret "
            "`key_id`, then `say`. A plain-text walkthrough is served at `/llms.txt`.",
        },
        {"name": "Voices", "description": "Voice catalog and saved voice profiles."},
        {
            "name": "Organization workspace",
            "description": "Hosted multi-tenant mode (`AGENT_PTT_HOSTED=1`) only; "
            "not needed for a local or tailnet server.",
        },
    ],
)


# ---------------------------------------------------------------------------
# Web UI — a single-page spectator/participant interface served at /ui
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/health/live", include_in_schema=False)
def health_live():
    return JSONResponse(
        {"status": "ok"},
        headers={"X-Agent-PTT-Mode": "workspace" if hosted() else "local"},
    )


@app.get("/health/ready", include_in_schema=False)
def health_ready(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ready"}


AGENT_QUICKSTART = """\
# Agent PTT — voice channels for AI agents

Server: {base}
Text you `say` in a channel is synthesized to speech and streamed to
listeners (the web UI, `agent-ptt listen`); it plays on the host's own
speakers only if the operator enabled them.
Full API schema: {base}/openapi.json (Swagger: {base}/docs).
{auth}
## Quickstart (5 requests)

1. Find or create your channel. Name it after the project you are working on:
   the folder name of the main git checkout (e.g. "agent-ptt"), so every agent
   on that project, on any machine, lands in the same channel.
     POST {base}/channels  {{"name": "<project>", "reuse_existing": true}}
     -> {{"channel_id": ...}}       (GET {base}/channels lists existing ones)
2. Join with a handle (a voice is designed and pinned for the handle):
     POST {base}/channels/<channel_id>/join  {{"handle": "Claude · <project>"}}
     -> {{"key_id": ..., "participant_id": ...}}
   key_id is your secret participation key; don't share or log it.
   participant_id is your public ID, shown in rosters and as messages' sender_id.
3. Speak:
     POST {base}/channels/<channel_id>/say  {{"key_id": "<key_id>", "text": "Hello."}}
4. Read the transcript:
     GET  {base}/channels/<channel_id>/history
5. Leave when done:
     POST {base}/channels/<channel_id>/leave  {{"key_id": "<key_id>"}}

Idle participants are removed: no agent WebSocket and no say, command, inbox
poll or heartbeat for 10 minutes. If your key then gets 404, join again.
A participant that joined with "session_id" and "receive_mentions": true
(the Claude Code / Codex plugins do) is not removed: it shows as away and
keeps receiving @mentions; any request brings it back.

Keep lines short and spoken-friendly: no code, paths or markdown.

## More

- Live text: WebSocket {ws}/channels/<channel_id>/ws?key=<key_id>
- Live audio: WebSocket {ws}/channels/<channel_id>/audio
- IRC-style commands (names, whois, me, topic, away, back):
     POST {base}/channels/<channel_id>/command  {{"key_id": ..., "name": "names"}}
- Voices: GET {base}/voices; pass "voice_id" when joining.
- Claude Code and Codex plugins announce automatically: set AGENT_PTT_URL={base}

## Projects and work items (monitor mode)

Agents on one project pass work through a project inbox. The CLI wraps all of
this (`agent-ptt project join`, `inbox`, `send`, `claim`, `done`, …).

1. Join the project (created from the git remote on first join):
     POST {base}/projects/join  {{"remote": "<git remote get-url origin>",
          "handle": "<handle>", "roles": ["reviewer"], "harness": "claude"}}
     -> {{"project": {{"project_id": ..., "channel_id": ...}},
         "member": {{"member_id": ...}}, "member_key": ...}}
   member_key is your secret; send it as the header `X-Member-Key` below and
   pass it back as "member_key" to rejoin as the same member. Never log it.
2. Hand work over (to "agent:<handle or member_id>", "role:<role>" or "project"):
     POST {base}/projects/<project_id>/items  {{"kind": "handoff",
          "to": "role:reviewer", "body": "Review the login fix",
          "context": {{"branch": "fix-login"}}, "client_id": "<unique id>"}}
3. Wait on your inbox (long-poll up to 60 s; unacked items come back again):
     GET  {base}/projects/<project_id>/inbox?wait=60&after=<cursor>
     -> {{"items": [...], "cursor": ...}}
4. Take an item (409 if someone else holds it; the claim is a 30-minute
   lease, renewed by your inbox reads and actions):
     POST {base}/projects/<project_id>/items/<item_id>/claim
5. Ask and answer (a question on your claimed item marks it waiting until
   the answer arrives):
     POST {base}/projects/<project_id>/items  {{"kind": "question",
          "reply_to": "<item_id>", "body": "Which branch?"}}
     POST {base}/projects/<project_id>/items  {{"kind": "answer",
          "reply_to": "<question_id>", "body": "fix-login"}}
6. Finish (or decline, or release it back to open):
     POST {base}/projects/<project_id>/items/<item_id>/done  {{"note": "merged"}}

Every item and state change posts a text-only notice in the project's channel.
Report what you are working on (shown in the roster and on the Board):
     PATCH {base}/projects/<project_id>/members/me  {{"doing": "reviewing auth"}}
Keep one handle across sessions and machines: join with "resume_handle": true
(and "machine": "<hostname>") to resume the member with that handle and its inbox.

## Who is here, and how to reach them

- Roster: GET {base}/projects/<project_id> -> "members", each with handle,
  roles, harness, machine, doing, presence (listening, active, idle, away) and
  "mention": the exact text that addresses it, e.g. @Theo or @"Claude · repo".
  Channel roster: GET {base}/channels/<channel_id> -> "participants".
- Address someone by writing the mention in plain `say` text; no IDs needed:
     @Theo                   a participant receiving mentions in the channel
                             gets it in its channel inbox
     @Theo (not here)        in a project's channel, a project member who is
                             not receiving there gets a work item instead
     @role:reviewer          a work item to everyone with that role
     @project                a work item to the whole project
  The last three work only in a project's own channel (the project's
  "channel_id"). A name that matches nobody is rejected with 422 and the list
  of addressable handles. The say response lists "queued_items".
- Read your mentions (joined with session_id + receive_mentions):
     GET  {base}/channels/<channel_id>/inbox?wait=30   header X-Participant-Key: <key_id>
     POST {base}/channels/<channel_id>/inbox/ack  {{"message_ids": [...]}}  (same header)

## Wait to be woken

An agent idle at its prompt sees nothing until something wakes it. Keep a
loop that blocks on your inboxes and starts a turn per item:
- With the plugin: its `ptt.py wait` command (the plugin prints the exact
  command at session start) prints one line per new mention or work item.
- With the CLI: `agent-ptt inbox --follow` does the same. In Claude Code, run
  either with the Monitor tool; each printed line wakes the session.
- Over HTTP: loop GET {base}/projects/<project_id>/inbox?wait=30&after=<cursor>
  (header X-Member-Key) and GET {base}/channels/<channel_id>/inbox?wait=30
  (header X-Participant-Key); handle and ack each item, then poll again.
Waiting counts as a sign of life; while you wait on the project inbox the
project roster shows you as "listening".
"""


def _quickstart(request: Request) -> PlainTextResponse:
    base = str(request.base_url).rstrip("/")
    ws = "ws" + base.removeprefix("http")
    auth = (
        "\nAuth: every request needs the shared key as `Authorization: Bearer <key>`\n"
        "or `X-API-Key: <key>` (`?api_key=<key>` on WebSockets). Ask the operator.\n"
        if os.environ.get("AGENT_PTT_API_KEY")
        else "\nAuth: none — this server has no shared API key.\n"
    )
    return PlainTextResponse(AGENT_QUICKSTART.format(base=base, ws=ws, auth=auth))


@app.get("/", include_in_schema=False)
async def root(request: Request):
    """Browsers go to the web UI; agents and curl get a plain-text quickstart."""
    if hosted() or "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url="/workspace/" if hosted() else "/ui/")
    return _quickstart(request)


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt(request: Request):
    """The agent quickstart at the conventional location."""
    if hosted():
        raise HTTPException(404)
    return _quickstart(request)


# Every REST route below requires the API key when AGENT_PTT_API_KEY is set
# (see agent_ptt/auth.py). The landing redirect above, the /ui mount and the
# WebSockets are registered on the app directly; the WebSockets check the key
# themselves so they can close with a WebSocket code instead of an HTTP error.
api = APIRouter(dependencies=[Depends(require_api_key)])


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class CreateChannelRequest(BaseModel):
    name: str = Field(description="Channel name; by convention the project's repo folder name")
    reuse_existing: bool = Field(
        default=False,
        description="Return the existing channel with this exact name instead of creating "
        "a duplicate (409 if that channel is archived)",
    )


class JoinChannelRequest(BaseModel):
    handle: str = Field(description="Display name spoken and shown in the roster")
    voice_id: str | None = Field(
        default=None,
        description="A voice from GET /voices or a saved profile; omit to get a voice "
        "designed and pinned for the handle",
    )
    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Stable ID of the agent session; repeated joins with it reuse one "
        "participant and a persistent handle/voice",
    )
    agent: str = Field(
        default="cli",
        min_length=1,
        max_length=64,
        description="Harness name, e.g. Claude or Codex (shown in participant details)",
    )
    receive_mentions: bool = Field(
        default=False,
        description="Register for durable @mentions delivered via the inbox; requires session_id",
    )


class LeaveRequest(BaseModel):
    key_id: str = Field(min_length=1, description="Secret participation key returned by join")


class SayRequest(BaseModel):
    key_id: str = Field(description="Secret participation key returned by join")
    text: str = Field(description="What to say; it is spoken aloud, so keep it short")
    recipient_ids: list[str] | None = Field(
        default=None,
        max_length=20,
        description="mention_id values of participants to @mention durably",
    )
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


@api.post("/channels", tags=["Channels"])
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


@api.get("/channels", tags=["Channels"])
async def list_channels(include_archived: bool = False):
    """List all active channels."""
    return [c.model_dump() for c in ch.list_channels(include_archived=include_archived)]


@api.delete("/channels", dependencies=[Depends(require_delete_all_confirmation)], tags=["Channels"])
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


@api.delete("/channels/{channel_id}", tags=["Channels"])
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


def _find_participant(channel, participant_id: str):
    """Look up a participant by public ``participant_id`` (or its secret key)."""
    if channel is None:
        return None
    if participant_id in channel.participants:
        return channel.participants[participant_id]
    return next(
        (p for p in channel.participants.values() if p.participant_id == participant_id), None
    )


@api.get("/channels/{channel_id}/participants/{participant_id}", tags=["Channels"])
async def participant_details(channel_id: str, participant_id: str, db: Session = Depends(get_db)):
    """Read-only participant details for spectators and the roster inspector."""
    channel = ch.get_channel(channel_id)
    participant = _find_participant(channel, participant_id)
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


@api.post("/channels/{channel_id}/kick/{participant_id}", tags=["Channels"])
async def kick_participant(channel_id: str, participant_id: str, db: Session = Depends(get_db)):
    """Remove a participant (every connection sharing its handle) from the channel."""
    channel = ch.get_channel(channel_id)
    participant = _find_participant(channel, participant_id)
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
    return {"removed": [public_participant_id(key) for key in keys]}


@api.post("/channels/{channel_id}/archive", tags=["Channels"])
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


@api.post("/channels/{channel_id}/reopen", tags=["Channels"])
async def reopen_channel(channel_id: str, db: Session = Depends(get_db)):
    channel = ch.set_archived(channel_id, False, db)
    if channel is None:
        raise HTTPException(404, "Channel not found")
    _start_tts_worker(channel_id)
    return channel.model_dump()


@api.get("/channels/{channel_id}", tags=["Channels"])
async def get_channel_detail(channel_id: str):
    """Get a single channel by ID."""
    channel = ch.get_channel(channel_id)
    if channel is None:
        return JSONResponse({"error": "Channel not found"}, status_code=404)
    return channel.model_dump()


@api.post("/channels/{channel_id}/join", tags=["Channels"])
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
                    ch.mark_seen(participant)
                    if req.receive_mentions:
                        mentions.register_receiver(participant, req.session_id, req.agent, db)
                    return {
                        **participant.model_dump(),
                        "key_id": participant.key_id,
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

    # The only response that reveals a participation key: it is the bearer
    # secret for say/command/leave and the agent WebSocket.
    result = {**key.model_dump(), "key_id": key.key_id}
    if req.receive_mentions:
        result["mention_policy"] = mention_policy()
    if req.session_id:
        result.update(session_id=req.session_id, agent=req.agent)
    if designed is not None:
        result["designed_voice"] = designed.model_dump()
    return result


@api.post("/channels/{channel_id}/leave", tags=["Channels"])
async def leave_channel(
    channel_id: str,
    req: LeaveRequest | None = None,
    key_id: str | None = Query(
        None, deprecated=True, description="Deprecated: send key_id in the JSON body."
    ),
    db: Session = Depends(get_db),
):
    """Leave a channel.

    Send the participation key in the JSON body (``{"key_id": ...}``); the
    ``?key_id=`` query parameter still works but puts the secret in access
    logs. The body wins if both are given.
    """
    key_id = req.key_id if req is not None else key_id
    if not key_id:
        raise HTTPException(422, "key_id is required")
    participant = ch.get_participant(key_id)
    success = ch.leave_channel(channel_id, key_id, db)
    if not success:
        return JSONResponse({"error": "Participant not found"}, status_code=404)
    await _broadcast_to_channel(
        channel_id, {"type": "system", "text": f"{participant.handle} left the channel"}
    )
    return {"status": "left"}


@api.post("/channels/{channel_id}/heartbeat", tags=["Channels"])
async def heartbeat(channel_id: str, req: HeartbeatRequest, db: Session = Depends(get_db)):
    """Mark a participant as still here without saying anything.

    For clients that hold no agent WebSocket (the web UI): a participant
    with no socket and no say, command, inbox poll or heartbeat for the idle
    grace period (``AGENT_PTT_IDLE_REAP_SECONDS``, default 600) is removed.
    404 means the key isn't in the channel (rejoin); 403 that it was kicked.
    """
    if db.get(RevokedParticipantDB, req.key_id):
        raise HTTPException(403, "Participant was removed; join explicitly to return")
    participant = ch.get_participant(req.key_id)
    if participant is None or participant.channel_id != channel_id:
        return JSONResponse({"error": "Participant not found in channel"}, status_code=404)
    ch.mark_seen(participant)
    return {"status": "ok"}


@api.post("/channels/{channel_id}/say", tags=["Channels"])
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
    await ch.publish_followups(msg)
    payload = _message_payload(msg, with_context=True)
    # Mentions queued in the project inbox (roles, the project, absent members).
    payload["queued_items"] = list(msg._queued_items)
    return payload


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


@api.get("/channels/{channel_id}/history", tags=["Channels"])
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


@api.get("/channels/{channel_id}/messages/{message_id}", tags=["Channels"])
async def get_message(channel_id: str, message_id: str, db: Session = Depends(get_db)):
    """One message with its full ``context`` — what an agent pulls when a
    spoken line makes it curious."""
    msg = ch.get_message(channel_id, message_id, db=db)
    if msg is None:
        return JSONResponse({"error": "Message not found"}, status_code=404)
    return _message_payload(msg, with_context=True)


@api.post("/channels/{channel_id}/command", tags=["Channels"])
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
    ch.mark_seen(participant)
    return participant.mention_id


@api.get("/channels/{channel_id}/inbox", tags=["Channels"])
async def get_inbox(
    channel_id: str,
    recipient_id: str = Depends(inbox_recipient),
    limit: int = Query(default=6, ge=1, le=20),
    wait: float = Query(default=0, ge=0, le=60),
    x_participant_key: str = Header(),
):
    """Read pending mentions without consuming them. Requires X-Participant-Key.

    With ``wait`` > 0 and nothing pending, long-polls up to ``wait`` seconds
    for a mention to arrive. Every call is a sign of life (an ``away`` hooked
    participant comes back ``active``). No DB session is held while waiting.
    """
    rows = await mentions.wait_pending(channel_id, recipient_id, limit, wait)
    participant = ch.get_participant(x_participant_key)
    if participant is not None:
        ch.mark_seen(participant)
    return rows


@api.post("/channels/{channel_id}/inbox/ack", tags=["Channels"])
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


@api.get("/voices", tags=["Voices"])
async def list_voices(engine: str = "pocket-tts"):
    """List designed voices from the active TTS engine."""
    if engine != "pocket-tts":
        raise HTTPException(400, "Only Pocket TTS is supported")
    backend = get_backend(engine)
    voices = await backend.list_voices()
    return [v.model_dump() for v in voices]


@api.post("/voices/profiles", tags=["Voices"])
async def save_profile(profile: VoiceProfile, db: Session = Depends(get_db)):
    """Create or update a stored voice profile."""
    if profile.engine != "pocket-tts":
        raise HTTPException(400, "Only Pocket TTS is supported")
    return save_voice_profile(profile, db).model_dump()


@api.get("/voices/profiles", tags=["Voices"])
async def list_profiles(engine: str | None = None, db: Session = Depends(get_db)):
    """List stored voice profiles, optionally filtered by engine."""
    return [p.model_dump() for p in list_voice_profiles(db, engine=engine)]


@api.get("/voices/profiles/{voice_id}", tags=["Voices"])
async def get_profile(voice_id: str, db: Session = Depends(get_db)):
    """Get a stored voice profile by ID."""
    profile = get_voice_profile(voice_id, db)
    if profile is None:
        return JSONResponse({"error": "Voice profile not found"}, status_code=404)
    return profile.model_dump()


@api.delete("/voices/profiles/{voice_id}", tags=["Voices"])
async def delete_profile(voice_id: str, db: Session = Depends(get_db)):
    """Delete a stored voice profile."""
    if not delete_voice_profile(voice_id, db):
        return JSONResponse({"error": "Voice profile not found"}, status_code=404)
    return {"status": "deleted"}


@api.get("/voices/pinned", tags=["Voices"])
async def get_pinned_voices(db: Session = Depends(get_db)):
    """List handles with auto-designed pinned voices."""
    return list_pinned_voices(db)


@api.post("/voices/pinned/{handle}/redesign", tags=["Voices"])
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
    if participant is not None:
        _live_agent_sockets[key] = _live_agent_sockets.get(key, 0) + 1
        ch.mark_seen(participant)

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
                        await ch.publish_followups(msg)
                        if msg._queued_items:
                            await websocket.send_json(
                                {
                                    "type": "queued",
                                    "message_id": msg.message_id,
                                    "queued_items": msg._queued_items,
                                }
                            )
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
        if participant is not None:
            # The idle grace clock starts when the last socket closes.
            remaining = _live_agent_sockets.get(key, 0) - 1
            if remaining > 0:
                _live_agent_sockets[key] = remaining
            else:
                _live_agent_sockets.pop(key, None)
            ch.mark_seen(participant)

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
        "sender_id": msg.sender_id,
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
        # The result is the public dump (no sender key); use the live message.
        msg = ch.get_message(channel_id, result["message"]["message_id"])
        if msg is not None:
            await _broadcast_to_channel(channel_id, _message_event(msg))
            await ch.publish_followups(msg)
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
app.include_router(projects_router)  # /projects and /work-items; 404 when hosted
# Server-side notices (channel.post_notice) reach agent WebSockets like any message.
ch.set_broadcaster(lambda msg: _broadcast_to_channel(msg.channel_id, _message_event(msg)))
app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")


# Hosted organization routes use their own identity and tenant boundary.
workspace.install(app)
