"""Plugin update nudges (agent_ptt/plugin_updates.py, POST /announce-update)."""

from __future__ import annotations

import json

import pytest

from agent_ptt import plugin_updates as pu
from agent_ptt import projects as pj
from agent_ptt.db import SessionLocal
from agent_ptt.models import ProjectMemberDB
from tests.test_projects import hdr, inbox, join, pid


@pytest.fixture(autouse=True)
def _latest(monkeypatch):
    pj.reset_runtime_state()
    pu.reset()
    monkeypatch.setenv(pu.ENV, "claude=0.6.2,codex=0.6.2,antigravity=0.6.2,cursor=0.6.2")
    yield
    pu.reset()
    pj.reset_runtime_state()


def nudges(client, joined):
    return [
        item
        for item in inbox(client, joined)["items"]
        if (item.get("context") or {}).get("kind") == pu.CONTEXT_KIND
    ]


def patch(client, joined, **body):
    response = client.patch(
        f"/projects/{pid(joined)}/members/me", json=body or {"doing": "x"}, headers=hdr(joined)
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# Latest versions
# --------------------------------------------------------------------------


def test_manifests_give_every_harness(tmp_path):
    for harness, parts in pu.MANIFESTS.items():
        path = tmp_path.joinpath(*parts)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"name": "x", "version": f"1.{len(harness)}.0"}))
    assert pu.read_manifests(tmp_path) == {
        "claude": "1.6.0",
        "codex": "1.5.0",
        "antigravity": "1.11.0",
        "cursor": "1.6.0",
    }
    tmp_path.joinpath(*pu.MANIFESTS["codex"]).write_text("{not json")
    assert "codex" not in pu.read_manifests(tmp_path)


def test_this_checkout_manifests_agree():
    found = pu.read_manifests()
    assert set(found) == set(pu.HARNESSES)
    assert len(set(found.values())) == 1  # released together (validate_plugins.py)


def test_env_override_forms(monkeypatch):
    manifests = pu.read_manifests()
    monkeypatch.delenv(pu.ENV)
    assert pu.latest_versions() == manifests
    assert pu.source() == "manifests"
    monkeypatch.setenv(pu.ENV, "9.0.0")
    assert pu.latest_versions() == dict.fromkeys(pu.HARNESSES, "9.0.0")
    assert pu.source() == "env"
    monkeypatch.setenv(pu.ENV, "claude=9.1.0, agy=9.2.0, bogus=1.0.0, codex=junk")
    latest = pu.latest_versions()
    assert latest["claude"] == "9.1.0"
    assert latest["antigravity"] == "9.2.0"
    assert latest["codex"] == manifests["codex"]  # unparsable part ignored
    assert "bogus" not in latest
    monkeypatch.setenv(pu.ENV, "off")
    assert pu.latest_versions() == {}
    assert pu.source() == "off"
    monkeypatch.setenv(pu.ENV, "garbage")
    assert pu.latest_versions() == manifests


def test_hosted_has_no_latest(monkeypatch):
    monkeypatch.setenv("AGENT_PTT_HOSTED", "1")
    assert pu.latest_versions() == {}
    assert pu.source() == "hosted"


def test_version_parsing_and_comparison():
    assert pu.parse_version("0.6.2") == (0, 6, 2)
    assert pu.parse_version("v1.2") == (1, 2, 0)
    assert pu.parse_version("0.10.0-dev") == (0, 10, 0)
    assert pu.parse_version("") is None
    assert pu.parse_version("dev") is None
    latest = {"claude": "0.10.0"}
    member = ProjectMemberDB(harness="claude", plugin_version="0.9.9")
    assert pu.outdated(member, latest) == "0.10.0"  # numeric, not string order
    member.plugin_version = "0.10.0"
    assert pu.outdated(member, latest) is None
    member.plugin_version = "0.11.0"
    assert pu.outdated(member, latest) is None  # ahead (a dev checkout)
    member.harness = "Agy"
    member.plugin_version = "0.1.0"
    assert pu.outdated(member, {"antigravity": "0.2.0"}) == "0.2.0"
    member.harness = "gemini"
    assert pu.outdated(member, {"antigravity": "0.2.0"}) is None


