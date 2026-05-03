"""Phase 4.5 — retention job + APScheduler wiring tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core import database as db_mod
from app.jobs import scheduler as scheduler_mod
from app.jobs.retention import run_retention_job
from app.jobs.scheduler import (
    get_scheduler,
    shutdown_scheduler,
    start_scheduler,
)
from app.models.event import Event
from app.models.project import Project
from app.models.settings import ProjectSettings
from app.models.user import User


@pytest.fixture()
async def wired_db(async_engine: AsyncEngine):
    """Wire app.core.database module-level singletons to the test engine.

    The retention job calls ``get_session_factory()`` which only works when
    ``init_db()`` has been called. We patch the module globals directly so
    we don't leak a second engine.
    """
    prev_engine = db_mod._engine
    prev_factory = db_mod._session_factory
    db_mod._engine = async_engine
    db_mod._session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    try:
        yield db_mod._session_factory
    finally:
        db_mod._engine = prev_engine
        db_mod._session_factory = prev_factory


async def _seed(factory, retention_days: int):
    """Seed user, project, settings, and two events. Returns (project_id, old_id, new_id)."""
    now = datetime.now(UTC)
    old_received = now - timedelta(days=100)
    new_received = now - timedelta(days=1)

    async with factory() as session, session.begin():
        # Use a unique tg id to avoid collisions across tests.
        user = User(telegram_user_id=900_000 + int(now.timestamp()) % 100_000)
        session.add(user)
        await session.flush()

        project = Project(
            name=f"retention-{uuid.uuid4().hex[:8]}.com",
            api_key_hash=f"ret_hash_{uuid.uuid4().hex}",
            admin_chat_id=12345,
            owner_user_id=user.id,
        )
        session.add(project)
        await session.flush()

        settings = ProjectSettings(project_id=project.id, retention_days=retention_days)
        session.add(settings)

        old_event = Event(
            project_id=project.id,
            event_name="old",
            session_id=str(uuid.uuid4()),
        )
        new_event = Event(
            project_id=project.id,
            event_name="new",
            session_id=str(uuid.uuid4()),
        )
        session.add_all([old_event, new_event])
        await session.flush()

        old_id = old_event.id
        new_id = new_event.id
        project_id = project.id

        # Override server-default received_at.
        await session.execute(
            sa.update(Event).where(Event.id == old_id).values(received_at=old_received)
        )
        await session.execute(
            sa.update(Event).where(Event.id == new_id).values(received_at=new_received)
        )

    return project_id, old_id, new_id


async def _cleanup(factory, project_id):
    async with factory() as session, session.begin():
        await session.execute(sa.delete(Event).where(Event.project_id == project_id))
        await session.execute(
            sa.delete(ProjectSettings).where(ProjectSettings.project_id == project_id)
        )
        proj = await session.get(Project, project_id)
        owner_id = proj.owner_user_id if proj else None
        await session.execute(sa.delete(Project).where(Project.id == project_id))
        if owner_id is not None:
            await session.execute(sa.delete(User).where(User.id == owner_id))


async def test_retention_job_deletes_old_events(wired_db) -> None:
    factory = wired_db
    project_id, old_id, new_id = await _seed(factory, retention_days=30)
    try:
        deleted = await run_retention_job()
        assert deleted == 1

        async with factory() as session:
            old_row = await session.get(Event, old_id)
            new_row = await session.get(Event, new_id)
            assert old_row is None
            assert new_row is not None
    finally:
        await _cleanup(factory, project_id)


async def test_retention_job_keeps_forever_when_zero(wired_db) -> None:
    factory = wired_db
    project_id, old_id, new_id = await _seed(factory, retention_days=0)
    try:
        deleted = await run_retention_job()
        assert deleted == 0

        async with factory() as session:
            old_row = await session.get(Event, old_id)
            new_row = await session.get(Event, new_id)
            assert old_row is not None
            assert new_row is not None
    finally:
        await _cleanup(factory, project_id)


@pytest.fixture()
def scheduler_isolation():
    """Snapshot/restore the module-global scheduler so the test can't leak."""
    prev = scheduler_mod._scheduler
    scheduler_mod._scheduler = None
    try:
        yield
    finally:
        import contextlib

        sched = scheduler_mod._scheduler
        if sched is not None:
            with contextlib.suppress(Exception):
                sched.shutdown(wait=False)
        scheduler_mod._scheduler = prev


async def test_scheduler_lifecycle(scheduler_isolation) -> None:
    assert get_scheduler() is None
    start_scheduler()
    sched = get_scheduler()
    assert sched is not None
    assert sched.running

    # Idempotent: a second start_scheduler() must not replace the instance.
    start_scheduler()
    assert get_scheduler() is sched

    await shutdown_scheduler()
    assert get_scheduler() is None
