"""Token usage: PATCH members/me usage, daily rollups, folding, endpoints, pricing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_ptt import pricing, usage
from agent_ptt import projects as pj
from agent_ptt.db import SessionLocal
from agent_ptt.models import MemberUsageDB, MemberUsageSnapshotDB
from tests.test_projects import hdr, join, pid

OPUS = "claude-opus-5-5"


@pytest.fixture(autouse=True)
def _reset():
    pj.reset_runtime_state()
    yield
    pj.reset_runtime_state()


@pytest.fixture
def day(monkeypatch):
    """Pin the server's local day; ``day.set("2026-11-20")`` moves it."""

    class Day:
        value = "2026-11-19"

        def set(self, value):
            self.value = value

    pinned = Day()
    monkeypatch.setattr(usage, "local_day", lambda now=None: pinned.value)
    return pinned


def counts(input=0, cache_read=0, cache_write=0, output=0, reasoning=0):
    return {
        "input": input,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "output": output,
        "reasoning": reasoning,
    }


def report(client, joined, session="s1", by_model=None, expect=200, **extra):
    body = {"usage": {"session_id": session, "harness": "claude", "by_model": by_model or {}}}
    body["usage"].update(extra)
    response = client.patch(f"/projects/{pid(joined)}/members/me", json=body, headers=hdr(joined))
    assert response.status_code == expect, response.text
    return response.json()


def project_usage(client, joined, **params):
    response = client.get(f"/projects/{pid(joined)}/usage", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def mine(payload, joined):
    member_id = joined["member"]["member_id"]
    return next(m for m in payload["members"] if m["member_id"] == member_id)


# --------------------------------------------------------------------------
# Delta / reset logic
# --------------------------------------------------------------------------


def test_cumulative_reports_are_idempotent(client, day):
    ada = join(client, "Ada", harness="claude", machine="kbot")
    report(client, ada, by_model={OPUS: counts(10, 1000, 50, 200, 30)})
    report(client, ada, by_model={OPUS: counts(10, 1000, 50, 200, 30)})  # replay
    entry = report(client, ada, by_model={OPUS: counts(15, 3000, 60, 300, 40)})
    total = entry["usage"]["total"]
    assert total["input"] == 15 and total["cache_read"] == 3000 and total["output"] == 300
    assert total["reasoning"] == 40
    # Headline leaves cache reads out (they dominate) and reasoning (inside output).
    assert total["headline"] == 15 + 60 + 300
    assert entry["usage"]["session"] == total == entry["usage"]["today"]


def test_lower_total_is_a_reset_and_never_negative(client, day):
    ada = join(client, "Ada", harness="claude")
    report(client, ada, by_model={OPUS: counts(100, 0, 0, 100)})
    entry = report(client, ada, by_model={OPUS: counts(5, 0, 0, 5)})  # counter restarted
    assert entry["usage"]["total"]["headline"] == 210
    entry = report(client, ada, by_model={OPUS: counts(8, 0, 0, 9)})
    assert entry["usage"]["total"]["headline"] == 217
    # One field dropping while the total grows adds nothing negative.
    entry = report(client, ada, by_model={OPUS: counts(2, 0, 0, 30)})
    assert entry["usage"]["total"]["input"] == 108
    assert entry["usage"]["total"]["output"] == 130


def test_days_sessions_and_models_split(client, day):
    ada = join(client, "Ada", harness="claude")
    report(client, ada, by_model={OPUS: counts(output=100)})
    day.set("2026-11-20")
    report(client, ada, by_model={OPUS: counts(output=150), "claude-haiku-5-5": counts(input=7)})
    entry = report(client, ada, session="s2", by_model={OPUS: counts(output=20)})
    u = entry["usage"]
    assert u["total"]["headline"] == 177
    assert u["today"]["headline"] == 77  # 50 + 7 from s1 today, 20 from s2
    assert u["session"]["headline"] == 20  # s2 is the latest session
    assert set(u["by_model"]) == {OPUS, "claude-haiku-5-5"}
    assert u["by_model"][OPUS]["output"] == 170


def test_new_session_for_same_member_adds_up(client, day):
    ada = join(client, "Ada", harness="codex")
    report(client, ada, session="a", by_model={"gpt-6-astra": counts(output=10)})
    report(client, ada, session="b", by_model={"gpt-6-astra": counts(output=10)})
    payload = project_usage(client, ada)
    assert payload["total"]["output"] == 20
    assert mine(payload, ada)["usage"]["session_id"] == "b"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"session_id": "s", "by_model": {OPUS: {"input": -1}}},
        {"session_id": "s", "by_model": {OPUS: {"input": 10**14}}},
        {"session_id": "s", "by_model": {"has spaces and prose": {"input": 1}}},
        {"session_id": "s", "by_model": {f"m{i}": {} for i in range(21)}},
        {"session_id": "", "by_model": {}},
        {"session_id": "s", "by_model": {OPUS: {"input": "lots"}}},
    ],
)
def test_usage_is_validated(client, bad):
    ada = join(client, "Ada")
    response = client.patch(
        f"/projects/{pid(ada)}/members/me", json={"usage": bad}, headers=hdr(ada)
    )
    assert response.status_code == 422