def test_plugins_latest_route(client, monkeypatch):
    body = client.get("/plugins/latest").json()
    assert body["latest"]["claude"] == "0.6.2"
    assert body["source"] == "env"
    monkeypatch.setenv("AGENT_PTT_HOSTED", "1")
    assert client.get("/plugins/latest").status_code == 404


# --------------------------------------------------------------------------
# The nudge
# --------------------------------------------------------------------------


def test_outdated_join_gets_one_private_fyi(client):
    old = join(client, "Ada", harness="claude", plugin_version="0.5.6")
    other = join(client, "Bob", harness="claude", plugin_version="0.6.2")
    [item] = nudges(client, old)
    assert item["fyi"] is True
    assert item["kind"] == "note"
    assert item["to"] == f"agent:{old['member']['member_id']}"
    assert item["from"] == {
        "member_id": "server",
        "handle": "agent-ptt",
        "harness": None,
        "machine": None,
        "doing": None,
    }
    assert item["context"] == {
        "kind": "plugin-update",
        "harness": "claude",
        "version": "0.5.6",
        "latest": "0.6.2",
    }
    assert "Your Agent PTT plugin is 0.5.6; the latest is 0.6.2." in item["body"]
    assert len(item["body"]) < 1500  # old hooks cut item bodies at 1500 chars
    assert nudges(client, other) == []
    # Private: no channel notice, and the Board leaves it out.
    assert all(i["item_id"] != item["item_id"] for i in client.get("/work-items").json()["items"])
    assert old["member"]["outdated"] is True
    assert old["member"]["plugin_latest"] == "0.6.2"
    assert other["member"]["outdated"] is False


def test_no_notice_in_the_channel(client):
    old = join(client, "Ada", harness="claude", plugin_version="0.5.6")
    channel_id = old["project"]["channel_id"]
    texts = [m["text"] for m in client.get(f"/channels/{channel_id}/history").json()]
    assert not any("plugin" in t.lower() for t in texts)


def test_current_or_unknown_versions_get_nothing(client):
    for handle, extra in {
        "Cur": {"harness": "claude", "plugin_version": "0.6.2"},
        "New": {"harness": "codex", "plugin_version": "0.7.0"},
        "Unk": {"harness": "claude"},
        "Odd": {"harness": "claude", "plugin_version": "dev"},
        "Hum": {"plugin_version": "0.1.0"},  # no harness: not a plugin we know
    }.items():
        assert nudges(client, join(client, handle, **extra)) == [], handle


def test_once_per_member_per_latest_version(client, monkeypatch):
    ada = join(client, "Ada", harness="claude", plugin_version="0.5.6")
    [first] = nudges(client, ada)
    rejoined = client.post(
        "/projects/join",
        json={"project_id": pid(ada), "handle": "Ada", "member_key": ada["member_key"]},
    ).json()
    patch(client, ada)
    patch(client, ada)
    assert [i["item_id"] for i in nudges(client, rejoined)] == [first["item_id"]]
    monkeypatch.setenv(pu.ENV, "0.6.3")  # a newer latest nudges again
    patch(client, ada)
    items = nudges(client, ada)
    assert [i["context"]["latest"] for i in items] == ["0.6.2", "0.6.3"]
    patch(client, ada)
    assert len(nudges(client, ada)) == 2


def test_patch_nudges_a_member_that_joined_before(client, monkeypatch):
    monkeypatch.setenv(pu.ENV, "off")
    ada = join(client, "Ada", harness="codex", plugin_version="0.6.0")
    assert nudges(client, ada) == []
    monkeypatch.setenv(pu.ENV, "0.6.2")
    patch(client, ada, doing="reading code")
    [item] = nudges(client, ada)
    assert "codex" in item["body"]
    with SessionLocal() as db:
        assert db.get(ProjectMemberDB, ada["member"]["member_id"]).update_nudged == "0.6.2"


