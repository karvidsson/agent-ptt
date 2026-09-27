"""Alembic environment configuration."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from agent_ptt import workspace_delivery, workspace_models, workspace_presence  # noqa: F401
from agent_ptt.db import DATABASE_URL
from agent_ptt.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def workspace_objects(obj, name, kind, reflected, compare_to):
    """Autogeneration must never propose changes to the local voice schema."""
    return kind != "table" or name.startswith("workspace_")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    raise RuntimeError("The additive workspace baseline requires an online schema inspection")


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    supplied = config.attributes.get("connection")
    if supplied is not None:
        context.configure(
            connection=supplied, target_metadata=target_metadata, include_object=workspace_objects
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, include_object=workspace_objects
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
