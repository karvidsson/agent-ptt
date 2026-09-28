"""Run on SQLite, and on isolated PostgreSQL schemas when a test URL is provided."""

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_ptt import db, server, workspace

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_engine(request, tmp_path):
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")

        @event.listens_for(engine, "connect")
        def foreign_keys(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")

        yield engine
        engine.dispose()
        return
    url = os.environ.get("AGENT_PTT_TEST_POSTGRES")
    if not url:
        pytest.skip("Set AGENT_PTT_TEST_POSTGRES to execute real PostgreSQL validation")
    admin = create_engine(url)
    schema = "ptt_test_" + uuid.uuid4().hex
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def upgrade(engine):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def test_migration_preserves_legacy_data_and_adopts_existing_workspace(migration_engine):
    engine = migration_engine
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE local_keep (id INTEGER PRIMARY KEY, payload TEXT)"))
        conn.execute(text("INSERT INTO local_keep VALUES (1, 'local remains local')"))
        conn.execute(text("CREATE TABLE channels (channel_id VARCHAR PRIMARY KEY, name VARCHAR)"))
        conn.execute(text("INSERT INTO channels VALUES ('local-room', 'Existing voice room')"))
    upgrade(engine)
    with engine.begin() as conn:
        assert conn.scalar(text("SELECT payload FROM local_keep")) == "local remains local"
        conn.execute(
            text(
                "INSERT INTO workspace_users (id,email,name,password_hash,created_at) "
                "VALUES ('u','u@example.com','User','unused',CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(text("DELETE FROM alembic_version"))  # Adopt a pre-migration preview.
        # Simulate the preview before delivery tables and the tenant message index.
        conn.execute(text("DROP TABLE workspace_replies"))
        conn.execute(text("DROP TABLE workspace_deliveries"))
        conn.execute(text("DROP INDEX ux_workspace_message_tenant"))
    upgrade(engine)
    upgrade(engine)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT name FROM workspace_users WHERE id='u'")) == "User"
        assert conn.scalar(text("SELECT COUNT(*) FROM workspace_organizations")) == 0
        assert conn.scalar(text("SELECT name FROM channels")) == "Existing voice room"
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "20260928_join_links"
    assert "workspace_deliveries" in inspect(engine).get_table_names()


def test_migration_rejects_incompatible_preview_schema(migration_engine):
    with migration_engine.begin() as conn:
        conn.execute(text("CREATE TABLE workspace_users (id VARCHAR PRIMARY KEY)"))
    with pytest.raises(RuntimeError, match="unsupported schema"):
        upgrade(migration_engine)


def test_migrated_database_runs_authenticated_delivery(migration_engine, monkeypatch):
    engine = migration_engine
    upgrade(engine)
    sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    for module in (db, server, workspace):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setenv("AGENT_PTT_HOSTED", "1")
    monkeypatch.setenv("AGENT_PTT_PUBLIC_ORIGIN", "http://localhost:8770")
    with TestClient(server.app, headers={"Origin": "http://localhost:8770"}) as owner:
        assert (
            owner.post(
                "/api/workspace/signup",
                json={
                    "email": "pg@example.com",
                    "name": "Owner",
                    "password": "long validation passphrase",
                    "organization": "First",
                },
            ).status_code
            == 201
        )
        org = owner.get("/api/workspace/me").json()["organizations"][0]["id"]
        base = f"/api/workspace/organizations/{org}"
        room = owner.get(base + "/channels").json()[0]["id"]
        agent = owner.post(base + "/agents", json={"name": "Agent"}).json()
        path = base + f"/channels/{room}"
        payload = {"text": "Please test", "client_id": "once", "recipient_ids": [agent["id"]]}
        message = owner.post(path + "/messages", json=payload).json()
        assert owner.post(path + "/messages", json=payload).json()["id"] == message["id"]
        other = owner.post("/api/workspace/organizations", json={"name": "Other"}).json()["id"]
        with TestClient(server.app, headers={"Authorization": "Bearer " + agent["token"]}) as bot:
            assert (
                bot.post(
                    base + "/sessions/pg-validation/presence",
                    json={"state": "working", "channel_id": room, "task": "Validate migration"},
                ).status_code
                == 204
            )
            roster = owner.get(base + "/sessions").json()
            assert roster[0]["principal_id"] == agent["id"]
            assert roster[0]["state"] == "working"
            assert bot.get(f"/api/workspace/organizations/{other}/channels").status_code == 404
            assert bot.get(path + "/inbox").json()[0]["id"] == message["id"]
            response = bot.post(
                path + "/messages",
                json={"text": "Answered", "client_id": "reply", "reply_to": message["id"]},
            )
            assert response.status_code == 201
            assert response.json()["sender_id"] == agent["id"]
            assert bot.get(path + "/inbox").json() == []
            owner.delete(base + "/agents/" + agent["id"])
            assert bot.get(path + "/inbox").status_code == 401
            assert owner.get(base + "/sessions").json() == []
        with sessions() as session:
            from agent_ptt.workspace_delivery import Delivery

            session.add(
                Delivery(
                    org_id=other,
                    message_id=message["id"],
                    recipient_id=agent["id"],
                    recipient_name="Bad",
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