def test_rejoin_with_the_new_version_is_not_nudged(client, monkeypatch):
    monkeypatch.setenv(pu.ENV, "off")
    ada = join(client, "Ada", harness="claude", plugin_version="0.5.6")
    monkeypatch.setenv(pu.ENV, "0.6.2")
    client.post(
        "/projects/join",
        json={
            "project_id": pid(ada),
            "handle": "Ada",
            "member_key": ada["member_key"],
            "plugin_version": "0.6.2",
        },
    )
    assert nudges(client, ada) == []


def test_read_nudge_closes_on_ack(client):
    ada = join(client, "Ada", harness="claude", plugin_version="0.5.6")
    [item] = nudges(client, ada)
    response = client.post(f"/projects/{pid(ada)}/items/{item['item_id']}/ack", headers=hdr(ada))
    assert response.status_code == 200
    assert (
        client.get(f"/projects/{pid(ada)}/items/{item['item_id']}").json()["item"]["state"]
        == "done"
    )


def test_wake_only_inbox_skips_the_fyi(client):
    ada = join(client, "Ada", harness="claude", plugin_version="0.5.6")
    assert inbox(client, ada, wake_only=1)["items"] == []
    assert len(nudges(client, ada)) == 1  # hooks read without wake_only


@pytest.mark.parametrize(
    ("harness", "expected"),
    [
        ("claude", "claude plugin marketplace update agent-ptt"),
        ("codex", "codex plugin marketplace upgrade agent-ptt"),
        ("antigravity", "agy plugin install"),
        ("agy", "agy plugin install"),
    ],
)
def test_text_carries_the_command_for_the_harness(harness, expected):
    text = pu.nudge_text(harness, "0.5.6", "0.6.2")
    assert text.startswith("Your Agent PTT plugin is 0.5.6; the latest is 0.6.2.")
    assert f"`{pu.UPDATE_COMMANDS[pu.harness_key(harness)]}`" in text
    assert expected in text
    assert "Update it without asking" in text
    assert "tell the user once, in one line" in text
    assert "don't repeat it every turn" in text
    for other, command in pu.UPDATE_COMMANDS.items():
        if other != pu.harness_key(harness):
            assert command not in text


def test_claude_text_updates_both_plugins():
    text = pu.nudge_text("claude", "0.5.6", "0.6.2")
    assert "claude plugin update agent-ptt-announcer@agent-ptt" in text
    assert "agent-ptt-voice@agent-ptt" in text
    assert "restart this session" in text


def test_cursor_text_pulls_the_clone_or_asks_the_user():
    text = pu.nudge_text("cursor", "0.5.6", "0.6.2")
    assert "git -C <your agent-ptt clone> pull --ff-only" in text
    assert "--plugin-dir" in text
    assert "Otherwise tell the user once" in text


def test_checkout_installs_get_the_git_pull_form():
    assert "pull --ff-only` instead" in pu.nudge_text("claude", "0.6.0", "0.6.2")
    codex = pu.nudge_text("codex", "0.6.0", "0.6.2")
    assert "codex plugin add agent-ptt-announcer@agent-ptt" in codex
    assert "not a Git marketplace" in codex


@pytest.mark.parametrize("harness", ["claude", "codex", "antigravity", "cursor"])
def test_text_fits_old_hook_lines(harness):
    assert len(pu.nudge_text(harness, "0.5.6", "0.6.2")) < 1000


# --------------------------------------------------------------------------
# POST /announce-update
# --------------------------------------------------------------------------


def _setup_members(client, monkeypatch):
    monkeypatch.setenv(pu.ENV, "off")  # joined before the server knew the latest
    ada = join(client, "Ada", harness="claude", plugin_version="0.5.6", machine="mac")
    bob = join(client, "Bob", harness="codex", plugin_version="0.6.0", remote="git@x:o/other")
    cur = join(client, "Cur", harness="claude", plugin_version="0.6.2")
    gone = join(client, "Gus", harness="claude", plugin_version="0.5.1")
    with SessionLocal() as db:
        db.get(ProjectMemberDB, gone["member"]["member_id"]).gone_at = pj.now_utc()
        db.commit()
    monkeypatch.setenv(pu.ENV, "0.6.2")
    return ada, bob, cur, gone


