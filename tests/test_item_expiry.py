"""Task 41: obsolete work items leave the Board.

A: informational items close on ack. B: the ``expired`` state and the expiry
pass. C: the Board hides old final items. D: the admin bulk route.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_ptt import projects as pj
from agent_ptt.db import SessionLocal
from agent_ptt.models import ProjectMemberDB, WorkItemDB, WorkItemEventDB

REMOTE = "git@github.com:Owner/Board.git"


@pytest.fixture(autouse=True)
def _reset_projects():
    pj.reset_runtime_state()
    yield
    pj.reset_runtime_state()


def join(client, handle, roles=None, remote=REMOTE):
    body = {"remote": remote, "handle": handle}
    if roles is not None:
        body["roles"] = roles
    response = client.post("/projects/join", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def hdr(joined):
    return {"X-Member-Key": joined["member_key"]}


def pid(joined):
    return joined["project"]["project_id"]


def mid(joined):
    return joined["member"]["member_id"]


def send(client, joined, expect=201, **body):
    response = client.post(f"/projects/{pid(joined)}/items", json=body, headers=hdr(joined))
    assert response.status_code == expect, response.text
    return response.json()


def act(client, joined, item_id, action, expect=200, json=None):
    response = client.post(
        f"/projects/{pid(joined)}/items/{item_id}/{action}", headers=hdr(joined), json=json
    )
    assert response.status_code == expect, response.text
    return response.json()


def inbox_ids(client, joined):
    response = client.get(f"/projects/{pid(joined)}/inbox", headers=hdr(joined))
    assert response.status_code == 200, response.text
    return [item["item_id"] for item in response.json()["items"]]


def state_of(item_id):
    with SessionLocal() as db:
        return db.get(WorkItemDB, item_id).state


def events(item_id):
    with SessionLocal() as db:
        rows = (
            db.query(WorkItemEventDB).filter_by(item_id=item_id).order_by(WorkItemEventDB.event_id)
        )
        return [(e.action, e.by_member_id, e.to_state, e.note) for e in rows]


def age_item(item_id, days):
    with SessionLocal() as db:
        db.get(WorkItemDB, item_id).updated_at = datetime.now(UTC) - timedelta(days=days)
        db.commit()


def mark_gone(*joined):
    with SessionLocal() as db:
        for j in joined:
            db.get(ProjectMemberDB, mid(j)).gone_at = datetime.now(UTC)
        db.commit()


def notices(client, joined):
    history = client.get(f"/channels/{joined['project']['channel_id']}/history").json()
    return [m["text"] for m in history if m["kind"] == "notice"]


def board_notices(client, joined):
    return [t for t in notices(client, joined) if t.startswith("Board:")]


def reap(client):
    response = client.post("/reap")
    assert response.status_code == 200, response.text
    return response.json()


def board_ids(client, **params):
    response = client.get("/work-items", params=params)
    assert response.status_code == 200, response.text
    return [item["item_id"] for item in response.json()["items"]]


# --------------------------------------------------------------------------
# A. Informational items close on ack
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "note", "to": "project", "body": "FYI the build is green"},
        {"kind": "instruction", "to": "role:dev", "body": "heads up", "fyi": True},
    ],
)
def test_ack_closes_an_informational_item_on_the_first_ack(client, body):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    kim = join(client, "Kim", roles=["dev"])
    item = send(client, finn, **body)
    acked = act(client, rex, item["item_id"], "ack")
    assert acked["state"] == "done"
    assert acked["last_event"]["action"] == "auto-done"
    assert acked["last_event"]["by"]["handle"] == "Rex"
    assert events(item["item_id"])[-1] == ("auto-done", mid(rex), "done", None)
    # The other recipient no longer sees it; a late ack changes nothing.
    assert item["item_id"] not in inbox_ids(client, kim)
    assert act(client, kim, item["item_id"], "ack")["state"] == "done"


def test_answer_closes_on_ack(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex")
    question = send(client, finn, kind="question", to="agent:Rex", body="Which port?")
    answer = send(client, rex, kind="answer", reply_to=question["item_id"], body="8771")
    assert act(client, finn, answer["item_id"], "ack")["state"] == "done"


def test_ack_leaves_tasks_open(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    for kind in ("instruction", "handoff", "question"):
        item = send(client, finn, kind=kind, to="role:dev", body=f"a {kind}")
        assert act(client, rex, item["item_id"], "ack")["state"] == "acked"


# --------------------------------------------------------------------------
# B. Expiry
# --------------------------------------------------------------------------


def test_untouched_item_expires_after_seven_days(client):
    finn = join(client, "Finn")
    join(client, "Rex", roles=["dev"])
    old = send(client, finn, kind="instruction", to="role:dev", body="Old work")
    fresh = send(client, finn, kind="instruction", to="role:dev", body="Fresh work")
    age_item(old["item_id"], 8)
    age_item(fresh["item_id"], 6)
    body = reap(client)
    assert body["items_expired"] == 1
    assert body["items_closed"] == 0
    assert body["item_projects"] == [
        {"project_id": pid(finn), "project": "board", "expired": 1, "closed": 0}
    ]
    assert state_of(old["item_id"]) == "expired"
    assert state_of(fresh["item_id"]) == "pending"
    assert events(old["item_id"])[-1] == ("expired", None, "expired", "untouched 7 days")
    assert board_notices(client, finn) == [
        "Board: 1 stale item expired (untouched 7 days or everyone gone)"
    ]
    # Nothing new: no second notice.
    assert reap(client)["items_expired"] == 0
    assert len(board_notices(client, finn)) == 1


def test_expire_days_is_configurable_and_zero_is_off(client, monkeypatch):
    finn = join(client, "Finn")
    join(client, "Rex", roles=["dev"])
    item = send(client, finn, kind="instruction", to="role:dev", body="Work")
    age_item(item["item_id"], 3)
    monkeypatch.setenv("AGENT_PTT_ITEM_EXPIRE_DAYS", "0")
    assert reap(client)["items_expired"] == 0
    age_item(item["item_id"], 300)
    assert reap(client)["items_expired"] == 0
    monkeypatch.setenv("AGENT_PTT_ITEM_EXPIRE_DAYS", "2")
    assert client.get("/reaper").json()["item_expire_days"] == 2
    assert reap(client)["items_expired"] == 1
    assert board_notices(client, finn) == [
        "Board: 1 stale item expired (untouched 2 days or everyone gone)"
    ]


def test_bad_expire_days_falls_back(monkeypatch):
    monkeypatch.setenv("AGENT_PTT_ITEM_EXPIRE_DAYS", "soon")
    assert pj.item_expire_days() == 7
    monkeypatch.setenv("AGENT_PTT_BOARD_DONE_DAYS", "-1")
    assert pj.board_done_days() == 3


def test_item_expires_when_sender_and_every_recipient_are_gone(client, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_ITEM_EXPIRE_DAYS", "0")  # Only the gone rule.
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    kim = join(client, "Kim", roles=["dev"])
    direct = send(client, finn, kind="instruction", to="agent:Rex", body="Direct")
    role = send(client, finn, kind="handoff", to="role:dev", body="Role")
    project = send(client, finn, kind="question", to="project", body="Anyone?")
    mark_gone(finn, rex)
    # Kim (a role/project recipient) is still here: only the direct item expires.
    body = reap(client)
    assert body["items_expired"] == 1
    assert state_of(direct["item_id"]) == "expired"
    assert events(direct["item_id"])[-1][3] == "everyone gone"
    assert board_notices(client, kim) == ["Board: 1 stale item expired (everyone gone)"]
    mark_gone(kim)
    assert reap(client)["items_expired"] == 2
    assert {state_of(role["item_id"]), state_of(project["item_id"])} == {"expired"}


def test_a_deleted_member_counts_as_gone(client, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_ITEM_EXPIRE_DAYS", "0")
    finn = join(client, "Finn")
    rex = join(client, "Rex")
    item = send(client, finn, kind="instruction", to="agent:Rex", body="Orphan")
    with SessionLocal() as db:
        db.delete(db.get(ProjectMemberDB, mid(finn)))
        db.commit()
    assert reap(client)["items_expired"] == 0  # Rex is still here.
    mark_gone(rex)
    assert reap(client)["items_expired"] == 1
    assert state_of(item["item_id"]) == "expired"


def test_held_item_expires_and_its_lease_is_cleared(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    item = send(client, finn, kind="instruction", to="role:dev", body="Slow work")
    act(client, rex, item["item_id"], "claim")
    age_item(item["item_id"], 10)
    with SessionLocal() as db:
        db.get(WorkItemDB, item["item_id"]).lease_expires_at = datetime.now(UTC) + timedelta(
            hours=1
        )
        db.commit()
    assert reap(client)["items_expired"] == 1
    with SessionLocal() as db:
        row = db.get(WorkItemDB, item["item_id"])
        assert row.state == "expired"
        assert row.lease_expires_at is None


def test_a_reply_counts_as_a_touch(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    item = send(client, finn, kind="instruction", to="role:dev", body="Work")
    act(client, rex, item["item_id"], "claim")
    age_item(item["item_id"], 8)
    send(client, rex, kind="question", reply_to=item["item_id"], body="Which branch?")
    assert reap(client)["items_expired"] == 0
    assert state_of(item["item_id"]) == "waiting"


def test_expired_items_are_not_delivered_or_in_the_inbox(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    item = send(client, finn, kind="instruction", to="role:dev", body="Stale")
    assert inbox_ids(client, rex) == [item["item_id"]]
    age_item(item["item_id"], 8)
    reap(client)
    assert inbox_ids(client, rex) == []
    # A late joiner with the role gets no delivery for it either.
    kim = join(client, "Kim", roles=["dev"])
    assert inbox_ids(client, kim) == []
    with SessionLocal() as db:
        assert pj.open_item_count(db, pid(finn)) == 0


def test_expired_item_reopens_on_claim(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    item = send(client, finn, kind="instruction", to="role:dev", body="Revive me")
    age_item(item["item_id"], 8)
    reap(client)
    claimed = act(client, rex, item["item_id"], "claim")
    assert claimed["state"] == "claimed"
    assert claimed["assignee"]["handle"] == "Rex"
    actions = [e[0] for e in events(item["item_id"])]
    assert actions[-3:] == ["expired", "reopened", "claimed"]
    assert 'Rex reopened "Revive me"' in notices(client, finn)


def test_expired_item_reopens_on_reply(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    item = send(client, finn, kind="instruction", to="role:dev", body="Reply revives")
    age_item(item["item_id"], 8)
    reap(client)
    send(client, finn, kind="note", reply_to=item["item_id"], to="role:dev", body="still needed")
    assert state_of(item["item_id"]) == "pending"
    assert item["item_id"] in inbox_ids(client, rex)


def test_expired_direct_item_can_be_finished(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex")
    item = send(client, finn, kind="instruction", to="agent:Rex", body="Done late")
    age_item(item["item_id"], 8)
    reap(client)
    assert act(client, rex, item["item_id"], "done")["state"] == "done"
    # An unrelated member can't revive it by trying to finish it.
    other = send(client, finn, kind="instruction", to="agent:Rex", body="Not yours")
    age_item(other["item_id"], 8)
    reap(client)
    kim = join(client, "Kim")
    act(client, kim, other["item_id"], "done", expect=403)
    assert state_of(other["item_id"]) == "expired"


def test_backlog_pass_closes_acked_informational_items(client):
    """Rows from before task 41: acked notes that stayed open close on /reap."""
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    note = send(client, finn, kind="note", to="project", body="old note")
    fyi = send(client, finn, kind="instruction", to="role:dev", body="old fyi", fyi=True)
    unread = send(client, finn, kind="note", to="project", body="unread note")
    with SessionLocal() as db:
        for item_id in (note["item_id"], fyi["item_id"]):
            pj._ack_delivery(db, db.get(WorkItemDB, item_id), db.get(ProjectMemberDB, mid(rex)))
            db.get(WorkItemDB, item_id).state = "acked"  # The old lifecycle.
        db.commit()
    body = reap(client)
    assert body["items_closed"] == 2
    assert body["items_expired"] == 0
    assert state_of(note["item_id"]) == "done"
    assert state_of(unread["item_id"]) == "pending"
    assert events(note["item_id"])[-1][:2] == ("auto-done", mid(rex))
    assert board_notices(client, finn) == ["Board: 2 read FYI/notes closed"]


def test_one_notice_per_project_per_pass(client):
    finn = join(client, "Finn")
    join(client, "Rex", roles=["dev"])
    zed = join(client, "Zed", remote="https://example.com/x/other")
    join(client, "Ivy", roles=["dev"], remote="https://example.com/x/other")
    for n in range(3):
        old = send(client, finn, kind="instruction", to="role:dev", body=f"old {n}")
        age_item(old["item_id"], 9)
    other = send(client, zed, kind="instruction", to="role:dev", body="other old")
    age_item(other["item_id"], 9)
    body = reap(client)
    assert body["items_expired"] == 4
    assert board_notices(client, finn) == [
        "Board: 3 stale items expired (untouched 7 days or everyone gone)"
    ]
    assert board_notices(client, zed) == [
        "Board: 1 stale item expired (untouched 7 days or everyone gone)"
    ]


def test_periodic_pass_runs_item_expiry(client):
    from agent_ptt import server

    finn = join(client, "Finn")
    join(client, "Rex", roles=["dev"])
    item = send(client, finn, kind="instruction", to="role:dev", body="Old")
    age_item(item["item_id"], 8)
    result = client.portal.call(server._expire_items_once)
    assert result.expired_total == 1
    assert state_of(item["item_id"]) == "expired"


# --------------------------------------------------------------------------
# C. The Board hides old final items
# --------------------------------------------------------------------------


def test_board_hides_final_items_older_than_three_days(client, monkeypatch):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    done = send(client, finn, kind="instruction", to="role:dev", body="Done long ago")
    act(client, rex, done["item_id"], "claim")
    act(client, rex, done["item_id"], "done")
    expired = send(client, finn, kind="instruction", to="role:dev", body="Expired")
    age_item(expired["item_id"], 8)
    reap(client)
    recent = send(client, finn, kind="instruction", to="role:dev", body="Open")
    age_item(done["item_id"], 4)
    age_item(expired["item_id"], 2)
    open_old = send(client, finn, kind="instruction", to="role:dev", body="Open but quiet")
    age_item(open_old["item_id"], 5)  # Open: never hidden (until it expires).

    body = client.get("/work-items").json()
    assert body["done_days"] == 3
    assert set(board_ids(client)) == {expired["item_id"], recent["item_id"], open_old["item_id"]}
    expired_out = next(i for i in body["items"] if i["item_id"] == expired["item_id"])
    assert expired_out["state"] == "expired"
    assert expired_out["last_event"]["action"] == "expired"
    assert expired_out["last_event"]["by"] is None

    everything = client.get("/work-items", params={"all": 1}).json()
    assert everything["done_days"] is None
    assert done["item_id"] in [i["item_id"] for i in everything["items"]]
    assert done["item_id"] in board_ids(client, include_done_days=5)
    monkeypatch.setenv("AGENT_PTT_BOARD_DONE_DAYS", "1")
    assert expired["item_id"] not in board_ids(client)


# --------------------------------------------------------------------------
# D. Bulk close / expire
# --------------------------------------------------------------------------


def bulk(client, project_id, ids, state, expect=200, headers=None):
    response = client.post(
        f"/projects/{project_id}/work-items/bulk",
        json={"ids": ids, "state": state},
        headers=headers or {},
    )
    assert response.status_code == expect, response.text
    return response.json()


def test_bulk_close_and_expire(client):
    finn = join(client, "Finn")
    rex = join(client, "Rex", roles=["dev"])
    a = send(client, finn, kind="instruction", to="role:dev", body="A")
    b = send(client, finn, kind="handoff", to="role:dev", body="B")
    c = send(client, finn, kind="instruction", to="role:dev", body="C")
    act(client, rex, c["item_id"], "claim")
    act(client, rex, c["item_id"], "decline")
    zed = join(client, "Zed", remote="https://example.com/x/other")
    foreign = send(client, zed, kind="note", to="project", body="elsewhere")

    body = bulk(client, pid(finn), [a["item_id"], c["item_id"], foreign["item_id"], "nope"], "done")
    assert body["changed"] == 1
    assert body["results"] == [
        {"item_id": a["item_id"], "ok": True, "changed": True, "state": "done"},
        {"item_id": c["item_id"], "ok": False, "changed": False, "state": "declined",
         "error": "already declined"},
        {"item_id": foreign["item_id"], "ok": False, "error": "not found"},
        {"item_id": "nope", "ok": False, "error": "not found"},
    ]  # fmt: skip
    assert events(a["item_id"])[-1] == ("closed", None, "done", "admin (bulk)")

    body = bulk(client, pid(finn), [b["item_id"], a["item_id"]], "expired")
    assert [r["state"] for r in body["results"]] == ["expired", "done"]
    assert body["results"][1]["error"] == "already done"
    assert events(b["item_id"])[-1] == ("expired", None, "expired", "admin (bulk)")
    # An expired item can still be closed; repeating is a no-op.
    assert bulk(client, pid(finn), [b["item_id"]], "done")["changed"] == 1
    assert bulk(client, pid(finn), [b["item_id"]], "done")["results"][0]["changed"] is False

    assert board_notices(client, finn) == [
        "Board: closed 1 item (admin)",
        "Board: expired 1 item (admin)",
        "Board: closed 1 item (admin)",
    ]
    assert inbox_ids(client, rex) == []


def test_bulk_validation_auth_and_hosted(client, monkeypatch):
    finn = join(client, "Finn")
    item = send(client, finn, kind="note", to="project", body="x")
    bulk(client, pid(finn), [item["item_id"]], "declined", expect=422)
    bulk(client, pid(finn), [], "done", expect=422)
    bulk(client, "no-such-project", [item["item_id"]], "done", expect=404)
    monkeypatch.setenv("AGENT_PTT_API_KEY", "secret")
    bulk(client, pid(finn), [item["item_id"]], "done", expect=401)
    ok = bulk(client, pid(finn), [item["item_id"]], "done", headers={"X-API-Key": "secret"})
    assert ok["changed"] == 1
    monkeypatch.setenv("AGENT_PTT_HOSTED", "1")
    bulk(client, pid(finn), [item["item_id"]], "done", expect=404, headers={"X-API-Key": "secret"})
