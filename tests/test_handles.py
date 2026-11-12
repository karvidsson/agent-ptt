"""The server-wide handle registry (handles.py): one live owner per name."""

from datetime import UTC, datetime, timedelta

from agent_ptt import channel as ch
from agent_ptt import handles
from agent_ptt.auth import API_KEY_ENV
from agent_ptt.db import SessionLocal
from agent_ptt.handle_migration import migrate
from agent_ptt.models import (
    ChannelDB,
    HandleClaimDB,
    MentionReceiverDB,
    MessageDB,
    ParticipantKeyDB,
    ProjectDB,
    ProjectMemberDB,
    SessionIdentityDB,
    VoiceProfile,
)
from agent_ptt.voices import save_voice_profile

REMOTE = "git@github.com:Owner/Repo.git"


def seed_voices():
    with SessionLocal() as db:
        for voice_id in ("builder", "ruler"):
            save_voice_profile(VoiceProfile(voice_id=voice_id, display_name=voice_id), db)


def channel(client, name="room"):
    return client.post("/channels", json={"name": name}).json()["channel_id"]


def cjoin(client, cid, session=None, expect=200, **extra):
    body = {"handle": extra.pop("handle", "ignored"), **extra}
    if session:
        body.setdefault("agent", "Claude")
        body["session_id"] = session
    response = client.post(f"/channels/{cid}/join", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def pjoin(client, handle="ignored", expect=200, **extra):
    response = client.post("/projects/join", json={"remote": REMOTE, "handle": handle, **extra})
    assert response.status_code == expect, response.text
    return response.json()


def age(handle, delta):
    """Pretend the claim on ``handle`` was last seen ``delta`` ago."""
    handles.reset_runtime_state()
    with SessionLocal() as db:
        claim = db.get(HandleClaimDB, handle.casefold())
        claim.last_seen = datetime.now(UTC) - delta
        db.commit()


def claim_of(handle):
    with SessionLocal() as db:
        return db.get(HandleClaimDB, handle.casefold())


# --- one name everywhere ------------------------------------------------------


def test_one_name_across_channels_and_projects(client):
    seed_voices()
    first = cjoin(client, channel(client, "one"), "s1", machine="kbot")
    second = cjoin(client, channel(client, "two"), "s1", machine="kbot")
    member = pjoin(client, "Whatever", session_id="s1", agent="Claude", harness="claude")
    assert first["handle"] == second["handle"] == member["member"]["handle"]
    # Another agent can't take it, in a channel or a project.
    other = cjoin(client, channel(client, "three"), "s2", agent="Codex")
    assert other["handle"] != first["handle"]
    taken = pjoin(client, first["handle"], expect=409, harness="codex", machine="mac")
    assert taken["error"] == "handle_taken"
    assert taken["held_by"]["harness"] == "claude" and taken["held_by"]["machine"] == "kbot"
    raw = cjoin(client, channel(client, "four"), expect=409, handle=first["handle"].lower())
    assert raw["detail"] == f"{first['handle']} is held by claude on kbot"


def test_raw_join_takes_a_free_name_and_keeps_it(client):
    cid = channel(client)
    first = cjoin(client, cid, handle="Kristian")
    assert first["handle_claim"]["handle"] == "Kristian"
    cjoin(client, channel(client, "other"), handle="Kristian")  # same client, another tab
    refused = cjoin(client, cid, "s1", expect=409, handle="Kristian", pin_handle=True)
    assert refused["held_by"]["harness"] == "cli"


# --- pins -------------------------------------------------------------------


def test_pin_from_another_harness_or_machine_gets_409(client):
    cid = channel(client)
    pinned = cjoin(client, cid, "c1", agent="Cursor", handle="Theo", pin_handle=True, machine="Mac")
    assert pinned["handle"] == "Theo"
    assert pinned["handle_claim"] == {"handle": "Theo", "pinned": True, "expires_after_hours": 720}
    body = cjoin(client, cid, "k1", expect=409, handle="Theo", pin_handle=True, machine="kbot")
    assert set(body) == {"error", "handle", "held_by", "detail"}
    assert body["error"] == "handle_taken" and body["handle"] == "Theo"
    assert body["held_by"]["harness"] == "cursor" and body["held_by"]["machine"] == "Mac"
    assert body["detail"] == "Theo is held by cursor on Mac"
    datetime.fromisoformat(body["held_by"]["last_seen"])
    # The same harness on another machine is refused too.
    cjoin(client, cid, "c2", expect=409, agent="Cursor", handle="Theo", pin_handle=True)
    # The plugin's fallback (same payload minus pin_handle) gets an automatic name.
    fallback = cjoin(client, cid, "k1", handle="Theo", machine="kbot")
    assert fallback["handle"] != "Theo"


def test_same_harness_and_machine_takes_a_pin_over(client):
    seed_voices()
    cid = channel(client)
    old = cjoin(client, cid, "old", handle="Ember", pin_handle=True, machine="kbot")
    new = cjoin(client, cid, "new", handle="Ember", pin_handle=True, machine="KBOT")
    assert new["handle"] == "Ember" and new["voice_id"] == old["voice_id"]
    assert claim_of("Ember").session_id == "new"
    # The old session is someone else now.
    back = cjoin(client, channel(client, "b"), "old", machine="kbot")
    assert back["handle"] != "Ember"


# --- restart stickiness -----------------------------------------------------


def test_new_session_in_the_same_folder_inherits_a_quiet_name(client):
    seed_voices()
    cid = channel(client)
    where = {"machine": "kbot", "folder": "/src/app"}
    first = cjoin(client, cid, "a", **where)
    # Running at the same time in the same folder: a different name.
    twin = cjoin(client, cid, "b", **where)
    assert twin["handle"] != first["handle"]
    # "a" goes quiet for 10 minutes and a restart takes its name and voice.
    age(first["handle"], timedelta(minutes=11))
    age(twin["handle"], timedelta(seconds=30))
    restart = cjoin(client, channel(client, "after"), "c", **where)
    assert (restart["handle"], restart["voice_id"]) == (first["handle"], first["voice_id"])
    # Another folder, machine or harness doesn't inherit.
    age(restart["handle"], timedelta(minutes=11))
    elsewhere = cjoin(client, cid, "d", machine="kbot", folder="/src/other")
    codex = cjoin(client, cid, "e", agent="Codex", **where)
    assert first["handle"] not in (elsewhere["handle"], codex["handle"])


def test_old_plugins_without_machine_get_no_stickiness(client):
    cid = channel(client)
    first = cjoin(client, cid, "a")
    age(first["handle"], timedelta(minutes=30))
    assert cjoin(client, cid, "b")["handle"] != first["handle"]


# --- TTL --------------------------------------------------------------------


def test_ttl_expiry_frees_a_name(client, monkeypatch):
    cid = channel(client)
    auto = cjoin(client, cid, "a", machine="kbot")
    pin = cjoin(client, cid, "p", handle="Poppy", pin_handle=True, machine="kbot")
    age(auto["handle"], timedelta(hours=25))
    age("Poppy", timedelta(hours=25))
    grab = {"agent": "Codex", "pin_handle": True, "machine": "mac"}
    assert cjoin(client, cid, "x", handle=auto["handle"], **grab)["handle"] == auto["handle"]
    cjoin(client, cid, "y", expect=409, handle="Poppy", **grab)  # pinned: 30 days
    age("Poppy", timedelta(days=31))
    assert cjoin(client, cid, "y", handle="Poppy", **grab)["handle"] == "Poppy"
    assert pin["handle"] == "Poppy"
    # Configurable.
    monkeypatch.setenv("AGENT_PTT_HANDLE_TTL_HOURS", "1")
    short = cjoin(client, cid, "s", machine="kbot")
    assert short["handle_claim"]["expires_after_hours"] == 1
    age(short["handle"], timedelta(hours=2))
    assert cjoin(client, cid, "z", handle=short["handle"], **grab)["handle"] == short["handle"]


def test_signs_of_life_keep_a_claim(client):
    cid = channel(client)
    joined = cjoin(client, cid, "a")
    age(joined["handle"], timedelta(hours=23, minutes=59))
    response = client.post(f"/channels/{cid}/heartbeat", json={"key_id": joined["key_id"]})
    assert response.status_code == 200
    handles.flush_now()
    seen = handles.as_utc(claim_of(joined["handle"]).last_seen)
    assert datetime.now(UTC) - seen < timedelta(minutes=1)


# --- project join -----------------------------------------------------------


def test_project_join_with_session_uses_the_session_name(client):
    joined = cjoin(client, channel(client), "s1", machine="kbot")
    member = pjoin(client, "Stale", session_id="s1", agent="Claude", harness="claude")
    assert member["member"]["handle"] == joined["handle"]
    # A rejoin with a stale cached handle is corrected.
    again = pjoin(client, "Stale", session_id="s1", agent="Claude", member_key=member["member_key"])
    assert again["member"]["handle"] == joined["handle"]
    assert again["member"]["member_id"] == member["member"]["member_id"]
    # A session that never joined a channel gets its wish if free.
    fresh = pjoin(client, "Rex", session_id="s9", agent="Codex", harness="codex")
    assert fresh["member"]["handle"] == "Rex"
    assert claim_of("Rex").session_id == "s9"


def test_project_join_without_session(client):
    first = pjoin(client, "Finn", harness="codex", machine="mac")
    # Already this agent's: rejoin and a second member are fine.
    pjoin(client, "Finn", member_key=first["member_key"])
    pjoin(client, "Finn", harness="codex", machine="mac")
    # Another harness or machine: 409 with the contract body.
    body = pjoin(client, "finn", expect=409, harness="claude", machine="kbot")
    assert body == {
        "detail": "Finn is held by codex on mac",
        "error": "handle_taken",
        "handle": "Finn",
        "held_by": {
            "harness": "codex",
            "machine": "mac",
            "last_seen": body["held_by"]["last_seen"],
        },
    }
    # An older plugin whose session holds the name may send it.
    session = cjoin(client, channel(client), "s1", agent="Claude")
    pjoin(client, session["handle"], harness="claude", machine="kbot")
    pjoin(client, session["handle"], expect=409, harness="codex", machine="kbot")


def test_mentions_reach_the_single_owner(client):
    lead = pjoin(client, "Lead", harness="claude", machine="kbot")
    cid = lead["project"]["channel_id"]
    agent = cjoin(client, cid, "s1", receive_mentions=True)
    member = pjoin(client, session_id="s1", agent="Claude", harness="claude")
    assert member["member"]["handle"] == agent["handle"]
    sent = client.post(
        f"/projects/{lead['project']['project_id']}/items",
        json={"kind": "instruction", "to": f"agent:{agent['handle']}", "body": "hi"},
        headers={"X-Member-Key": lead["member_key"]},
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["to"] == f"agent:{member['member']['member_id']}"


def test_session_adopts_its_members_sessionless_name(client):
    old = pjoin(client, "Theo", harness="cursor", machine="Mac")  # an old plugin
    cid = old["project"]["channel_id"]
    auto = cjoin(client, cid, "new", agent="Cursor", machine="Mac", receive_mentions=True)
    assert auto["handle"] != "Theo"
    upgraded = pjoin(
        client,
        auto["handle"],
        session_id="new",
        agent="Cursor",
        harness="cursor",
        machine="Mac",
        member_key=old["member_key"],
    )
    assert upgraded["member"]["handle"] == "Theo"
    assert claim_of("Theo").session_id == "new"
    assert claim_of(auto["handle"]) is None
    roster = client.get(f"/channels/{cid}").json()["participants"]
    assert [p["handle"] for p in roster.values()] == ["Theo"]
    assert cjoin(client, cid, "new", agent="Cursor", machine="Mac")["handle"] == "Theo"


# --- /handles ---------------------------------------------------------------


def test_handles_endpoints(client, monkeypatch):
    seed_voices()
    cid = channel(client, "lobby")
    joined = cjoin(client, cid, "secret-session", handle="Ember", pin_handle=True, machine="kbot")
    pjoin(client, session_id="secret-session", agent="Claude", harness="claude")
    listed = client.get("/handles").json()["handles"]
    assert [row["handle"] for row in listed] == ["Ember"]
    row = listed[0]
    assert set(row) == {
        "handle",
        "harness",
        "machine",
        "pinned",
        "last_seen",
        "expires_at",
        "channels",
        "projects",
    }
    assert (row["harness"], row["machine"], row["pinned"]) == ("claude", "kbot", True)
    assert row["channels"] == ["lobby"] and row["projects"] == ["repo"]
    text = client.get("/handles").text
    assert "secret-session" not in text and joined["key_id"] not in text

    assert client.delete("/handles/nobody").status_code == 404
    assert client.delete("/handles/ember").json() == {"released": "Ember"}
    assert client.get("/handles").json() == {"handles": []}
    # Anyone may take it now; the old session gets a new name next time.
    other = cjoin(client, cid, "x", agent="Codex", handle="Ember", pin_handle=True, machine="mac")
    assert other["handle"] == "Ember"
    assert cjoin(client, cid, "secret-session", machine="kbot")["handle"] != "Ember"

    monkeypatch.setenv(API_KEY_ENV, "k")
    assert client.get("/handles").status_code == 401
    assert client.delete("/handles/Ember").status_code == 401
    assert client.get("/handles", headers={"X-API-Key": "k"}).status_code == 200


# --- migration --------------------------------------------------------------


def test_migration_keeps_theo_for_cursor_on_the_mac(db_session):
    db = db_session
    now = datetime.now(UTC)
    rooms = {}
    for name in ("agent-ptt", "litium-brain", "krille"):
        chan = ch.create_channel(name, db=db)
        project = ProjectDB(name=name, remote=f"git@x:{name}.git", channel_id=chan.channel_id)
        db.add(project)
        db.flush()
        rooms[name] = (chan.channel_id, project.project_id)

    def member(project, harness, machine, minutes_ago, handle="Theo"):
        row = ProjectMemberDB(
            project_id=rooms[project][1],
            member_key=f"key-{project}-{harness}-{handle}",
            handle=handle,
            harness=harness,
            machine=machine,
            last_seen=now - timedelta(minutes=minutes_ago),
        )
        db.add(row)
        return row

    # The lead's session on kbot holds the identity; seen most recently.
    db.add(SessionIdentityDB(session_id="0a7d0259", agent="Claude", handle="Theo", voice_id="v"))
    key = ParticipantKeyDB(handle="Theo", voice_id="v", channel_id=rooms["agent-ptt"][0])
    db.add(key)
    db.flush()
    db.add(
        MentionReceiverDB(
            key_id=key.key_id,
            recipient_id=handles.recipient_id("Claude", "0a7d0259"),
            channel_id=rooms["agent-ptt"][0],
        )
    )
    lead = member("agent-ptt", "claude", "kbot", 1)
    cursor = member("litium-brain", "cursor", "Mac", 60)
    mac_claude = member("krille", "claude", "Mac", 70)
    stale = member("krille", "codex", "old-box", 60 * 30)  # not seen for 30 h: left alone
    solo = member("litium-brain", "cursor", "Mac", 5, handle="Felix")
    db.commit()
    ch._channels[rooms["agent-ptt"][0]].participants[key.key_id] = ch.ParticipantKey(
        key_id=key.key_id, handle="Theo", voice_id="v", channel_id=rooms["agent-ptt"][0]
    )

    renames = migrate(db)
    assert {r.owner for r in renames} == {"claude/kbot", "claude/Mac"}
    assert all(r.kept_by == "cursor/Mac" and r.old == "Theo" for r in renames)
    db.expire_all()
    theo = db.get(HandleClaimDB, "theo")
    assert theo.owner_id == handles.host_owner("cursor", "Mac")
    assert db.get(ProjectMemberDB, cursor.member_id).handle == "Theo"
    lead_name = db.get(ProjectMemberDB, lead.member_id).handle
    mac_name = db.get(ProjectMemberDB, mac_claude.member_id).handle
    assert len({"Theo", lead_name, mac_name}) == 3
    assert db.get(SessionIdentityDB, ("0a7d0259", "Claude")).handle == lead_name
    assert db.get(ParticipantKeyDB, key.key_id).handle == lead_name
    assert ch._channels[rooms["agent-ptt"][0]].participants[key.key_id].handle == lead_name
    assert db.get(HandleClaimDB, lead_name.casefold()).session_id == "0a7d0259"
    assert db.get(ProjectMemberDB, stale.member_id).handle == "Theo"
    assert db.get(ProjectMemberDB, solo.member_id).handle == "Felix"
    notices = {
        (m.channel_id, m.text)
        for m in db.query(MessageDB).filter_by(kind="notice")
        if db.get(ChannelDB, m.channel_id) is not None
    }
    assert (
        rooms["agent-ptt"][0],
        f"@Theo (cursor/Mac) keeps Theo; @{lead_name} is now claude/kbot (was Theo)",
    ) in notices
    assert (
        rooms["krille"][0],
        f"@Theo (cursor/Mac) keeps Theo; @{mac_name} is now claude/Mac (was Theo)",
    ) in notices
    # Idempotent: a second run (any later startup) changes nothing.
    assert migrate(db) == []


def test_migration_without_duplicates_only_builds_claims(db_session):
    db = db_session
    db.add(SessionIdentityDB(session_id="s", agent="Codex", handle="Ada", voice_id="v"))
    db.commit()
    assert migrate(db) == []
    claim = db.get(HandleClaimDB, "ada")
    assert claim.owner_id == handles.session_owner("Codex", "s")
