"""Projects and the project inbox (agent_ptt/projects.py, projects_api.py)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from agent_ptt import projects as pj
from agent_ptt.db import SessionLocal
from agent_ptt.models import ProjectMemberDB, WorkItemDB

REMOTE = "git@github.com:Owner/Repo.git"


@pytest.fixture(autouse=True)
def _reset_projects():
    pj.reset_runtime_state()
    yield
    pj.reset_runtime_state()


def join(client, handle, roles=None, remote=REMOTE, **extra):
    body = {"remote": remote, "handle": handle, **extra}
    if roles is not None:
        body["roles"] = roles
    response = client.post("/projects/join", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def hdr(joined):
    return {"X-Member-Key": joined["member_key"]}


def pid(joined):
    return joined["project"]["project_id"]


def send(client, joined, expect=201, **body):
    response = client.post(f"/projects/{pid(joined)}/items", json=body, headers=hdr(joined))
    assert response.status_code == expect, response.text
    return response.json()


def inbox(client, joined, **params):
    response = client.get(f"/projects/{pid(joined)}/inbox", params=params, headers=hdr(joined))
    assert response.status_code == 200, response.text
    return response.json()


def act(client, joined, item_id, action, expect=200, json=None):
    response = client.post(
        f"/projects/{pid(joined)}/items/{item_id}/{action}", headers=hdr(joined), json=json
    )
    assert response.status_code == expect, response.text
    return response.json()


def ids(payload):
    return [item["item_id"] for item in payload["items"]]


# --------------------------------------------------------------------------
# Remotes, join, rejoin
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "git@github.com:Owner/Repo.git",
        "https://github.com/owner/repo",
        "https://user@github.com:443/Owner/Repo.git/",
        "ssh://git@github.com:22/owner/repo.git",
        "github.com/owner/repo",
    ],
)
def test_normalize_remote(raw):
    assert pj.normalize_remote(raw) == "github.com/owner/repo"
    assert pj.default_project_name(pj.normalize_remote(raw)) == "repo"


def test_join_auto_creates_project_and_channel_and_rejoins_by_key(client):
    first = join(client, "Finn", roles=["dev"], harness="claude")
    assert first["project"]["name"] == "repo"
    assert first["project"]["remote"] == "github.com/owner/repo"
    assert first["member"]["roles"] == ["dev"]
    assert first["member"]["mode"] == "monitor"
    channel_id = first["project"]["channel_id"]
    assert client.get(f"/channels/{channel_id}").json()["name"] == "repo"

    again = join(
        client,
        "Finn 2",
        roles=["dev", "reviewer"],
        remote="https://github.com/owner/repo",
        member_key=first["member_key"],
    )
    assert again["member"]["member_id"] == first["member"]["member_id"]
    assert again["member"]["handle"] == "Finn 2"
    assert again["member"]["roles"] == ["dev", "reviewer"]
    assert again["project"]["project_id"] == pid(first)

    other = join(client, "Rex", remote="https://github.com/owner/repo")
    assert pid(other) == pid(first)
    assert other["member"]["member_id"] != first["member"]["member_id"]
    projects = client.get("/projects").json()["projects"]
    assert len(projects) == 1 and projects[0]["member_count"] == 2


def test_rejoin_keeps_roles_mode_and_harness_when_omitted(client):
    first = join(client, "Finn", roles=["dev"], mode="hooks", harness="codex")
    again = join(client, "Finn", member_key=first["member_key"])
    assert again["member"] == {**first["member"], "handle": "Finn"}
    assert again["member"]["mode"] == "hooks"
    changed = join(client, "Finn", roles=[], mode="monitor", member_key=first["member_key"])
    assert changed["member"]["roles"] == []
    assert changed["member"]["mode"] == "monitor"
    assert changed["member"]["harness"] == "codex"


def test_join_reuses_existing_channel_named_after_project(client):
    existing = client.post("/channels", json={"name": "repo"}).json()
    joined = join(client, "Finn")
    assert joined["project"]["channel_id"] == existing["channel_id"]


def test_join_validation(client):
    assert client.post("/projects/join", json={"handle": "x"}).status_code == 422
    unknown = client.post(
        "/projects/join", json={"remote": REMOTE, "handle": "x", "member_key": "nope"}
    )
    assert unknown.status_code == 401
    elsewhere = join(client, "Other", remote="https://example.com/a/b")
    wrong = client.post(
        "/projects/join",
        json={"remote": REMOTE, "handle": "x", "member_key": elsewhere["member_key"]},
    )
    assert wrong.status_code == 403
    by_name = client.post("/projects/join", json={"name": "scratch", "handle": "Ada"}).json()
    assert by_name["project"]["remote"] is None
    assert by_name["project"]["name"] == "scratch"


def test_member_routes_need_member_key(client):
    finn = join(client, "Finn")
    other = join(client, "Other", remote="https://example.com/a/b")
    url = f"/projects/{pid(finn)}/inbox"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"X-Member-Key": "bogus"}).status_code == 401
    assert client.get(url, headers=hdr(other)).status_code == 403
    assert client.get("/projects/nope/inbox", headers=hdr(finn)).status_code == 404


# --------------------------------------------------------------------------
# Deliveries and the inbox
# --------------------------------------------------------------------------


def test_late_joiner_gets_open_project_and_role_items(client):
    finn = join(client, "Finn")
    to_project = send(client, finn, kind="note", to="project", body="Heads up")
    to_role = send(client, finn, kind="handoff", to="role:reviewer", body="Review this")
    to_other = send(client, finn, kind="instruction", to="role:tester", body="Test it")

    rex = join(client, "Rex", roles=["reviewer"])
    got = ids(inbox(client, rex))
    assert got == [to_project["item_id"], to_role["item_id"]]
    assert to_other["item_id"] not in got

    # Rejoining with a new role brings that role's open items too.
    join(client, "Rex", roles=["reviewer", "tester"], member_key=rex["member_key"])
    assert to_other["item_id"] in ids(inbox(client, rex))
    # The sender never gets its own items.
    assert inbox(client, finn)["items"] == []


def test_unacked_items_are_redelivered_until_acked(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    item = send(client, finn, kind="instruction", to="role:dev", body="Fix the login\nDetails…")
    assert item["title"] == "Fix the login"
    assert item["from"]["handle"] == "Finn"

    first = inbox(client, rex)
    assert ids(first) == [item["item_id"]]
    assert first["items"][0]["delivery"]["state"] == "delivered"
    assert first["items"][0]["state"] == "delivered"
    # A "reconnect" (fresh read from the start) gets it again: at-least-once.
    assert ids(inbox(client, rex)) == [item["item_id"]]
    # Even a reader that kept its cursor still sees the unacked item.
    assert ids(inbox(client, rex, after=first["cursor"])) == [item["item_id"]]

    acked = act(client, rex, item["item_id"], "ack")
    assert acked["state"] == "acked"
    assert inbox(client, rex)["items"] == []


def test_cursor_and_long_poll(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    first = send(client, finn, kind="instruction", to="role:dev", body="One")
    cursor = inbox(client, rex)["cursor"]
    assert cursor > 0

    started = time.monotonic()
    empty = inbox(client, rex, wait=1, after=cursor)
    assert time.monotonic() - started >= 0.9
    assert ids(empty) == [first["item_id"]]  # still unacked, but nothing new
    assert empty["cursor"] == cursor

    result = {}

    def poll():
        result["payload"] = inbox(client, rex, wait=10, after=cursor)
        result["at"] = time.monotonic()

    thread = threading.Thread(target=poll)
    thread.start()
    time.sleep(0.4)
    sent_at = time.monotonic()
    second = send(client, finn, kind="instruction", to="role:dev", body="Two")
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result["at"] - sent_at < 3
    assert second["item_id"] in ids(result["payload"])
    assert result["payload"]["cursor"] > cursor


def test_long_poll_times_out_empty(client):
    rex = join(client, "Rex")
    started = time.monotonic()
    payload = inbox(client, rex, wait=1)
    assert payload == {"items": [], "cursor": 0}
    assert time.monotonic() - started >= 0.9


# --------------------------------------------------------------------------
# Claims and leases
# --------------------------------------------------------------------------


def test_claim_race_one_wins(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    ada = join(client, "Ada", roles=["dev"])
    item = send(client, finn, kind="handoff", to="role:dev", body="Take this")
    inbox(client, rex)
    inbox(client, ada)

    statuses = []

    def claim(member):
        response = client.post(
            f"/projects/{pid(finn)}/items/{item['item_id']}/claim", headers=hdr(member)
        )
        statuses.append((member["member"]["handle"], response.status_code, response.json()))

    threads = [threading.Thread(target=claim, args=(m,)) for m in (rex, ada)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    codes = sorted(code for _, code, _ in statuses)
    assert codes == [200, 409]
    winner = next(handle for handle, code, _ in statuses if code == 200)
    loser_body = next(body for _, code, body in statuses if code == 409)
    assert loser_body["assignee"]["handle"] == winner
    assert "member_key" not in str(loser_body)

    # Claiming again as the holder is idempotent.
    holder = rex if winner == "Rex" else ada
    again = act(client, holder, item["item_id"], "claim")
    assert again["state"] == "claimed"
    assert again["assignee"]["handle"] == winner
    assert again["lease_expires_at"] is not None
    # The other member no longer sees the claimed item.
    other = ada if holder is rex else rex
    assert item["item_id"] not in ids(inbox(client, other))


def test_claim_is_atomic_against_a_stale_read(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    ada = join(client, "Ada", roles=["dev"])
    item = send(client, finn, kind="instruction", to="role:dev", body="Race")
    with SessionLocal() as slow, SessionLocal() as fast:
        slow_member = slow.get(ProjectMemberDB, ada["member"]["member_id"])
        assert slow.get(WorkItemDB, item["item_id"]).state == "pending"  # stale snapshot
        pj.claim_item(fast, fast.get(ProjectMemberDB, rex["member"]["member_id"]), item["item_id"])
        with pytest.raises(pj.ProjectError) as lost:
            pj.claim_item(slow, slow_member, item["item_id"])
    assert lost.value.status == 409
    assert lost.value.extra["assignee"]["member_id"] == rex["member"]["member_id"]


def test_unclaimable_kinds(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex")
    note = send(client, finn, kind="note", to="project", body="FYI")
    act(client, rex, note["item_id"], "claim", expect=422)


def _expire(item_id):
    with SessionLocal() as db:
        db.get(WorkItemDB, item_id).lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        db.commit()


def test_lease_expiry_returns_to_pending_and_redelivers(client, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_CLAIM_LEASE_SECONDS", "1")
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    ada = join(client, "Ada", roles=["dev"])
    item = send(client, finn, kind="handoff", to="role:dev", body="Lease me")
    act(client, ada, item["item_id"], "ack")
    claimed = act(client, rex, item["item_id"], "claim")
    lease = datetime.fromisoformat(claimed["lease_expires_at"])
    assert lease - datetime.now(UTC) < timedelta(seconds=2)
    assert item["item_id"] not in ids(inbox(client, ada))

    _expire(item["item_id"])
    # Lazy expiry on read: Ada (who had acked) gets it again.
    payload = inbox(client, ada)
    assert ids(payload) == [item["item_id"]]
    assert payload["items"][0]["assignee"] is None
    detail = client.get(f"/projects/{pid(finn)}/items/{item['item_id']}").json()
    assert detail["item"]["state"] in ("pending", "delivered")
    assert "lease_expired" in [e["action"] for e in detail["events"]]
    expired_event = next(e for e in detail["events"] if e["action"] == "lease_expired")
    assert expired_event["by"] is None
    history = client.get(f"/channels/{finn['project']['channel_id']}/history").json()
    assert any("went back to open (lease expired)" in m["text"] for m in history)


def test_background_expiry_function(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    item = send(client, finn, kind="instruction", to="role:dev", body="Background")
    act(client, rex, item["item_id"], "claim")
    _expire(item["item_id"])
    with SessionLocal() as db:
        reopened = pj.expire_leases(db)
        assert list(reopened) == [pid(finn)]
        assert db.get(WorkItemDB, item["item_id"]).state == "pending"


def test_inbox_read_renews_the_assignees_lease(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    item = send(client, finn, kind="instruction", to="role:dev", body="Keep it")
    act(client, rex, item["item_id"], "claim")
    with SessionLocal() as db:
        db.get(WorkItemDB, item["item_id"]).lease_expires_at = datetime.now(UTC) + timedelta(
            seconds=30
        )
        db.commit()
    inbox(client, rex)
    detail = client.get(f"/projects/{pid(finn)}/items/{item['item_id']}").json()["item"]
    assert datetime.fromisoformat(detail["lease_expires_at"]) > datetime.now(UTC) + timedelta(
        minutes=20
    )


def test_release(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    ada = join(client, "Ada", roles=["dev"])
    item = send(client, finn, kind="handoff", to="role:dev", body="Release me")
    act(client, rex, item["item_id"], "claim")
    act(client, ada, item["item_id"], "release", expect=403)
    released = act(client, rex, item["item_id"], "release", json={"note": "out of time"})
    assert released["state"] == "pending"
    assert released["assignee"] is None
    assert released["last_event"]["action"] == "released"
    assert item["item_id"] in ids(inbox(client, ada))
    assert item["item_id"] in ids(inbox(client, rex))


# --------------------------------------------------------------------------
# Questions, answers, states
# --------------------------------------------------------------------------


def test_question_sets_waiting_and_answer_resumes(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    handoff = send(client, finn, kind="handoff", to="role:dev", body="Build it")
    act(client, rex, handoff["item_id"], "claim")

    question = send(client, rex, kind="question", reply_to=handoff["item_id"], body="Which DB?")
    assert question["to"] == f"agent:{finn['member']['member_id']}"
    detail = client.get(f"/projects/{pid(finn)}/items/{handoff['item_id']}").json()
    assert detail["item"]["state"] == "waiting"
    assert detail["item"]["open_questions"] == 1
    assert question["item_id"] in ids(inbox(client, finn))
    # A question with reply_to is not claimable work.
    act(client, finn, question["item_id"], "claim", expect=422)

    answer = send(client, finn, kind="answer", reply_to=question["item_id"], body="SQLite")
    assert answer["to"] == f"agent:{rex['member']['member_id']}"
    assert answer["item_id"] in ids(inbox(client, rex))
    detail = client.get(f"/projects/{pid(finn)}/items/{handoff['item_id']}").json()
    assert detail["item"]["state"] == "claimed"
    assert detail["item"]["open_questions"] == 0
    assert [i["item_id"] for i in detail["thread"]] == [
        handoff["item_id"],
        question["item_id"],
        answer["item_id"],
    ]
    actions = [e["action"] for e in detail["events"]]
    assert "waiting" in actions and "resumed" in actions
    q = client.get(f"/projects/{pid(finn)}/items/{question['item_id']}").json()
    assert q["item"]["state"] == "done"
    assert q["events"][-1]["action"] == "answered"
    assert q["thread"][0]["item_id"] == handoff["item_id"]


def test_answer_requires_a_question(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    handoff = send(client, finn, kind="handoff", to="role:dev", body="Build it")
    send(client, rex, expect=422, kind="answer", body="No reply_to")
    send(client, rex, expect=422, kind="answer", reply_to=handoff["item_id"], body="Not a q")
    send(client, rex, expect=422, kind="answer", reply_to="missing", body="Unknown")


def test_state_endpoint_review_and_resume(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    item = send(client, finn, kind="instruction", to="role:dev", body="Ship it")
    act(client, rex, item["item_id"], "state", expect=403, json={"state": "review"})
    act(client, rex, item["item_id"], "claim")
    act(client, finn, item["item_id"], "state", expect=403, json={"state": "review"})
    assert act(client, rex, item["item_id"], "state", json={"state": "review"})["state"] == "review"
    back = act(client, rex, item["item_id"], "state", json={"state": "claimed"})
    assert back["state"] == "claimed"
    assert back["last_event"]["action"] == "resumed"
    act(client, rex, item["item_id"], "state", expect=422, json={"state": "done"})


def test_done_and_decline_are_final(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    item = send(client, finn, kind="instruction", to="role:dev", body="Finish it")
    act(client, rex, item["item_id"], "done", expect=403)  # not claimed yet
    act(client, rex, item["item_id"], "claim")
    done = act(client, rex, item["item_id"], "done", json={"note": "merged"})
    assert done["state"] == "done"
    assert act(client, rex, item["item_id"], "done")["state"] == "done"
    act(client, rex, item["item_id"], "decline", expect=409)
    act(client, rex, item["item_id"], "claim", expect=409)
    act(client, rex, item["item_id"], "release", expect=409)
    assert item["item_id"] not in ids(inbox(client, rex))


def test_idempotent_client_id(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    first = send(client, finn, kind="instruction", to="role:dev", body="Once", client_id="c-1")
    again = send(
        client, finn, expect=200, kind="instruction", to="role:dev", body="Once", client_id="c-1"
    )
    assert again["item_id"] == first["item_id"]
    assert ids(inbox(client, rex)) == [first["item_id"]]
    # The same client_id from another member is a different item.
    other = send(client, rex, kind="note", body="Mine", client_id="c-1")
    assert other["item_id"] != first["item_id"]


def test_agent_addressed_items(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", harness="codex")
    ada = join(client, "Ada")
    by_handle = send(client, finn, kind="instruction", to="agent:rex", body="Just you")
    assert by_handle["to"] == f"agent:{rex['member']['member_id']}"
    assert by_handle["item_id"] not in ids(inbox(client, ada))
    act(client, ada, by_handle["item_id"], "claim", expect=403)
    act(client, ada, by_handle["item_id"], "ack", expect=403)
    act(client, ada, by_handle["item_id"], "done", expect=403)
    # The recipient can finish without claiming first (implicit claim).
    done = act(client, rex, by_handle["item_id"], "done")
    assert done["state"] == "done"
    assert done["assignee"] == {
        "member_id": rex["member"]["member_id"],
        "handle": "Rex",
        "harness": "codex",
    }

    send(client, finn, expect=422, kind="instruction", to="agent:nobody", body="?")
    join(client, "Rex")  # a second "Rex" makes the handle ambiguous
    send(client, finn, expect=422, kind="instruction", to="agent:Rex", body="?")
    by_id = send(
        client, finn, kind="instruction", to=f"agent:{rex['member']['member_id']}", body="ok"
    )
    assert by_id["to"] == f"agent:{rex['member']['member_id']}"
    send(client, finn, expect=422, kind="instruction", body="no address")
    send(client, finn, expect=422, kind="instruction", to="team:x", body="bad address")


def test_resource_claims_are_advisory(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex")
    send(client, finn, expect=422, kind="claim", body="no resource")
    claim = send(
        client, finn, kind="claim", body="Working on login", context={"resource": "branch:login"}
    )
    assert claim["state"] == "pending"
    assert claim["to"] == "project"
    project = client.get(f"/projects/{pid(finn)}").json()
    assert [c["item_id"] for c in project["resource_claims"]] == [claim["item_id"]]
    send(client, rex, expect=422, kind="release", body="no parent")
    send(client, finn, kind="release", reply_to=claim["item_id"], body="Done with login")
    assert client.get(f"/projects/{pid(finn)}").json()["resource_claims"] == []


def test_channel_notices(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["reviewer"])
    item = send(client, finn, kind="handoff", to="role:reviewer", body="Please review\nlong body")
    act(client, rex, item["item_id"], "claim")
    act(client, rex, item["item_id"], "done")
    history = client.get(f"/channels/{finn['project']['channel_id']}/history").json()
    texts = [m["text"] for m in history if m["kind"] == "notice"]
    assert texts == [
        "Finn → role:reviewer · handoff · Please review",
        'Rex claimed "Please review"',
        'Rex finished "Please review"',
    ]
    assert history[0]["handle"] == "Finn"
    detail = client.get(f"/projects/{pid(finn)}/items/{item['item_id']}").json()
    assert detail["item"]["notice_message_id"] == history[0]["message_id"]


def test_notices_reach_agent_websockets(client):
    finn = join(client, "Finn")
    channel_id = finn["project"]["channel_id"]
    with client.websocket_connect(f"/channels/{channel_id}/ws") as ws:
        send(client, finn, kind="note", to="project", body="Live notice")
        event = ws.receive_json()
        assert event["type"] == "message"
        assert event["kind"] == "notice"
        assert event["text"] == "Finn → project · note · Live notice"


def test_member_key_only_in_join_response(client):
    finn = join(client, "Finn", roles=["dev"])
    rex = join(client, "Rex", roles=["dev"])
    keys = [finn["member_key"], rex["member_key"]]
    item = send(client, finn, kind="handoff", to="role:dev", body="Secret-free")
    act(client, rex, item["item_id"], "claim")
    send(client, rex, kind="question", reply_to=item["item_id"], body="Q?")
    bodies = [
        client.get("/projects").text,
        client.get(f"/projects/{pid(finn)}").text,
        client.get(f"/projects/{pid(finn)}/items/{item['item_id']}").text,
        client.get(f"/projects/{pid(finn)}/inbox", headers=hdr(finn)).text,
        client.get(f"/projects/{pid(finn)}/inbox", headers=hdr(rex)).text,
        client.get("/work-items").text,
        client.get(f"/channels/{finn['project']['channel_id']}/history").text,
        client.get("/channels").text,
    ]
    for body in bodies:
        assert "member_key" not in body
        for key in keys:
            assert key not in body


# --------------------------------------------------------------------------
# Board query
# --------------------------------------------------------------------------


def test_work_items_updated_after_and_done_cutoff(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    old = send(client, finn, kind="instruction", to="role:dev", body="Old work")
    act(client, rex, old["item_id"], "claim")
    act(client, rex, old["item_id"], "done")
    with SessionLocal() as db:
        db.get(WorkItemDB, old["item_id"]).updated_at = datetime.now(UTC) - timedelta(days=8)
        db.commit()
    recent = send(client, finn, kind="instruction", to="role:dev", body="Recent work")

    first = client.get("/work-items").json()
    assert ids(first) == [recent["item_id"]]
    assert first["items"][0]["project_name"] == "repo"
    assert first["items"][0]["channel_id"] == finn["project"]["channel_id"]
    assert ids(client.get("/work-items", params={"include_done_days": 30}).json()) == [
        old["item_id"],
        recent["item_id"],
    ]

    server_time = first["server_time"]
    assert client.get("/work-items", params={"updated_after": server_time}).json()["items"] == []
    act(client, rex, recent["item_id"], "claim")
    changed = client.get("/work-items", params={"updated_after": server_time}).json()
    assert ids(changed) == [recent["item_id"]]
    assert changed["items"][0]["state"] == "claimed"
    assert changed["items"][0]["last_event"]["action"] == "claimed"
    assert changed["items"][0]["last_event"]["by"]["handle"] == "Rex"

    mine = client.get("/work-items", params={"member_id": rex["member"]["member_id"]}).json()
    assert ids(mine) == [recent["item_id"]]
    other = join(client, "Zed", remote="https://example.com/x/y")
    assert client.get("/work-items", params={"project_id": pid(other)}).json()["items"] == []


def test_hosted_mode_hides_projects(client, monkeypatch):
    finn = join(client, "Finn")
    monkeypatch.setenv("AGENT_PTT_HOSTED", "1")
    assert client.get("/projects").status_code == 404
    assert client.get("/work-items").status_code == 404
    assert client.post("/projects/join", json={"remote": REMOTE, "handle": "x"}).status_code == 404
    assert client.get(f"/projects/{pid(finn)}/inbox", headers=hdr(finn)).status_code == 404


def test_shared_api_key_applies(client, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_API_KEY", "s3cret")
    assert client.get("/projects").status_code == 401
    assert client.get("/projects", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_init_db_adds_project_tables_to_an_existing_database(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import Session, sessionmaker

    from agent_ptt import db

    legacy = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy.begin() as connection:
        connection.execute(
            text("""
            CREATE TABLE messages (
                message_id VARCHAR PRIMARY KEY, channel_id VARCHAR,
                sender_key VARCHAR, handle VARCHAR, text TEXT, timestamp DATETIME
            )
        """)
        )
        connection.execute(
            text("INSERT INTO messages VALUES ('m1', 'c1', 'k1', 'Ada', 'Hi', NULL)")
        )
    with monkeypatch.context() as patch:
        patch.setattr(db, "engine", legacy)
        patch.setattr(db, "SessionLocal", sessionmaker(bind=legacy, class_=Session))
        db.init_db()
        db.init_db()
    tables = set(inspect(legacy).get_table_names())
    assert {
        "projects",
        "project_members",
        "work_items",
        "work_item_deliveries",
        "work_item_events",
    } <= tables
    with legacy.connect() as connection:
        assert connection.execute(text("SELECT text FROM messages")).scalar_one() == "Hi"
    legacy.dispose()


def test_quickstart_covers_projects(client):
    text = client.get("/llms.txt").text
    assert "## Quickstart (5 requests)" in text
    assert "## Projects and work items (monitor mode)" in text
    assert "/projects/join" in text and "/inbox?wait=60" in text and "/claim" in text
