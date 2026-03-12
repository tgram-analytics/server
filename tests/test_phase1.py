"""Phase 1 tests — DB schema, migrations, model constraints, and indexes.

These tests require a live PostgreSQL connection.  Set DATABASE_URL in the
environment (or let the CI service container provide it) before running:

    DATABASE_URL=postgresql+asyncpg://tga:password@localhost/tganalytics_test \
        pytest tests/test_phase1.py -v

The migration round-trip test runs first and leaves the schema in place for
all subsequent model tests.
"""

import os
import subprocess
import uuid
from datetime import UTC
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Server root so alembic.ini is found.
SERVER_ROOT = Path(__file__).parent.parent


# ── Migration round-trip ──────────────────────────────────────────────────────


_CONN_ERROR_KEYWORDS = (
    "password authentication",
    "connection refused",
    "could not connect",
    "no such host",
    "name or service not known",
)


def test_migrations_apply_cleanly(db_url: str) -> None:
    """alembic upgrade → downgrade → upgrade completes without errors."""
    env = {**os.environ, "DATABASE_URL": db_url}

    def run(cmd: list[str]) -> None:
        result = subprocess.run(
            cmd,
            cwd=SERVER_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            combined = (result.stdout + result.stderr).lower()
            if any(kw in combined for kw in _CONN_ERROR_KEYWORDS):
                pytest.skip(f"DB not reachable — {result.stderr[:300]}")
            raise AssertionError(
                f"Command {cmd} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )

    run(["alembic", "upgrade", "head"])
    run(["alembic", "downgrade", "base"])
    run(["alembic", "upgrade", "head"])


# ── Project model ─────────────────────────────────────────────────────────────


async def test_project_can_be_created(db_session: AsyncSession) -> None:
    """Project can be inserted with all required fields."""
    from app.models.project import Project

    project = Project(
        name="acme.com",
        api_key_hash="sha256_deadbeef" + "a" * 52,
        admin_chat_id=123456789,
    )
    db_session.add(project)
    await db_session.flush()

    assert project.id is not None
    assert project.name == "acme.com"
    assert project.domain_allowlist == []


async def test_project_api_key_hash_unique(db_session: AsyncSession) -> None:
    """api_key_hash enforces a UNIQUE constraint."""
    from sqlalchemy.exc import IntegrityError

    from app.models.project import Project

    shared_hash = "unique_hash_" + "b" * 52

    p1 = Project(name="first.com", api_key_hash=shared_hash, admin_chat_id=1)
    p2 = Project(name="second.com", api_key_hash=shared_hash, admin_chat_id=2)

    db_session.add(p1)
    await db_session.flush()

    db_session.add(p2)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_project_created_at_auto_populated(db_session: AsyncSession) -> None:
    """created_at is set server-side and is not None after flush."""
    from app.models.project import Project

    project = Project(
        name="timestamp-test.com",
        api_key_hash="ts_hash_" + "c" * 56,
        admin_chat_id=999,
    )
    db_session.add(project)
    await db_session.flush()
    await db_session.refresh(project)

    assert project.created_at is not None


# ── Event model ───────────────────────────────────────────────────────────────


async def test_event_can_be_inserted_minimal(db_session: AsyncSession) -> None:
    """Event can be inserted with only project_id, event_name, session_id."""
    from app.models.event import Event
    from app.models.project import Project

    project = Project(
        name="event-test.com",
        api_key_hash="evt_hash_" + "d" * 55,
        admin_chat_id=111,
    )
    db_session.add(project)
    await db_session.flush()

    event = Event(
        project_id=project.id,
        event_name="purchase",
        session_id=str(uuid.uuid4()),
    )
    db_session.add(event)
    await db_session.flush()
    await db_session.refresh(event)

    assert event.id is not None
    assert event.url is None
    assert event.referrer is None


async def test_event_properties_defaults_to_empty_dict(db_session: AsyncSession) -> None:
    """properties defaults to an empty dict when not supplied."""
    from app.models.event import Event
    from app.models.project import Project

    project = Project(
        name="props-test.com",
        api_key_hash="props_hash_" + "e" * 53,
        admin_chat_id=222,
    )
    db_session.add(project)
    await db_session.flush()

    event = Event(
        project_id=project.id,
        event_name="signup",
        session_id=str(uuid.uuid4()),
    )
    db_session.add(event)
    await db_session.flush()
    await db_session.refresh(event)

    assert event.properties == {}


async def test_event_received_at_is_server_time(db_session: AsyncSession) -> None:
    """received_at is always set server-side regardless of client timestamp."""
    from datetime import datetime

    from app.models.event import Event
    from app.models.project import Project

    project = Project(
        name="rcvd-test.com",
        api_key_hash="rcvd_hash_" + "f" * 54,
        admin_chat_id=333,
    )
    db_session.add(project)
    await db_session.flush()

    client_ts = datetime(2000, 1, 1, tzinfo=UTC)
    event = Event(
        project_id=project.id,
        event_name="pageview",
        session_id=str(uuid.uuid4()),
        timestamp=client_ts,
    )
    db_session.add(event)
    await db_session.flush()
    await db_session.refresh(event)

    # received_at is the current server time, not the client-supplied timestamp.
    assert event.received_at is not None
    assert event.received_at != client_ts


# ── Settings defaults ─────────────────────────────────────────────────────────


async def test_settings_retention_days_defaults_to_90(db_session: AsyncSession) -> None:
    """ProjectSettings row defaults retention_days to 90."""
    from app.models.project import Project
    from app.models.settings import ProjectSettings

    project = Project(
        name="settings-test.com",
        api_key_hash="cfg_hash_" + "g" * 55,
        admin_chat_id=444,
    )
    db_session.add(project)
    await db_session.flush()

    settings = ProjectSettings(project_id=project.id)
    db_session.add(settings)
    await db_session.flush()
    await db_session.refresh(settings)

    assert settings.retention_days == 90


# ── Indexes ───────────────────────────────────────────────────────────────────


async def test_indexes_exist(db_session: AsyncSession) -> None:
    """Expected indexes are present in pg_indexes after migration."""
    expected = {
        "ix_events_project_event_ts",
        "ix_events_session_id",
        "ix_aggregations_lookup",
    }

    result = await db_session.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    )
    existing = {row[0] for row in result.fetchall()}

    missing = expected - existing
    assert not missing, f"Missing indexes after migration: {missing}"