def test_announce_dry_run_lists_without_sending(client, monkeypatch):
    ada, bob, cur, _ = _setup_members(client, monkeypatch)
    response = client.post("/announce-update?dry_run=1")
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["total"] == 2
    assert {(m["handle"], m["plugin_version"], m["latest"]) for m in body["members"]} == {
        ("Ada", "0.5.6", "0.6.2"),
        ("Bob", "0.6.0", "0.6.2"),
    }
    ada_row = next(m for m in body["members"] if m["handle"] == "Ada")
    assert ada_row["machine"] == "mac" and ada_row["harness"] == "claude"
    assert sorted(p["nudged"] for p in body["projects"]) == [1, 1]
    raw = response.text
    for joined in (ada, bob, cur):
        assert joined["member_key"] not in raw
    assert "member_key" not in raw
    assert nudges(client, ada) == []


def test_announce_sends_once_and_force_resends(client, monkeypatch):
    ada, bob, cur, gone = _setup_members(client, monkeypatch)
    body = client.post("/announce-update").json()
    assert body["total"] == 2
    assert "members" not in body and "woken" not in body
    assert {p["project"]: p["nudged"] for p in body["projects"]} == {"repo": 1, "other": 1}
    assert len(nudges(client, ada)) == 1
    assert len(nudges(client, bob)) == 1
    assert nudges(client, cur) == []
    assert client.post("/announce-update").json()["total"] == 0
    patch(client, ada)  # already told about 0.6.2
    assert len(nudges(client, ada)) == 1
    forced = client.post("/announce-update", json={"force": True}).json()
    assert forced["total"] == 2 and forced["force"] is True
    assert len(nudges(client, ada)) == 2
    with SessionLocal() as db:
        assert db.get(ProjectMemberDB, gone["member"]["member_id"]).update_nudged is None


def test_announce_off_nudges_nobody(client, monkeypatch):
    _setup_members(client, monkeypatch)
    monkeypatch.setenv(pu.ENV, "off")
    body = client.post("/announce-update").json()
    assert body == {
        "latest": {},
        "source": "off",
        "dry_run": False,
        "force": False,
        "total": 0,
        "projects": [],
    }


def test_announce_needs_the_api_key_and_404s_hosted(client, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_API_KEY", "secret")
    assert client.post("/announce-update").status_code == 401
    assert client.get("/plugins/latest").status_code == 401
    ok = client.post("/announce-update?dry_run=1", headers={"X-API-Key": "secret"})
    assert ok.status_code == 200
    monkeypatch.setenv("AGENT_PTT_HOSTED", "1")
    assert client.post("/announce-update", headers={"X-API-Key": "secret"}).status_code == 404


# --------------------------------------------------------------------------
# Roster data the web UI polls
# --------------------------------------------------------------------------


def test_usage_and_roster_carry_the_outdated_flag(client):
    ada = join(client, "Ada", harness="claude", plugin_version="0.5.6")
    join(client, "Bob", harness="claude", plugin_version="0.6.2")
    usage = client.get(f"/projects/{pid(ada)}/usage").json()
    rows = {m["handle"]: m for m in usage["members"]}
    assert rows["Ada"]["outdated"] is True
    assert rows["Ada"]["plugin_latest"] == "0.6.2"
    assert rows["Ada"]["plugin_version"] == "0.5.6"
    assert rows["Bob"]["outdated"] is False and rows["Bob"]["plugin_latest"] is None
    detail = client.get(f"/projects/{pid(ada)}").json()
    flags = {m["handle"]: m["outdated"] for m in detail["members"]}
    assert flags == {"Ada": True, "Bob": False}


def test_web_ui_ships_the_update_dialog(client):
    assert client.get("/ui/updates.js").status_code == 200
    page = client.get("/ui/").text
    assert 'src="updates.js"' in page
    assert 'id="updateAgentsBtn"' in page and 'id="updateDialog"' in page
