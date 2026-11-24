"""Dead channel seats don't hold names; the admin rename (handles.py)."""

from datetime import UTC, datetime, timedelta

from agent_ptt import channel as ch
from agent_ptt import handles
from agent_ptt.auth import API_KEY_ENV
from agent_ptt.compound_names import COMPOUNDS
from agent_ptt.db import SessionLocal
from agent_ptt.models import (
    HandleClaimDB,
    MentionReceiverDB,
    ParticipantKeyDB,
    ProjectMemberDB,
    SessionIdentityDB,
)
from agent_ptt.session_identity import NAMES

REMOTE = "git@github.com:Owner/Repo.git"


def channel(client, name="room"):
    return client.post("/channels", json={"name": name}).json()["channel_id"]


def cjoin(client, cid, session, expect=200, **extra):
    body = {"handle": "ignored", "agent": "Claude", "session_id": session, **extra}
    response = client.post(f"/channels/{cid}/join", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def pjoin(client, handle="ignored", expect=200, **extra):
    response = client.post("/projects/join", json={"remote": REMOTE, "handle": handle, **extra})
    assert response.status_code == expect, response.text
    return response.json()


def seat(cid, handle, state="offline", seen_ago=timedelta(0)):
    """A channel participant (DB + memory) as restore_channels leaves it."""
    with SessionLocal() as db:
        key = ch.join_channel(cid, handle, db=db)
    key.state = state
    key.last_seen = datetime.now(UTC) - seen_ago
    return key


def claim(handle, session, ago=timedelta(0)):
    with SessionLocal() as db:
        owner = handles.Owner(
            handles.session_owner("Claude", session), harness="Claude", session_id=session
        )
        row = handles.take(db, handle, owner)
        row.last_seen = datetime.now(UTC) - ago
        db.commit()


def seat_row(key_id):
    with SessionLocal() as db:
        return db.get(ParticipantKeyDB, key_id)


def plain(handle):
    return handle in NAMES


# --- dead seats don't hold names ---------------------------------------------


def test_stale_seats_leave_plain_names_free(client):
    """The live-DB situation: 31 live claims, 117 dead seats over almost every name."""
    rooms = [channel(client, f"room{i}") for i in range(4)]
    live = list(NAMES[:31])
    for i, name in enumerate(live):
        claim(name, f"live-{i}")
    seats = []
    for i, name in enumerate(NAMES[2:] + tuple(f"{n}2" for n in NAMES[:19])):
        room = rooms[i % len(rooms)]
        if i % 3 == 0:  # restored after a restart and never came back
            seats.append(seat(room, name, "offline"))
        elif i % 3 == 1:  # away for days (a hooked session that died)
            seats.append(seat(room, name, "away", seen_ago=timedelta(days=3)))
        else:  # its claim expired long ago
            claim(name, f"dead-{i}", ago=timedelta(days=5))
            seats.append(seat(room, name, "offline", seen_ago=timedelta(days=5)))
    assert len(seats) == 117

    joined = cjoin(client, rooms[0], "fresh")
    assert plain(joined["handle"]), joined["handle"]
    assert joined["handle"] not in live
    # The dead seat of that name was detached on hand-out (DB and memory).
    dead = [s for s in seats if s.handle == joined["handle"]]
    assert dead
    for key in dead:
        assert seat_row(key.key_id).channel_id is None
        assert ch.get_participant(key.key_id) is None
    roster = [p.handle for p in ch.get_channel(rooms[0]).participants.values()]
    assert roster.count(joined["handle"]) == 1


def test_a_live_seat_without_a_claim_still_blocks_its_name(client):
    cid = channel(client)
    others = (set(NAMES) | set(COMPOUNDS)) - {"Atlas"}
    seat(cid, "Atlas", "active")
    with SessionLocal() as db:
        name = handles.allocate(db, exclude=others)
    assert name != "Atlas" and name.endswith("2")


def test_which_seats_count_as_alive(client):
    cid, archived = channel(client), channel(client, "old")
    seat(cid, "Atlas", "away", seen_ago=timedelta(hours=2))  # hooked, idle: alive
    seat(cid, "Nova", "offline")  # restored, never seen since
    seat(cid, "Milo", "active", seen_ago=timedelta(hours=30))  # past the name TTL
    seat(archived, "Cleo", "active")
    ch.get_channel(archived).archived = True
    assert handles.live_seat_handles() == {"atlas"}


def test_hand_out_keeps_the_sessions_own_seats(client):
    """Eviction spares seats registered to the session that gets the name."""
    cid = channel(client)
    with SessionLocal() as db:
        mine = ch.join_channel(cid, "Atlas", db=db)
        db.add(
            MentionReceiverDB(
                key_id=mine.key_id,
                recipient_id=handles.recipient_id("Claude", "me"),
                channel_id=cid,
            )
        )
        db.commit()
        dead = ch.join_channel(cid, "Atlas", db=db)
        evicted = handles.evict_seats(
            db, "atlas", keep_recipients={handles.recipient_id("Claude", "me")}
        )
        db.commit()
    assert evicted == {cid}
    assert ch.get_participant(mine.key_id) is not None
    assert ch.get_participant(dead.key_id) is None
    assert seat_row(dead.key_id).channel_id is None
    with SessionLocal() as db:
        assert db.query(MentionReceiverDB).filter_by(key_id=mine.key_id).count() == 1


# --- machine on claims ---------------------------------------------------------


def test_old_plugin_project_join_teaches_the_claim_its_machine(client):
    # 0.5.6: the channel join has a session but no machine …
    joined = cjoin(client, channel(client), "s1")
    assert handles.get_claim(SessionLocal(), joined["handle"]).machine is None
    # … its project join sends the machine but no session.
    pjoin(client, joined["handle"], harness="claude", machine="kbot")
    with SessionLocal() as db:
        row = handles.get_claim(db, joined["handle"])
        assert (row.machine, row.session_id) == ("kbot", "s1")


def test_fill_unknown_never_overwrites():
    row = HandleClaimDB(handle_ci="x", handle="X", owner_id="o", machine="mac", harness=None)
    handles.fill_unknown(row, handles.Owner("other", harness="codex", machine="kbot"))
    assert (row.machine, row.harness) == ("mac", "codex")


# --- POST /handles/{handle}/rename ---------------------------------------------


def _oscar2(client, plugin_version="0.6.2"):
    """A 0.6 session called Oscar2 in a channel and a project; Oscar's holder is dead."""
    cid = channel(client)
    claim("Oscar", "dead", ago=timedelta(days=3))
    dead = seat(cid, "Oscar", "offline", seen_ago=timedelta(days=3))
    member = pjoin(
        client,
        "Oscar2",
        session_id="s2",
        agent="Claude",
        harness="claude",
        machine="kbot",
        plugin_version=plugin_version,
    )
    assert member["member"]["handle"] == "Oscar2"
    joined = cjoin(client, cid, "s2", machine="kbot", receive_mentions=True)
    assert joined["handle"] == "Oscar2"
    return cid, joined, dead, member


def test_rename_moves_the_name_everywhere(client):
    cid, joined, dead, member = _oscar2(client)
    project_channel = member["project"]["channel_id"]
    response = client.post("/handles/oscar2/rename")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "renamed": "Oscar2",
        "to": "Oscar",
        "channels": sorted({cid, project_channel}),
    }
    with SessionLocal() as db:
        assert db.get(SessionIdentityDB, ("s2", "Claude")).handle == "Oscar"
        assert db.get(HandleClaimDB, "oscar2") is None
        new = db.get(HandleClaimDB, "oscar")
        assert (new.session_id, new.machine) == ("s2", "kbot")
        assert db.get(ProjectMemberDB, member["member"]["member_id"]).handle == "Oscar"
        assert db.get(ParticipantKeyDB, joined["key_id"]).handle == "Oscar"
    # In memory: the seat is renamed, the dead Oscar seat is gone, a notice is posted.
    live = ch.get_channel(cid)
    assert live.participants[joined["key_id"]].handle == "Oscar"
    assert dead.key_id not in live.participants
    assert seat_row(dead.key_id).channel_id is None
    assert live.messages[-1].kind == "notice"
    assert live.messages[-1].text == "@Oscar2 is now @Oscar (name freed up)"
    # The session's next join keeps the new name and its seat.
    again = cjoin(client, cid, "s2", machine="kbot", receive_mentions=True)
    assert (again["handle"], again["key_id"]) == ("Oscar", joined["key_id"])


