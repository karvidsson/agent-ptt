"""Paged channel history (task 46): limit/before/has_more, the unchanged default, the index."""

from datetime import UTC, datetime

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from agent_ptt import channel as ch
from agent_ptt import db
from agent_ptt.models import Message, MessageDB


def _channel_with(client, count: int, same_timestamp: bool = False) -> tuple[str, list[str]]:
    """A channel holding ``count`` messages, oldest first; returns (channel_id, message_ids)."""
    cid = client.post("/channels", json={"name": "Long"}).json()["channel_id"]
    channel = ch.get_channel(cid)
    stamp = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    for i in range(count):
        ts = stamp if same_timestamp else stamp.replace(minute=i % 60, hour=12 + i // 60)
        channel.messages.append(
            Message(channel_id=cid, sender_key="k", handle="Ada", text=f"m{i}", timestamp=ts)
        )
    return cid, [m.message_id for m in channel.messages]


def test_default_history_is_still_the_whole_list(client):
    cid, ids = _channel_with(client, 7)
    body = client.get(f"/channels/{cid}/history").json()
    assert isinstance(body, list)
    assert [m["message_id"] for m in body] == ids
    assert {"has_context", "context_summary", "deliveries", "sender_id"} <= set(body[0])
    assert "context" not in body[0]


def test_limit_returns_the_latest_page_with_has_more(client):
    cid, ids = _channel_with(client, 7)
    page = client.get(f"/channels/{cid}/history", params={"limit": 3}).json()
    assert [m["message_id"] for m in page["messages"]] == ids[-3:]
    assert page["has_more"] is True
    whole = client.get(f"/channels/{cid}/history", params={"limit": 7}).json()
    assert [m["message_id"] for m in whole["messages"]] == ids
    assert whole["has_more"] is False


def test_before_pages_back_to_the_start(client):
    cid, ids = _channel_with(client, 7)
    seen: list[str] = []
    page = client.get(f"/channels/{cid}/history", params={"limit": 3}).json()
    while True:
        seen = [m["message_id"] for m in page["messages"]] + seen
        if not page["has_more"]:
            break
        page = client.get(f"/channels/{cid}/history", params={"limit": 3, "before": seen[0]}).json()
    assert seen == ids  # nothing missing, nothing twice, in order
    # before without limit: everything older than the cursor.
    older = client.get(f"/channels/{cid}/history", params={"before": ids[2]}).json()
    assert [m["message_id"] for m in older["messages"]] == ids[:2]
    assert older["has_more"] is False
    first = client.get(f"/channels/{cid}/history", params={"before": ids[0]}).json()
    assert first == {"messages": [], "has_more": False}


def test_equal_timestamps_keep_a_stable_order_across_pages(client):
    cid, ids = _channel_with(client, 10, same_timestamp=True)
    latest = client.get(f"/channels/{cid}/history", params={"limit": 4}).json()
    older = client.get(
        f"/channels/{cid}/history",
        params={"limit": 4, "before": latest["messages"][0]["message_id"]},
    ).json()
    paged = [m["message_id"] for m in older["messages"] + latest["messages"]]
    assert paged == ids[-8:]
    assert len(set(paged)) == 8


def test_unknown_cursor_is_404_and_bad_limit_is_422(client):
    cid, _ = _channel_with(client, 3)
    _, other_ids = _channel_with(client, 2)
    assert client.get(f"/channels/{cid}/history", params={"before": "nope"}).status_code == 404
    # A message of another channel is not a cursor here.
    resp = client.get(f"/channels/{cid}/history", params={"before": other_ids[0]})
    assert resp.status_code == 404
    assert client.get(f"/channels/{cid}/history", params={"limit": 0}).status_code == 422
    assert client.get(f"/channels/{cid}/history", params={"limit": 5000}).status_code == 422


def test_paged_history_carries_receipts_and_omits_context(client):
    cid = client.post("/channels", json={"name": "Receipts"}).json()["channel_id"]
    ada = client.post(f"/channels/{cid}/join", json={"handle": "Ada"}).json()["key_id"]
    client.post(f"/channels/{cid}/join", json={"handle": "Bo"})
    sent = client.post(
        f"/channels/{cid}/say",
        json={
            "key_id": ada,
            "text": "@Bo look",
            "speak": False,
            "context": {"repo": "agent-ptt", "files": [{"path": "a.py", "op": "edit"}]},
        },
    ).json()
    page = client.get(f"/channels/{cid}/history", params={"limit": 10}).json()
    msg = next(m for m in page["messages"] if m["message_id"] == sent["message_id"])
    assert [d["handle"] for d in msg["deliveries"]] == ["Bo"]
    assert msg["has_context"] is True and "context" not in msg
    full = client.get(f"/channels/{cid}/history", params={"limit": 10, "with_context": 1}).json()
    assert next(m for m in full["messages"] if m["has_context"])["context"]["repo"] == "agent-ptt"


def test_channel_reads_can_leave_out_the_transcript(client):
    cid, _ = _channel_with(client, 4)
    detail = client.get(f"/channels/{cid}").json()
    assert len(detail["messages"]) == 4 and detail["message_count"] == 4  # default unchanged
    light = client.get(f"/channels/{cid}", params={"with_messages": 0}).json()
    assert "messages" not in light and light["message_count"] == 4
    listed = client.get("/channels", params={"with_messages": 0}).json()
    assert [(c["channel_id"], c["message_count"]) for c in listed] == [(cid, 4)]
    assert all("messages" not in c for c in listed)
    assert len(client.get("/channels").json()[0]["messages"]) == 4


def test_restore_orders_equal_timestamps_deterministically(client):
    cid = client.post("/channels", json={"name": "Ties"}).json()["channel_id"]
    stamp = datetime(2026, 11, 20, 12, 0)
    with db.SessionLocal() as session:
        for mid in ("m-c", "m-a", "m-b"):
            session.add(
                MessageDB(
                    message_id=mid,
                    channel_id=cid,
                    sender_key="k",
                    handle="Ada",
                    text=mid,
                    timestamp=stamp,
                )
            )
        session.commit()
    ch._channels.clear()
    with db.SessionLocal() as session:
        ch.restore_channels(session)
    assert [m.message_id for m in ch.get_channel(cid).messages] == ["m-a", "m-b", "m-c"]


def test_init_db_adds_the_history_index_to_an_old_database(tmp_path, monkeypatch):
    legacy = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with legacy.begin() as connection:
        connection.execute(
            text("""
            CREATE TABLE messages (
                message_id VARCHAR PRIMARY KEY, channel_id VARCHAR,
                sender_key VARCHAR, handle VARCHAR, text TEXT, timestamp DATETIME
            )
        """)
        )
    with monkeypatch.context() as patch:
        patch.setattr(db, "engine", legacy)
        patch.setattr(db, "SessionLocal", sessionmaker(bind=legacy, class_=Session))
        db.init_db()
        db.init_db()  # idempotent
    indexes = {ix["name"]: ix["column_names"] for ix in inspect(legacy).get_indexes("messages")}
    assert indexes["ix_messages_channel_timestamp"] == ["channel_id", "timestamp"]
    legacy.dispose()


def test_cli_history_limit_asks_the_server_for_one_page(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    import agent_ptt.cli as cli

    monkeypatch.setattr(cli, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(cli, "SESSION_FILE", tmp_path / "session.json")
    entry = {
        "handle": "Ada",
        "text": "latest",
        "timestamp": "2026-11-20T12:00:00",
        "kind": "message",
    }
    calls = []

    def get(url, params=None, headers=None):
        calls.append(params)
        body = {"messages": [entry], "has_more": True} if "limit" in params else [entry, entry]
        return SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr(cli.httpx, "get", get)
    result = CliRunner().invoke(cli.app, ["channel", "history", "c1", "-n", "1"])
    assert result.exit_code == 0, result.output
    assert "latest" in result.output and calls == [{"limit": 1}]
    # An older server ignores limit and returns the list: still sliced client-side.
    monkeypatch.setattr(
        cli.httpx,
        "get",
        lambda url, params=None, headers=None: SimpleNamespace(
            status_code=200, json=lambda: [dict(entry, text="old"), entry]
        ),
    )
    result = CliRunner().invoke(cli.app, ["channel", "history", "c1", "-n", "1"])
    assert "latest" in result.output and "old" not in result.output
