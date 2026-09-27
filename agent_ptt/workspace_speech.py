"""Optional tenant speech: persisted text remains authoritative and independent."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_ptt.db import SessionLocal, get_db
from agent_ptt.models import VoiceProfile
from agent_ptt.tts import POCKET_VOICES, get_backend
from agent_ptt.workspace_auth import (
    Principal,
    authenticate,
    principal,
    rate_limit,
    role_for,
    token_from,
)
from agent_ptt.workspace_models import ChatMessage

router = APIRouter(prefix="/api/workspace", tags=["Workspace speech"])
PATH = "/organizations/{org_id}/channels/{room_id}/messages/{message_id}/speech"
MAX_TEXT = 600
MAX_AUDIO_BYTES = 8 * 1024 * 1024
SYNTHESIS_TIMEOUT = 30
_lock = threading.Lock()
_active: set[str] = set()
_tasks: set[asyncio.Task] = set()


def require_server_speech():
    if os.environ.get("AGENT_PTT_SERVER_SPEECH", "1") != "1":
        raise HTTPException(410, "Server speech is disabled")


def speech_input(org_id: str, room_id: str, message_id: int, identity: Principal, db: Session):
    role_for(org_id, identity, db)
    row = db.scalar(
        select(ChatMessage).where(
            ChatMessage.org_id == org_id,
            ChatMessage.room_id == room_id,
            ChatMessage.id == message_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Message not found")
    # Only public catalog voices: never consult global profiles, clones, or paths.
    seed = hashlib.sha256(f"{org_id}:{row.sender_kind}:{row.sender_id}".encode()).digest()
    voice = POCKET_VOICES[int.from_bytes(seed[:4], "big") % len(POCKET_VOICES)]
    return {
        "message_id": row.id,
        "text": row.text[:MAX_TEXT],
        "truncated": len(row.text) > MAX_TEXT,
        "voice": {"engine": "pocket-tts", "id": voice, "scope": "public-catalog", "version": 1},
        "mime_type": "audio/wav",
    }


@router.get(PATH + "/metadata")
def metadata(
    org_id: str,
    room_id: str,
    message_id: int,
    identity: Principal = Depends(principal),
    db: Session = Depends(get_db),
):
    require_server_speech()
    return JSONResponse(
        content=speech_input(org_id, room_id, message_id, identity, db),
        headers={"Cache-Control": "no-store"},
    )


async def synthesize(org_id: str, data: dict) -> bytes:
    # Reject excess work immediately; there is no unbounded inference queue.
    with _lock:
        if org_id in _active or len(_active) >= 2:
            raise HTTPException(429, "Speech is busy", headers={"Retry-After": "2"})
        _active.add(org_id)

    async def work():
        profile = VoiceProfile(
            voice_id=data["voice"]["id"],
            display_name=data["voice"]["id"],
            engine="pocket-tts",
            settings={"voice": data["voice"]["id"]},
        )
        return await get_backend("pocket-tts").synthesize(data["text"], profile)

    task = asyncio.create_task(work())
    _tasks.add(task)

    def finished(done):
        with _lock:
            _active.discard(org_id)
        _tasks.discard(done)
        if not done.cancelled():
            done.exception()  # Retrieve late exceptions after a request times out.

    task.add_done_callback(finished)
    try:
        # Shield keeps the slot occupied while the backend's CPU thread is running,
        # even if the HTTP caller leaves or times out. Never stack abandoned jobs.
        audio = await asyncio.wait_for(asyncio.shield(task), SYNTHESIS_TIMEOUT)
    except TimeoutError as exc:
        raise HTTPException(504, "Speech timed out; text is still available") from exc
    except Exception as exc:
        raise HTTPException(503, "Speech unavailable; text is still available") from exc
    if not isinstance(audio, bytes) or not audio or len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(503, "Speech output unavailable")
    return audio


@router.get(PATH)
async def speech(
    org_id: str,
    room_id: str,
    message_id: int,
    request: Request,
    identity: Principal = Depends(principal),
    db: Session = Depends(get_db),
):
    require_server_speech()
    data = speech_input(org_id, room_id, message_id, identity, db)
    rate_limit("speech:identity:" + identity.id, 20)
    rate_limit("speech:org:" + org_id, 40)
    audio = await synthesize(org_id, data)
    # Revocation during slow synthesis must prevent delivery too.
    with SessionLocal() as fresh:
        current = authenticate(token_from(request.headers, request.cookies), fresh)
        speech_input(org_id, room_id, message_id, current, fresh)
    return Response(
        audio,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "Vary": "Authorization, Cookie",
            "X-Speech-Engine": data["voice"]["engine"],
            "X-Speech-Voice": data["voice"]["id"],
            "X-Speech-Truncated": str(data["truncated"]).lower(),
        },
    )


def install(app):
    """Register routes; no tables or message-send hooks are required."""
    app.include_router(router)