def test_rename_to_an_explicit_name(client):
    _oscar2(client)
    response = client.post("/handles/Oscar2/rename", json={"to": "Gandalf"})
    assert response.json()["to"] == "Gandalf"
    assert handles.get_claim(SessionLocal(), "Gandalf").session_id == "s2"


def test_rename_refuses_a_live_target(client):
    cid, *_ = _oscar2(client)
    claim("Oscar", "alive")
    response = client.post("/handles/Oscar2/rename")
    assert response.status_code == 409
    assert "live claim" in response.json()["error"]
    # A live seat without a claim blocks it too.
    with SessionLocal() as db:
        handles.release(db, "Oscar")
        db.commit()
    seat(cid, "Oscar", "active")
    assert client.post("/handles/Oscar2/rename").status_code == 409
    assert handles.get_claim(SessionLocal(), "Oscar2").session_id == "s2"


def test_rename_refuses_old_plugins_and_sessionless_holders(client):
    _oscar2(client, plugin_version="0.5.6")
    response = client.post("/handles/Oscar2/rename")
    assert response.status_code == 409
    assert "0.5.6" in response.json()["error"]

    pjoin(client, "Hugo3", harness="codex", machine="mac")  # no session
    response = client.post("/handles/Hugo3/rename")
    assert response.status_code == 409
    assert "without a session" in response.json()["error"]

    assert client.post("/handles/nobody/rename").status_code == 404


def test_rename_needs_the_api_key(client, monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "k")
    assert client.post("/handles/Oscar2/rename").status_code == 401


def test_restored_hooked_seats_stay_offline_and_free_their_names(client):
    """Task 42: 10 min after a restart the idle reaper turned every restored
    hooked seat ``away``, so dead seats counted as live and new sessions got Name2."""
    import asyncio

    from agent_ptt import server

    rooms = [channel(client, f"restored{i}") for i in range(4)]
    seats = []
    for i, name in enumerate(NAMES):
        key = seat(rooms[i % len(rooms)], name, "offline")  # restored, never came back
        key.mention_id = f"receiver-{i}"  # hooked: it had a mention receiver
        seats.append(key)

    later = datetime.now(UTC) + timedelta(minutes=11)
    asyncio.run(server._reap_idle_once(600, now=later))
    assert {key.state for key in seats} == {"offline"}

    joined = cjoin(client, rooms[0], "after-restart")
    assert plain(joined["handle"]), joined["handle"]