def test_unknown_fields_are_dropped_not_stored(client, day):
    ada = join(client, "Ada", harness="cursor")
    entry = report(
        client,
        ada,
        by_model={"some-model": {"input": 3, "text": "the reply"}},
        text="prompt text",
    )
    assert entry["usage"]["total"]["input"] == 3
    assert "text" not in str(entry)


def test_usage_alongside_doing_and_counts_as_seen(client, day):
    ada = join(client, "Ada", harness="claude")
    response = client.patch(
        f"/projects/{pid(ada)}/members/me",
        json={"doing": "testing", "usage": {"session_id": "s", "by_model": {OPUS: counts(1)}}},
        headers=hdr(ada),
    )
    assert response.status_code == 200
    assert response.json()["doing"] == "testing"
    assert response.json()["usage"]["total"]["input"] == 1


# --------------------------------------------------------------------------
# Limits, roster, endpoints
# --------------------------------------------------------------------------


def limits(at, pct=41, plan=None, source="claude-statusline"):
    return {
        "source": source,
        "plan": plan,
        "windows": [
            {"name": "5h", "used_pct": pct, "resets_at": "2026-11-19T14:00:00Z"},
            {"name": "7d", "used_pct": 140},
        ],
        "reported_at": at,
    }


def test_limits_are_per_machine_and_harness_and_newest_wins(client, day):
    now = datetime.now(UTC)
    ada = join(client, "Ada", harness="claude", machine="kbot")
    bo = join(client, "Bo", harness="claude", machine="kbot")
    cy = join(client, "Cy", harness="claude", machine="laptop")
    report(client, ada, limits=limits((now - timedelta(minutes=5)).isoformat(), pct=41))
    # An older report arriving late doesn't overwrite.
    report(client, bo, limits=limits((now - timedelta(minutes=50)).isoformat(), pct=10))
    entry = report(client, bo)
    assert entry["limits"]["windows"][0]["used_pct"] == 41
    assert entry["limits"]["windows"][1]["used_pct"] == 100  # clamped
    assert entry["limits"]["machine"] == "kbot"
    entry = report(client, cy)
    assert entry["limits"] is None  # nothing reported for laptop yet

    payload = client.get("/usage").json()
    assert len(payload["limits"]) == 1
    assert payload["limits"][0]["reported_by"]["handle"] == "Ada"
    assert len(project_usage(client, ada)["limits"]) == 1


def test_roster_carries_usage_and_limits(client, day):
    ada = join(client, "Ada", harness="claude", machine="kbot")
    join(client, "Quiet", harness="agy")
    report(
        client,
        ada,
        by_model={OPUS: counts(2, 48653, 1485, 240)},
        limits=limits(datetime.now(UTC).isoformat()),
    )
    roster = client.get(f"/projects/{pid(ada)}").json()["members"]
    by_handle = {m["handle"]: m for m in roster}
    u = by_handle["Ada"]["usage"]
    assert u["total"]["headline"] == 2 + 1485 + 240
    assert u["by_model"][OPUS]["cache_read"] == 48653
    assert "est_usd" not in u["total"]
    assert by_handle["Ada"]["limits"]["source"] == "claude-statusline"
    assert by_handle["Quiet"]["usage"] is None and by_handle["Quiet"]["limits"] is None
    assert "member_key" not in str(roster)


def test_project_and_server_totals(client, day):
    ada = join(client, "Ada", harness="claude")
    bo = join(client, "Bo", harness="codex")
    other = join(client, "Cy", harness="claude", remote="git@github.com:Owner/Other.git")
    report(client, ada, by_model={OPUS: counts(output=100)})
    report(client, bo, by_model={"gpt-6-astra": counts(output=50)}, harness="codex")
    report(client, other, by_model={OPUS: counts(output=7)})
    day.set("2026-11-20")
    report(client, ada, by_model={OPUS: counts(output=110)})

    payload = project_usage(client, ada)
    assert payload["today"]["output"] == 10
    assert payload["total"]["output"] == 160
    assert {m["handle"] for m in payload["members"]} == {"Ada", "Bo"}

    server = client.get("/usage").json()
    assert server["total"]["output"] == 167 and server["today"]["output"] == 10
    assert {p["name"]: p["total"]["output"] for p in server["projects"]} == {
        "repo": 160,
        "other": 7,
    }
    assert {h["harness"]: h["total"]["output"] for h in server["harnesses"]} == {
        "claude": 117,
        "codex": 50,
    }


