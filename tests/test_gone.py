"""The gone window: the server clears out seats and project members that are gone."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from agent_ptt import channel as ch
from agent_ptt import handles, server
from agent_ptt import projects as pj
from agent_ptt.db import SessionLocal
from agent_ptt.models import HandleClaimDB, ParticipantKeyDB, ProjectMemberDB

REMOTE = "git@github.com:Owner/Gone.git"
HOUR = 3600


@pytest.fixture(autouse=True)
def _reset_projects():
    pj.reset_runtime_state()
    yield
    pj.reset_runtime_state()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def room(client, name="room"):
    return client.post("/channels", json={"name": name}).json()["channel_id"]


def hooked(client, cid, session, handle="Poppy"):
    response = client.post(
        f"/channels/{cid}/join",
        json={
            "handle": handle,
            "voice_id": "alba",
            "session_id": session,
            "agent": "Claude",
            "receive_mentions": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def age_seat(key_id, seconds):
    ch.get_participant(key_id).last_seen = datetime.now(UTC) - timedelta(seconds=seconds)


def reap(client, grace=600):
    return client.portal.call(server._reap_idle_once, grace)


def pjoin(client, handle, expect=200, **extra):
    body = {"remote": REMOTE, "handle": handle, "machine": "krille", "harness": "claude", **extra}
    response = client.post("/projects/join", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def hdr(joined):
    return {"X-Member-Key": joined["member_key"]}


def pid(joined):
    return joined["project"]["project_id"]


def mid(joined):
    return joined["member"]["member_id"]


def age_member(joined, hours, claim_hours=None):
    """Pretend the member (and, by default, its name claim) was last seen ``hours`` ago."""
    handles.flush_now()
    when = datetime.now(UTC) - timedelta(hours=hours)
    with SessionLocal() as db:
        member = db.get(ProjectMemberDB, mid(joined))
        member.last_seen = when
        claim = handles.get_claim(db, member.handle)
        if claim is not None:
            claim.claimed_at = when - timedelta(minutes=1)  # Held since before it went quiet.
            claim.last_seen = datetime.now(UTC) - timedelta(
                hours=hours if claim_hours is None else claim_hours
            )
        db.commit()


def reap_members():
    with SessionLocal() as db:
        return pj.reap_gone_members(db)


def roster(client, joined):
    response = client.get(f"/projects/{pid(joined)}")
    assert response.status_code == 200
    return {m["handle"] for m in response.json()["members"]}


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"), [("", 86400), ("0", 0), ("1.5", 5400), ("x", 86400), ("-2", 86400)]
)
def test_gone_seconds_env(monkeypatch, raw, expected):
    monkeypatch.setenv("AGENT_PTT_GONE_HOURS", raw)
    assert ch.gone_seconds() == expected


def test_away_reap_follows_gone_window_unless_set(monkeypatch):
    monkeypatch.delenv("AGENT_PTT_AWAY_REAP_SECONDS", raising=False)
    monkeypatch.setenv("AGENT_PTT_GONE_HOURS", "2")
    assert ch.away_reap_seconds() == 2 * HOUR
    monkeypatch.setenv("AGENT_PTT_AWAY_REAP_SECONDS", "604800")
    assert ch.away_reap_seconds() == 604800


def test_reaper_settings_and_startup_line(client, monkeypatch):
    body = client.get("/reaper").json()
    assert body["gone_seconds"] == 86400 and body["away_seconds"] == 86400
    assert body["idle_seconds"] == 600 and body["name_ttl_seconds"] == 86400
    assert "marked gone" in body["description"]
    monkeypatch.setenv("AGENT_PTT_GONE_HOURS", "0")
    assert "never marked gone" in ch.gone_message(600)
    assert "stay away forever" in ch.gone_message(600)


# --------------------------------------------------------------------------
# Seats
# --------------------------------------------------------------------------


def test_hooked_seat_unseen_25h_is_reaped_23h_is_kept(client):
    cid = room(client)
    old = hooked(client, cid, "sess-old", "Old")
    recent = hooked(client, cid, "sess-recent", "Recent")
    age_seat(old["key_id"], 25 * HOUR)
    age_seat(recent["key_id"], 23 * HOUR)
    assert reap(client) == [old["handle"]]
    assert ch.get_participant(old["key_id"]) is None
    assert ch.get_participant(recent["key_id"]).state == "away"


def test_seat_in_live_channel_wait_is_kept(client):
    cid = room(client)
    agent = hooked(client, cid, "sess-wait", "Waiter")
    result: dict = {}

    def waiter():
        result["response"] = client.get(
            f"/channels/{cid}/inbox",
            params={"wait": 2},
            headers={"X-Participant-Key": agent["key_id"]},
        )

    thread = threading.Thread(target=waiter)
    thread.start()
    deadline = time.monotonic() + 3
    while agent["key_id"] not in ch.waiting_keys() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert agent["key_id"] in ch.waiting_keys()
    age_seat(agent["key_id"], 25 * HOUR)
    assert reap(client) == []
    assert ch.get_participant(agent["key_id"]).state != "away"  # Not even marked away.
    thread.join(timeout=5)
    assert result["response"].status_code == 200
    assert agent["key_id"] not in ch.waiting_keys()


def test_seat_whose_member_waits_on_project_inbox_is_kept(client):
    member = pjoin(client, "Pia", session_id="sess-pia", agent="Claude")
    cid = member["project"]["channel_id"]
    seat = hooked(client, cid, "sess-pia", "Pia")
    assert seat["handle"] == member["member"]["handle"]
    pj._active_waits[mid(member)] = 1  # An open GET /projects/{p}/inbox?wait=
    age_seat(seat["key_id"], 25 * HOUR)
    assert reap(client) == []
    pj._active_waits.clear()
    assert reap(client) == [seat["handle"]]


def test_seat_signs_of_life_are_persisted_debounced(client):
    cid = room(client)
    agent = hooked(client, cid, "sess-persist", "Pat")
    with SessionLocal() as db:
        stored = db.get(ParticipantKeyDB, agent["key_id"]).last_seen
    assert stored is not None  # Join writes it.
    client.post(f"/channels/{cid}/heartbeat", json={"key_id": agent["key_id"]})
    assert agent["key_id"] in ch._seen_keys  # Memory only until the flush.
    with SessionLocal() as db:
        assert ch.flush_seen(db) == 1
        assert ch.flush_seen(db) == 0
        newer = db.get(ParticipantKeyDB, agent["key_id"]).last_seen
    assert newer > stored


def restart():
    ch._channels.clear()
    ch._message_queues.clear()
    with SessionLocal() as db:
        ch.restore_channels(db)


def test_restart_keeps_seat_age_and_legacy_rows_count_from_now(client):
    cid = room(client)
    dead = hooked(client, cid, "sess-dead", "Dead")
    legacy = hooked(client, cid, "sess-legacy", "Legacy")
    fresh = hooked(client, cid, "sess-fresh", "Fresh")
    ch.reset_runtime_state()  # Drop the join touches; set the stored ages by hand.
    with SessionLocal() as db:
        db.get(ParticipantKeyDB, dead["key_id"]).last_seen = datetime.now(UTC) - timedelta(hours=25)
        db.get(ParticipantKeyDB, legacy["key_id"]).last_seen = None
        db.get(ParticipantKeyDB, fresh["key_id"]).last_seen = datetime.now(UTC) - timedelta(hours=1)
        db.commit()
    before = datetime.now(UTC)
    restart()
    assert ch.get_participant(dead["key_id"]).last_seen < before - timedelta(hours=24)
    assert ch.get_participant(legacy["key_id"]).last_seen >= before
    assert ch.get_participant(fresh["key_id"]).last_seen < before - timedelta(minutes=59)
    assert reap(client) == [dead["handle"]]
    assert ch.get_participant(legacy["key_id"]) is not None
    assert ch.get_participant(fresh["key_id"]) is not None


def test_gone_zero_keeps_hooked_seats(client, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_GONE_HOURS", "0")
    cid = room(client)
    agent = hooked(client, cid, "sess-keep", "Keep")
    age_seat(agent["key_id"], 10**7)
    assert reap(client) == []
    assert ch.get_participant(agent["key_id"]).state == "away"


# --------------------------------------------------------------------------
# Project members
# --------------------------------------------------------------------------


def test_member_gone_after_25h(client):
    ada = pjoin(client, "Ada")
    bo = pjoin(client, "Bo")
    sent = client.post(
        f"/projects/{pid(ada)}/items",
        json={"kind": "instruction", "to": "agent:Bo", "body": "Fix the login"},
        headers=hdr(ada),
    )
    assert sent.status_code == 201
    age_member(ada, 25)
    age_member(bo, 23)

    result = reap_members()
    assert [m.handle for m in result.members] == ["Ada"]
    assert result.claims_released == ["Ada"]

    assert roster(client, bo) == {"Bo"}
    listed = client.get("/projects").json()["projects"]
    assert [p["member_count"] for p in listed] == [1]
    # The key stops working everywhere.
    assert client.get(f"/projects/{pid(ada)}/inbox", headers=hdr(ada)).status_code == 401
    patched = client.patch(f"/projects/{pid(ada)}/members/me", json={}, headers=hdr(ada))
    assert patched.status_code == 401
    # The name is free again.
    with SessionLocal() as db:
        assert handles.get_claim(db, "Ada") is None
        assert db.get(ProjectMemberDB, mid(ada)).gone_at is not None
    # History keeps the handle.
    items = client.get("/work-items").json()["items"]
    assert [i["from"]["handle"] for i in items] == ["Ada"]
    # A second pass finds nothing new.
    assert reap_members().members == []


def test_member_with_live_claim_of_the_same_agent_is_kept(client):
    ada = pjoin(client, "Ada")
    age_member(ada, 25, claim_hours=1)  # Alive in a channel; quiet in the project.
    assert reap_members().members == []
    assert roster(client, ada) == {"Ada"}


def test_claim_of_another_agent_does_not_keep_a_member(client):
    ada = pjoin(client, "Ada")
    age_member(ada, 25, claim_hours=1)
    with SessionLocal() as db:
        claim = db.get(HandleClaimDB, "ada")
        claim.machine = "elsewhere"
        db.commit()
    assert [m.handle for m in reap_members().members] == ["Ada"]
    with SessionLocal() as db:
        assert handles.get_claim(db, "Ada") is not None  # Not ours: left alone.


def test_name_reused_by_a_later_session_does_not_keep_a_member(client):
    ada = pjoin(client, "Ada")
    age_member(ada, 25, claim_hours=1)
    with SessionLocal() as db:  # Taken by its current owner after Ada went quiet.
        db.get(HandleClaimDB, "ada").claimed_at = datetime.now(UTC) - timedelta(hours=2)
        db.commit()
    assert [m.handle for m in reap_members().members] == ["Ada"]


def test_older_duplicate_is_gone_while_the_newer_one_holds_the_name(client):
    first = pjoin(client, "Ada")
    second = pjoin(client, "Ada")  # Same agent, a new member (no resume_handle).
    assert mid(first) != mid(second)
    age_member(first, 30, claim_hours=0)
    result = reap_members()
    assert [m.member_id for m in result.members] == [mid(first)]
    assert result.claims_released == []
    assert roster(client, second) == {"Ada"}


def test_member_in_a_live_wait_is_kept(client):
    ada = pjoin(client, "Ada")
    result: dict = {}

    def waiter():
        result["response"] = client.get(
            f"/projects/{pid(ada)}/inbox", params={"wait": 2}, headers=hdr(ada)
        )

    thread = threading.Thread(target=waiter)
    thread.start()
    deadline = time.monotonic() + 3
    while not pj.waiting_member_ids() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pj.waiting_member_ids() == {mid(ada)}
    age_member(ada, 25)
    assert reap_members().members == []
    thread.join(timeout=5)
    assert result["response"].status_code == 200


def test_returning_plugin_rejoins_as_a_new_member(client):
    ada = pjoin(client, "Ada", roles=["lead"])
    age_member(ada, 25)
    reap_members()
    # The plugin's stored key: 401, so it retries without it (project_link._join_project).
    stale = pjoin(client, "Ada", expect=401, member_key=ada["member_key"])
    assert stale["detail"] == "Unknown member_key"
    # A pinned handle's resume_handle doesn't resurrect the gone member either.
    again = pjoin(client, "Ada", resume_handle=True, roles=["lead"])
    assert again["resumed"] is False
    assert mid(again) != mid(ada) and again["member_key"] != ada["member_key"]
    assert roster(client, again) == {"Ada"}
    assert client.get(f"/projects/{pid(again)}/inbox", headers=hdr(again)).status_code == 200


def test_gone_member_keeps_its_usage_rows(client):
    ada = pjoin(client, "Ada")
    report = {
        "harness": "claude",
        "session_id": "s1",
        "by_model": {"claude-opus-5-5": {"input": 100, "output": 10}},
    }
    patched = client.patch(
        f"/projects/{pid(ada)}/members/me", json={"usage": report}, headers=hdr(ada)
    )
    assert patched.status_code == 200, patched.text
    pjoin(client, "Bo")
    age_member(ada, 25)
    reap_members()
    usage = client.get(f"/projects/{pid(ada)}/usage").json()
    rows = {m["handle"]: m for m in usage["members"]}
    assert rows["Ada"]["gone"] is True and rows["Ada"]["usage"] is not None
    assert rows["Bo"]["gone"] is False


def test_gone_zero_keeps_members(client, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_GONE_HOURS", "0")
    ada = pjoin(client, "Ada")
    age_member(ada, 10**4)
    assert reap_members().members == []
    assert client.post("/reap").json()["members"] == []


# --------------------------------------------------------------------------
# POST /reap
# --------------------------------------------------------------------------


def test_post_reap_runs_one_pass(client):
    ada = pjoin(client, "Ada")
    cid = room(client, "other")
    seat = hooked(client, cid, "sess-reap", "Seat")
    age_seat(seat["key_id"], 25 * HOUR)
    age_member(ada, 25)
    body = client.post("/reap").json()
    assert [s["handle"] for s in body["seats"]] == [seat["handle"]]
    assert body["seats"][0]["channel_id"] == cid
    assert "key_id" not in body["seats"][0]
    assert [m["handle"] for m in body["members"]] == ["Ada"]
    assert body["members"][0]["project_id"] == pid(ada)
    assert "member_key" not in body["members"][0]
    assert body["claims_released"] == ["Ada"]
    assert body["windows"]["gone_seconds"] == 86400
    assert client.post("/reap").json()["members"] == []


def test_post_reap_needs_the_api_key(client, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_API_KEY", "secret")
    assert client.post("/reap").status_code == 401
    assert client.post("/reap", headers={"X-API-Key": "secret"}).status_code == 200
