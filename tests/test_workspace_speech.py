"""Isolated hosted speech boundary; no real model, speakers, or shared server."""

import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from agent_ptt import workspace
from agent_ptt import workspace_auth as auth
from agent_ptt import workspace_speech as speech
from agent_ptt.db import SessionLocal, init_db
from agent_ptt.workspace_models import Agent, ChatMessage, Member, Organization, Room, User


@pytest.fixture
def setup_speech(monkeypatch):
    monkeypatch.setenv("AGENT_PTT_SERVER_SPEECH", "1")
    init_db()
    auth._attempts.clear()
    with SessionLocal() as db:
        for suffix in ("a", "b"):
            db.add(User(id=suffix, email=suffix, name=suffix, password_hash="unused"))
            db.flush()
            db.add(Organization(id=suffix, name=suffix, owner_id=suffix))
            db.flush()
            db.add_all(
                [
                    Member(org_id=suffix, user_id=suffix),
                    Room(id=suffix, org_id=suffix, name="general"),
                ]
            )
            db.flush()
        db.add(Agent(id="agent", org_id="a", name="Agent"))
        db.flush()
        human_token = auth.issue_credential(db, user_id="a")
        agent_token = auth.issue_credential(db, agent_id="agent")
        db.add(
            ChatMessage(
                id=1,
                org_id="a",
                room_id="a",
                sender_id="agent",
                sender_kind="agent",
                sender_name="Agent",
                client_id="1",
                text="hello " * 200,
            )
        )
        db.commit()
    app = FastAPI()
    app.include_router(workspace.router)
    speech.install(app)
    calls = []

    class Backend:
        async def synthesize(self, text, profile):
            calls.append((text, profile))
            return b"fake-wav"

    monkeypatch.setattr(speech, "get_backend", lambda engine: Backend())
    with TestClient(app, headers={"Authorization": f"Bearer {human_token}"}) as client:
        yield client, calls, agent_token
    auth._attempts.clear()


URL = "/api/workspace/organizations/a/channels/a/messages/1/speech"


def test_authorized_audio_metadata_and_bounded_public_voice(setup_speech):
    client, calls, token = setup_speech
    meta = client.get(URL + "/metadata")
    assert meta.status_code == 200
    assert meta.json()["truncated"]
    assert meta.json()["voice"]["scope"] == "public-catalog"
    response = client.get(URL, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["x-speech-voice"] == meta.json()["voice"]["id"]
    assert len(calls[0][0]) == speech.MAX_TEXT
    assert calls[0][1].settings == {"voice": meta.json()["voice"]["id"]}


def test_no_anonymous_cross_tenant_or_cross_room_audio(setup_speech):
    client, calls, token = setup_speech
    for ending in ("", "/metadata"):
        assert (
            client.get(URL + ending, headers={"Authorization": "Bearer invalid"}).status_code == 401
        )
        assert (
            client.get(URL.replace("organizations/a", "organizations/b") + ending).status_code
            == 404
        )
        assert client.get(URL.replace("channels/a", "channels/b") + ending).status_code == 404
        assert client.get(URL.replace("messages/1", "messages/999") + ending).status_code == 404
    assert not calls
    with SessionLocal() as db:
        db.get(Agent, "agent").active = False
        db.commit()
    assert client.get(URL, headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_failed_speech_does_not_prevent_chat(setup_speech, monkeypatch):
    client, _, _ = setup_speech

    class Broken:
        async def synthesize(self, *args):
            raise RuntimeError("private backend error")

    monkeypatch.setattr(speech, "get_backend", lambda engine: Broken())
    assert client.get(URL).status_code == 503
    response = client.post(
        URL.rsplit("/1/speech", 1)[0], json={"text": "Still chatting", "client_id": "next"}
    )
    assert response.status_code == 201
    assert response.json()["text"] == "Still chatting"
    assert "private backend error" not in client.get(URL).text


def test_revoked_during_synthesis_blocks_audio(setup_speech, monkeypatch):
    client, _, token = setup_speech

    class Revoking:
        async def synthesize(self, *args):
            with SessionLocal() as db:
                db.get(Agent, "agent").active = False
                db.commit()
            return b"fake-wav"

    monkeypatch.setattr(speech, "get_backend", lambda engine: Revoking())
    assert client.get(URL, headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.asyncio
async def test_timeout_retains_capacity_until_backend_finishes(monkeypatch):
    release = asyncio.Event()

    class Slow:
        async def synthesize(self, *args):
            await release.wait()
            return b"wav"

    monkeypatch.setattr(speech, "get_backend", lambda engine: Slow())
    monkeypatch.setattr(speech, "SYNTHESIS_TIMEOUT", 0.01)
    data = {"text": "hello", "voice": {"id": "alba"}}
    for org in ("a", "b"):
        with pytest.raises(HTTPException) as timeout:
            await speech.synthesize(org, data)
        assert timeout.value.status_code == 504
    for org in ("a", "c"):
        with pytest.raises(HTTPException) as busy:
            await speech.synthesize(org, data)
        assert busy.value.status_code == 429
    release.set()
    await asyncio.gather(*speech._tasks)
    await asyncio.sleep(0)
    assert not speech._active
    assert await speech.synthesize("a", data) == b"wav"


@pytest.mark.asyncio
async def test_cancellation_keeps_inference_slot(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    class Slow:
        async def synthesize(self, *args):
            started.set()
            await release.wait()
            return b"wav"

    monkeypatch.setattr(speech, "get_backend", lambda engine: Slow())
    data = {"text": "hello", "voice": {"id": "alba"}}
    caller = asyncio.create_task(speech.synthesize("a", data))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert "a" in speech._active
    release.set()
    await asyncio.gather(*speech._tasks)
    await asyncio.sleep(0)
    assert not speech._active


def test_audio_output_limit_and_request_rate(setup_speech, monkeypatch):
    client, _, _ = setup_speech
    monkeypatch.setattr(speech, "MAX_AUDIO_BYTES", 1)
    assert client.get(URL).status_code == 503
    for _ in range(19):
        client.get(URL)
    response = client.get(URL)
    assert response.status_code == 429
    assert response.headers["Retry-After"]


def test_disabled_speech_never_runs_server_synthesis(setup_speech, monkeypatch):
    client, calls, _ = setup_speech
    monkeypatch.setenv("AGENT_PTT_SERVER_SPEECH", "0")
    assert client.get(URL).status_code == 410
    assert client.get(URL + "/metadata").status_code == 410
    assert calls == []


def test_server_speech_enabled_by_default(setup_speech, monkeypatch):
    client, calls, _ = setup_speech
    monkeypatch.delenv("AGENT_PTT_SERVER_SPEECH", raising=False)
    assert client.get(URL).status_code == 200
    assert len(calls) == 1