def test_empty_usage_endpoints(client):
    ada = join(client, "Ada")
    payload = project_usage(client, ada)
    assert payload["total"]["headline"] == 0 and payload["members"][0]["usage"] is None
    server = client.get("/usage").json()
    assert server["projects"] == [] and server["limits"] == []
    assert client.get("/projects/nope/usage").status_code == 404


def test_usage_endpoints_need_the_api_key(client, monkeypatch):
    monkeypatch.setenv("AGENT_PTT_API_KEY", "secret")
    assert client.get("/usage").status_code == 401
    assert client.get("/usage", headers={"X-API-Key": "secret"}).status_code == 200


# --------------------------------------------------------------------------
# Folding
# --------------------------------------------------------------------------


def test_old_session_rows_fold_into_daily_rows(client, day):
    ada = join(client, "Ada", harness="claude")
    day.set("2026-09-01")
    report(client, ada, session="old1", by_model={OPUS: counts(output=10)})
    report(client, ada, session="old2", by_model={OPUS: counts(output=5)})
    day.set("2026-11-19")
    pj.reset_runtime_state()  # the fold runs once per day
    report(client, ada, session="new", by_model={OPUS: counts(output=1)})
    with SessionLocal() as db:
        rows = db.query(MemberUsageDB).order_by(MemberUsageDB.day).all()
        assert [(r.session_id, r.day, r.output) for r in rows] == [
            ("", "2026-09-01", 15),
            ("new", "2026-11-19", 1),
        ]
        # Snapshots stay, so resuming an old session doesn't count it twice.
        assert db.query(MemberUsageSnapshotDB).count() == 3
    entry = report(client, ada, session="old1", by_model={OPUS: counts(output=12)})
    assert entry["usage"]["total"]["output"] == 18


def test_fold_runs_at_most_once_a_day(day):
    assert usage.maybe_fold(SessionLocal()) == 0
    with SessionLocal() as db:
        db.add(
            MemberUsageDB(
                project_id="p", member_id="m", session_id="s", model=OPUS, day="2026-01-01"
            )
        )
        db.commit()
        assert usage.maybe_fold(db) == 0  # already ran today
        usage.reset_runtime_state()
        assert usage.maybe_fold(db) == 1


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


def test_price_lookup_and_estimate():
    assert pricing.price_for(OPUS)["input"] == 4.0
    assert pricing.price_for("claude-opus-5-5[1m]") is pricing.price_for(OPUS)
    assert pricing.price_for("claude-sonnet-5-20261001") is pricing.price_for("claude-sonnet-5")
    assert pricing.price_for("claude-opus-5-6") is None
    assert pricing.estimate_usd("mystery", counts(1_000_000)) is None
    # 1M input at $4 + 1M cache read at $0.20 + 1M output at $20; reasoning not extra.
    usd = pricing.estimate_usd(OPUS, counts(10**6, 10**6, 0, 10**6, 10**6))
    assert usd == pytest.approx(24.2)


def test_estimate_only_when_asked(client, day):
    ada = join(client, "Ada", harness="claude")
    report(client, ada, by_model={OPUS: counts(output=10**6), "mystery-model": counts(output=5)})
    assert "est_usd" not in project_usage(client, ada)["total"]
    payload = project_usage(client, ada, estimate=1)
    assert payload["total"]["est_usd"] == pytest.approx(20.0)
    by_model = mine(payload, ada)["usage"]["by_model"]
    assert by_model["mystery-model"]["total"]["est_usd"] is None
    roster = client.get(f"/projects/{pid(ada)}", params={"estimate": 1}).json()["members"]
    assert roster[0]["usage"]["total"]["est_usd"] == pytest.approx(20.0)
    assert client.get("/usage", params={"estimate": 1}).json()["total"]["est_usd"] == 20.0


def test_usage_script_is_served_and_loaded(client):
    assert client.get("/ui/usage.js").status_code == 200
    assert 'src="usage.js"' in client.get("/ui/").text
    assert 'src="usage.js"' in client.get("/ui/board.html").text
