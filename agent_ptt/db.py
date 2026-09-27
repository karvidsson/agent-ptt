"""Database layer — SQLAlchemy + libSQL (Turso-compatible).

Local development:  DATABASE_URL=sqlite:///agent_ptt.db  (default)
Production/Turso:   DATABASE_URL=libsql://your-db.turso.io?authToken=...
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from agent_ptt.models import Base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///agent_ptt.db")

engine = create_engine(DATABASE_URL, echo=False)
if engine.dialect.name == "sqlite":

    @event.listens_for(engine, "connect")
    def _sqlite_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def init_db() -> None:
    """Create all tables if they don't exist yet."""
    # Register the additive hosted workspace tables before schema creation.
    from agent_ptt import workspace_delivery, workspace_models, workspace_presence  # noqa: F401

    # Existing workspace tables need this tenant key before new inbox foreign
    # keys are created (create_all does not add indexes to existing tables).
    if inspect(engine).has_table("workspace_messages"):
        for index in workspace_models.ChatMessage.__table__.indexes:
            if index.name == "ux_workspace_message_tenant":
                index.create(bind=engine, checkfirst=True)

    Base.metadata.create_all(bind=engine)
    # Existing databases predate IRC message kinds; create_all does not alter tables.
    with engine.begin() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("messages")}
        if "kind" not in columns:
            connection.execute(
                text("ALTER TABLE messages ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'message'")
            )
        if "context" not in columns:
            connection.execute(text("ALTER TABLE messages ADD COLUMN context JSON"))
    # Local preview databases also need additive onboarding fields. Hosted releases
    # apply the versioned Alembic migration before starting the service.
    with engine.begin() as connection:
        for table, additions in {
            "workspace_invitations": {
                "agent_limit": "INTEGER NOT NULL DEFAULT 0",
                "channel_id": "VARCHAR",
                "claimed_by": "VARCHAR",
            },
            "workspace_agents": {"onboarded_by": "VARCHAR", "harness": "VARCHAR(20)"},
        }.items():
            existing = {column["name"] for column in inspect(connection).get_columns(table)}
            for name, definition in additions.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
    from agent_ptt.voices import migrate_legacy_voice_profiles

    with SessionLocal() as db:
        migrate_legacy_voice_profiles(db)


def get_db():
    """FastAPI dependency — yields a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
