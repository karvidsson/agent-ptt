"""Cross-machine collaboration: presence, durable mentions, away, inbox long-poll."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from agent_ptt import channel as ch
from agent_ptt import projects as pj
from agent_ptt import server
from agent_ptt.db import SessionLocal
from agent_ptt.mention_policy import mention_policy
from agent_ptt.models import ProjectMemberDB, WorkItemDB

REMOTE = "git@github.com:Owner/Collab.git"


@pytest.fixture(autouse=True)
def _reset_projects():
    pj.reset_runtime_state()
    yield
    pj.reset_runtime_state()


def pjoin(client, handle, expect=200, **extra):
    body = {"remote": REMOTE, "handle": handle, **extra}
    response = client.post("/projects/join", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def hdr(joined):
    return {"X-Member-Key": joined["member_key"]}


def pid(joined):
    return joined["project"]["project_id"]


def inbox(client, joined, **params):
    response = client.get(f"/projects/{pid(joined)}/inbox", params=params, headers=hdr(joined))
    assert response.status_code == 200, response.text
    return response.json()["items"]


def cjoin(client, cid, handle="Poppy", **extra):
    response = client.post(
        f"/channels/{cid}/join", json={"handle": handle, "voice_id": "alba", **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


def hooked(client, cid, session, agent="Claude"):
    return cjoin(client, cid, session_id=session, agent=agent, receive_mentions=True)


def say(client, cid, who, text, expect=200):
    response = client.post(f"/channels/{cid}/say", json={"key_id": who["key_id"], "text": text})
    assert response.status_code == expect, response.text
    return response.json()


def project(client, joined):
    response = client.get(f"/projects/{pid(joined)}")
    assert response.status_code == 200
    return response.json()


def roster(client, joined):
    return {m["handle"]: m for m in project(client, joined)["members"]}


def chan_inbox(client, cid, who, **params):
    return client.get(
        f"/channels/{cid}/inbox", params=params, headers={"X-Participant-Key": who["key_id"]}
    )


# --------------------------------------------------------------------------
# Section 1: presence, resume_handle, PATCH members/me
# --------------------------------------------------------------------------


def test_resume_handle_keeps_member_and_its_inbox(client):
    ember = pjoin(client, "Ember", roles=["lead"], machine="krille", harness="claude")
    finn = pjoin(client, "Finn")
    sent = client.post(
        f"/projects/{pid(finn)}/items",
        json={"kind": "instruction", "to": "agent:Ember", "body": "Look at the login bug"},
        headers=hdr(finn),
    )
    assert sent.status_code == 201

    # A new session (another machine), same pinned handle, no stored key.
    again = pjoin(client, "ember", resume_handle=True, machine="mbp", doing="triage")
    assert again["resumed"] is True
    assert again["member"]["member_id"] == ember["member"]["member_id"]
    assert again["member_key"] == ember["member_key"]
    assert again["member"]["roles"] == ["lead"]
    assert again["member"]["machine"] == "mbp" and again["member"]["doing"] == "triage"
    assert [i["title"] for i in inbox(client, again)] == ["Look at the login bug"]

    fresh = pjoin(client, "Ember")  # Without resume_handle: a new member, as before.
    assert fresh["resumed"] is False
    assert fresh["member"]["member_id"] != ember["member"]["member_id"]
    nobody = pjoin(client, "Nobody", resume_handle=True)
    assert nobody["resumed"] is False


def test_patch_me_updates_presence_and_roles(client):
    lead = pjoin(client, "Lead")
    rex = pjoin(client, "Rex")
    client.post(
        f"/projects/{pid(lead)}/items",
        json={"kind": "handoff", "to": "role:reviewer", "body": "Review PR 12"},
        headers=hdr(lead),
    )
    url = f"/projects/{pid(rex)}/members/me"
    assert client.patch(url, json={"doing": "x"}).status_code == 401
    assert client.patch(url, json={"doing": "x"}, headers=hdr(lead)).status_code == 200

    me = client.patch(
        url,
        json={"doing": "reviewing auth", "machine": "mbp", "roles": ["reviewer"]},
        headers=hdr(rex),
    )
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["doing"] == "reviewing auth" and body["doing_at"]
    assert body["machine"] == "mbp" and body["roles"] == ["reviewer"]
    assert body["presence"] == "active" and body["mention"] == "@Rex"
    assert "member_key" not in body
    assert [i["title"] for i in inbox(client, rex)] == ["Review PR 12"]

    cleared = client.patch(url, json={"doing": ""}, headers=hdr(rex)).json()
    assert cleared["doing"] is None and cleared["doing_at"] is None
    assert cleared["machine"] == "mbp"
    bad = client.patch(url, json={"roles": ["a:b"]}, headers=hdr(rex))
    assert bad.status_code == 422


def test_project_roster_has_presence_and_mention_but_no_keys(client):
    a = pjoin(client, "Ada", machine="kbot", doing="docs", harness="codex")
    pjoin(client, "Claude · agent-ptt")
    with SessionLocal() as db:
        old = db.query(ProjectMemberDB).filter_by(handle="Claude · agent-ptt").one()
        old.last_seen = datetime.now(UTC) - timedelta(hours=2)
        db.commit()
    detail = project(client, a)
    members = {m["handle"]: m for m in detail["members"]}
    assert members["Ada"]["presence"] == "active"
    assert members["Ada"]["machine"] == "kbot" and members["Ada"]["doing"] == "docs"
    assert members["Ada"]["mention"] == "@Ada"
    assert members["Claude · agent-ptt"]["presence"] == "idle"
    assert members["Claude · agent-ptt"]["mention"] == '@"Claude · agent-ptt"'
    assert a["member_key"] not in str(detail)

    with SessionLocal() as db:
        old = db.query(ProjectMemberDB).filter_by(handle="Claude · agent-ptt").one()
        old.last_seen = datetime.now(UTC) - timedelta(days=2)
        db.commit()
    assert roster(client, a)["Claude · agent-ptt"]["presence"] == "away"
    pj._active_waits[a["member"]["member_id"]] = 1
    assert roster(client, a)["Ada"]["presence"] == "listening"


def test_board_refs_carry_machine_and_doing(client):
    a = pjoin(client, "Ada", machine="kbot", doing="docs")
    client.post(
        f"/projects/{pid(a)}/items",
        json={"kind": "note", "body": "hello"},
        headers=hdr(a),
    )
    items = client.get("/work-items").json()["items"]
    assert items[0]["from"]["machine"] == "kbot" and items[0]["from"]["doing"] == "docs"


# --------------------------------------------------------------------------
# Section 2 + addendum A: durable addressing in the project channel
# --------------------------------------------------------------------------


def test_mention_of_absent_member_becomes_work_item(client):
    theo = pjoin(client, "Theo", harness="codex", machine="mbp")
    cid = theo["project"]["channel_id"]
    poppy = cjoin(client, cid, "Poppy")

    msg = say(client, cid, poppy, "@Theo please review the parser\nDetails in the PR.")
    assert [q["to_label"] for q in msg["queued_items"]] == ["agent:Theo"]
    items = inbox(client, theo)
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "instruction"
    assert item["title"] == "@Theo please review the parser"
    assert item["body"].startswith("@Theo please review")
    assert item["context"] == {"channel_id": cid, "message_id": msg["message_id"]}
    assert item["from"]["handle"] == "Poppy"

    # The sender became a project member (mode hooks) automatically.
    members = roster(client, theo)
    assert members["Poppy"]["mode"] == "hooks"

    history = client.get(f"/channels/{cid}/history").json()
    texts = [(m["kind"], m["text"]) for m in history]
    assert ("message", msg["text"]) in texts
    notice = [t for k, t in texts if k == "notice" and "agent:Theo" in t]
    assert notice and texts.index(("message", msg["text"])) < texts.index(("notice", notice[0]))

    # A second mention reuses the sender member, no duplicate.
    say(client, cid, poppy, "@theo one more thing")
    assert sum(1 for h in roster(client, theo) if h == "Poppy") == 1
    assert len(inbox(client, theo)) == 2


def test_role_and_project_mentions(client):
    lead = pjoin(client, "Lead")
    rex = pjoin(client, "Rex", roles=["reviewer"])
    cid = lead["project"]["channel_id"]
    poppy = cjoin(client, cid, "Poppy")

    msg = say(client, cid, poppy, "@role:reviewer. can you look? cc @project")
    assert [q["to"] for q in msg["queued_items"]] == ["role:reviewer", "project"]
    assert {i["to"] for i in inbox(client, rex)} == {"role:reviewer", "project"}
    assert [i["to"] for i in inbox(client, lead)] == ["project"]
    # A role nobody holds yet still queues: a later joiner with the role gets it.
    say(client, cid, poppy, "@role:designer mockups please")
    designer = pjoin(client, "Dee", roles=["designer"])
    assert [i["title"] for i in inbox(client, designer)][-1] == "@role:designer mockups please"


def test_sender_member_takes_harness_from_session(client):
    lead = pjoin(client, "Lead", roles=["dev"])
    cid = lead["project"]["channel_id"]
    codex = cjoin(client, cid, session_id="codex-1", agent="Codex")
    say(client, cid, codex, "@role:dev hello")
    member = roster(client, lead)[codex["handle"]]
    assert member["harness"] == "codex" and member["mode"] == "hooks"


def test_live_receiver_uses_channel_inbox_not_project(client):
    lead = pjoin(client, "Lead")
    cid = lead["project"]["channel_id"]
    poppy = cjoin(client, cid, "Poppy")
    agent = hooked(client, cid, "sess-live")
    pjoin(client, agent["handle"])  # Also a project member.
    token = pj.mention_text(agent["handle"])
    msg = say(client, cid, poppy, f"{token} ping")
    assert msg["queued_items"] == []
    assert [d["recipient_id"] for d in msg["deliveries"]] == [agent["mention_id"]]
    with SessionLocal() as db:
        assert db.query(WorkItemDB).count() == 0


def test_present_non_receiver_member_gets_project_item(client):
    theo = pjoin(client, "Theo")
    cid = theo["project"]["channel_id"]
    cjoin(client, cid, "Theo")  # In the channel, but without a mention receiver.
    poppy = cjoin(client, cid, "Poppy")
    msg = say(client, cid, poppy, "@Theo are you there?")
    assert [q["to_label"] for q in msg["queued_items"]] == ["agent:Theo"]
    assert len(inbox(client, theo)) == 1


def test_unknown_mention_422_lists_addressable_handles(client):
    lead = pjoin(client, "Lead")
    cid = lead["project"]["channel_id"]
    poppy = cjoin(client, cid, "Poppy")
    agent = hooked(client, cid, "sess-422")
    detail = say(client, cid, poppy, "@Ghost hi", expect=422)["detail"]
    assert "@Ghost" in detail and "@Lead" in detail
    assert pj.mention_text(agent["handle"]) in detail
    assert "@role:<role>" in detail and "@project" in detail
    assert "@Poppy" not in detail  # Not yourself.
    with SessionLocal() as db:
        assert db.query(WorkItemDB).count() == 0

    plain = client.post("/channels", json={"name": "no-project"}).json()["channel_id"]
    p2 = cjoin(client, plain, "Poppy")
    hooked(client, plain, "sess-plain")
    detail = say(client, plain, p2, "@role:dev hi", expect=422)["detail"]
    assert "only in a project's channel" in detail
    say(client, plain, p2, "@project hi", expect=422)
    say(client, plain, p2, "@Lead hi", expect=422)  # Project members don't count here.


def test_self_mention_queues_nothing(client):
    lead = pjoin(client, "Poppy")
    cid = lead["project"]["channel_id"]
    poppy = cjoin(client, cid, "Poppy")
    assert say(client, cid, poppy, "note to @Poppy")["queued_items"] == []


def test_websocket_say_queues_durable_mentions(client):
    rex = pjoin(client, "Rex", roles=["reviewer"])
    cid = rex["project"]["channel_id"]
    poppy = cjoin(client, cid, "Poppy")
    with client.websocket_connect(f"/channels/{cid}/ws?key={poppy['key_id']}") as ws:
        ws.send_json({"type": "message", "text": "@role:reviewer over to you"})
        frames = [ws.receive_json() for _ in range(3)]
        queued = next(f for f in frames if f["type"] == "queued")
        assert queued["queued_items"][0]["to"] == "role:reviewer"
        ws.send_json({"type": "message", "text": "@Nobody hi"})
        error = ws.receive_json()
        while error["type"] != "error":
            error = ws.receive_json()
        assert "Addressable here" in error["detail"]
    assert [i["title"] for i in inbox(client, rex)] == ["@role:reviewer over to you"]


# --------------------------------------------------------------------------
# Section 3: hooked participants go away instead of being reaped
# --------------------------------------------------------------------------


def age(key_id, seconds):
    ch.get_participant(key_id).last_seen = datetime.now(UTC) - timedelta(seconds=seconds)


def test_hooked_participant_goes_away_and_keeps_receiving(client):
    cid = client.post("/channels", json={"name": "room"}).json()["channel_id"]
    human = cjoin(client, cid, "Human")
    agent = hooked(client, cid, "sess-away")
    age(agent["key_id"], 30)
    age(human["key_id"], 5)

    with client.websocket_connect(f"/channels/{cid}/ws?key={human['key_id']}") as ws:
        ws.send_json({"type": "command", "name": "names"})
        assert ws.receive_json()["type"] == "result"
        reaped = client.portal.call(server._reap_idle_once, 10)
        assert reaped == []
        assert ws.receive_json() == {
            "type": "presence",
            "handle": agent["handle"],
            "state": "away",
            "reason": "idle",
        }
    entry = ch.get_participant(agent["key_id"])
    assert entry.state == "away" and entry.away_reason == "idle"
    roster = client.get(f"/channels/{cid}").json()["participants"]
    assert roster[agent["participant_id"]]["state"] == "away"

    # Still addressable; a second pass does not re-announce.
    say(client, cid, human, f"{pj.mention_text(agent['handle'])} wake up")
    assert client.portal.call(server._reap_idle_once, 10) == []
    # A sign of life (inbox poll) brings it back.
    rows = chan_inbox(client, cid, agent).json()
    assert [r["text"] for r in rows] == [f"{pj.mention_text(agent['handle'])} wake up"]
    assert ch.get_participant(agent["key_id"]).state == "active"


def test_manual_away_is_not_cleared_by_activity(client):
    cid = client.post("/channels", json={"name": "room"}).json()["channel_id"]
    agent = hooked(client, cid, "sess-manual")
    client.post(
        f"/channels/{cid}/command",
        json={"key_id": agent["key_id"], "name": "away", "args": "lunch"},
    )
    chan_inbox(client, cid, agent)
    entry = ch.get_participant(agent["key_id"])
    assert entry.state == "away" and entry.away_reason == "lunch"


def test_away_participant_is_reaped_after_away_grace(client, monkeypatch):
    cid = client.post("/channels", json={"name": "room"}).json()["channel_id"]
    agent = hooked(client, cid, "sess-old")
    monkeypatch.setenv("AGENT_PTT_AWAY_REAP_SECONDS", "100")
    age(agent["key_id"], 50)
    assert client.portal.call(server._reap_idle_once, 10) == []
    age(agent["key_id"], 200)
    assert client.portal.call(server._reap_idle_once, 10) == [agent["handle"]]
    assert ch.get_participant(agent["key_id"]) is None


def test_away_reap_zero_means_never(client, monkeypatch):
    cid = client.post("/channels", json={"name": "room"}).json()["channel_id"]
    agent = hooked(client, cid, "sess-never")
    monkeypatch.setenv("AGENT_PTT_AWAY_REAP_SECONDS", "0")
    age(agent["key_id"], 10**8)
    assert client.portal.call(server._reap_idle_once, 10) == []
    assert ch.get_participant(agent["key_id"]).state == "away"


@pytest.mark.parametrize(("raw", "expected"), [("", 604800), ("0", 0), ("60", 60), ("x", 604800)])
def test_away_reap_seconds_env(monkeypatch, raw, expected):
    monkeypatch.setenv("AGENT_PTT_AWAY_REAP_SECONDS", raw)
    assert ch.away_reap_seconds() == expected


# --------------------------------------------------------------------------
# Section 4: channel inbox long-poll
# --------------------------------------------------------------------------


def test_channel_inbox_long_poll_returns_on_new_mention(client):
    cid = client.post("/channels", json={"name": "room"}).json()["channel_id"]
    human = cjoin(client, cid, "Human")
    agent = hooked(client, cid, "sess-wait")
    assert chan_inbox(client, cid, agent, wait=0).json() == []
    assert chan_inbox(client, cid, agent, wait=61).status_code == 422

    result: dict = {}

    def waiter():
        started = time.monotonic()
        result["response"] = chan_inbox(client, cid, agent, wait=10)
        result["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.3)
    say(client, cid, human, f"{pj.mention_text(agent['handle'])} new task")
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["elapsed"] < 5
    assert [m["text"] for m in result["response"].json()] == [
        f"{pj.mention_text(agent['handle'])} new task"
    ]
    # Pending mentions return at once, without waiting.
    started = time.monotonic()
    assert len(chan_inbox(client, cid, agent, wait=10).json()) == 1
    assert time.monotonic() - started < 2


def test_channel_inbox_wait_times_out_empty(client):
    cid = client.post("/channels", json={"name": "room"}).json()["channel_id"]
    agent = hooked(client, cid, "sess-timeout")
    started = time.monotonic()
    assert chan_inbox(client, cid, agent, wait=0.5).json() == []
    assert 0.4 < time.monotonic() - started < 3


def test_mention_policy_advertises_wait_but_stays_v1():
    policy = mention_policy()
    assert policy["version"] == 1 and policy["idle_wakeup"] is False
    assert policy["wait_supported"] is True and policy["wait_max_seconds"] == 60


def test_quickstart_covers_roster_addressing_and_waking(client):
    text = client.get("/llms.txt").text
    for needle in (
        "GET http://testserver/projects/<project_id>",
        "@role:reviewer",
        "@project",
        "resume_handle",
        "members/me",
        "agent-ptt inbox --follow",
        "inbox?wait=30",
        "Monitor",
    ):
        assert needle in text, needle
    assert "~/" not in text and "/home/" not in text


def test_pinned_handle_survives_a_new_session(client):
    cid = client.post("/channels", json={"name": "pin-room"}).json()["channel_id"]
    first = cjoin(client, cid, "Ember", session_id="s-old", agent="Claude", pin_handle=True)
    assert first["handle"] == "Ember"
    # Without pinning, a session gets an assigned name.
    other = hooked(client, cid, "s-other")
    assert other["handle"] != "Ember"
    # A later session (another machine, after a restart) takes the name over.
    later = cjoin(client, cid, "Ember", session_id="s-new", agent="Claude", pin_handle=True)
    assert later["handle"] == "Ember"
    assert later["voice_id"] == first["voice_id"]
    again = cjoin(client, cid, "Ember", session_id="s-new", agent="Claude", pin_handle=True)
    assert again["key_id"] == later["key_id"]


def test_say_speak_false_posts_a_text_only_notice(client):
    cid = client.post("/channels", json={"name": "quiet-room"}).json()["channel_id"]
    me = cjoin(client, cid, "Poppy")
    response = client.post(
        f"/channels/{cid}/say", json={"key_id": me["key_id"], "text": "just text", "speak": False}
    )
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "notice"
    spoken = client.post(f"/channels/{cid}/say", json={"key_id": me["key_id"], "text": "hi"})
    assert spoken.json()["kind"] == "message"
