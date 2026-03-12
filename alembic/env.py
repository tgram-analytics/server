"""Alembic environment configuration.

Reads DATABASE_URL from environment variables and runs migrations using the
async SQLAlchemy engine so the migration context matches the application engine.
"""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# Alembic Config object — gives access to values in alembic.ini.
config = context.config

# Set up Python logging as configured in alembic.ini.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import the declarative Base so Alembic can detect model changes.
from app.core.database import Base  # noqa: E402
from app.models import *  # noqa: E402, F401, F403 — ensure all models are registered

target_metadata = Base.metadata


def get_database_url() -> str:
    """Return the DATABASE_URL from the environment, with a clear error if missing."""
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Copy .env.example to .env and fill in the value."
        )
    return url


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (generates SQL script)."""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live async DB connection."""
    connectable = create_async_engine(
        get_database_url(),
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
