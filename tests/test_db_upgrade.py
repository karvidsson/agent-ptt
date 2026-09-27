"""Upgrading an existing message archive must preserve its contents."""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from agent_ptt import db


def test_init_db_upgrades_legacy_messages_without_losing_history(tmp_path, monkeypatch):
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
            text("""
            INSERT INTO messages VALUES ('m1', 'c1', 'k1', 'Ada', 'Keep this', NULL)
        """)
        )
    with monkeypatch.context() as patch:
        patch.setattr(db, "engine", legacy)
        patch.setattr(db, "SessionLocal", sessionmaker(bind=legacy, class_=Session))
        db.init_db()
        db.init_db()  # Repeated startup is safe.
    with legacy.connect() as connection:
        assert connection.execute(text("SELECT text, kind FROM messages")).one() == (
            "Keep this",
            "message",
        )
    legacy.dispose()
