"""Tests for the 🩺 /doctor bot handler."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

ADMIN_ID = 111


def _make_message():
    update = MagicMock()
    update.effective_chat.id = ADMIN_ID
    update.effective_user.id = ADMIN_ID
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    ctx = MagicMock()
    return update, ctx


async def test_doctor_no_projects(session_factory, singleton_user):
    """No projects → friendly empty-state message."""
    from app.bot.handlers.doctor import doctor_command

    update, ctx = _make_message()
    await doctor_command(update, ctx)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Doctor" in text
    assert "No projects" in text


async def test_doctor_flags_no_events_and_open_allowlist(session_factory, singleton_user):
    """Project with zero events AND empty allowlist → 2 issues, no ✅."""
    from app.bot.handlers.doctor import doctor_command
    from app.services.projects import create_project

    async with session_factory() as session:
        await create_project(
            session,
            name="brand-new.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()

    update, ctx = _make_message()
    await doctor_command(update, ctx)

    text = update.message.reply_text.call_args[0][0]
    assert "brand-new.com" in text
    assert "No events received yet" in text
    assert "open" in text
    assert "any site can POST" in text
    assert "2 issues detected" in text


async def test_doctor_passes_with_recent_events_and_allowlist(session_factory, singleton_user):
    """Recent events + non-empty allowlist → all checks pass."""
    from sqlalchemy import update as sql_update

    from app.bot.handlers.doctor import doctor_command
    from app.models.event import Event
    from app.models.project import Project
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="healthy.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = project.id

        await session.execute(
            sql_update(Project).where(Project.id == pid).values(domain_allowlist=["healthy.com"])
        )
        session.add(
            Event(
                project_id=pid,
                event_name="pageview",
                session_id="s1",
                properties={},
                timestamp=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        await session.commit()

    update, ctx = _make_message()
    await doctor_command(update, ctx)

    text = update.message.reply_text.call_args[0][0]
    assert "healthy.com" in text
    assert "All checks passed" in text
    assert "issue" not in text.lower() or "issues detected" not in text


async def test_doctor_flags_stale_events(session_factory, singleton_user):
    """Last event >7 days ago → stale warning."""
    from sqlalchemy import update as sql_update

    from app.bot.handlers.doctor import doctor_command
    from app.models.event import Event
    from app.models.project import Project
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="stale.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = project.id

        await session.execute(
            sql_update(Project).where(Project.id == pid).values(domain_allowlist=["stale.com"])
        )
        # received_at is server-default now() — backdate via SQL after insert.
        from app.models.event import Event as EventModel

        old_ts = datetime.now(UTC) - timedelta(days=10)
        evt = Event(
            project_id=pid,
            event_name="pageview",
            session_id="s1",
            properties={},
            timestamp=old_ts,
        )
        session.add(evt)
        await session.commit()
        await session.execute(
            sql_update(EventModel).where(EventModel.id == evt.id).values(received_at=old_ts)
        )
        await session.commit()

    update, ctx = _make_message()
    await doctor_command(update, ctx)

    text = update.message.reply_text.call_args[0][0]
    assert "stale.com" in text
    assert "stale" in text
    assert "1 issue detected" in text


async def test_doctor_multi_project_mixed_states(session_factory, singleton_user):
    """Healthy + broken project together → counts only the broken one."""
    from sqlalchemy import update as sql_update

    from app.bot.handlers.doctor import doctor_command
    from app.models.event import Event
    from app.models.project import Project
    from app.services.projects import create_project

    async with session_factory() as session:
        good, _ = await create_project(
            session,
            name="good.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        bad, _ = await create_project(
            session,
            name="bad.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()

        await session.execute(
            sql_update(Project).where(Project.id == good.id).values(domain_allowlist=["good.com"])
        )
        session.add(
            Event(
                project_id=good.id,
                event_name="pageview",
                session_id="s1",
                properties={},
                timestamp=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        # bad.com: no events, no allowlist → 2 issues
        await session.commit()

    update, ctx = _make_message()
    await doctor_command(update, ctx)

    text = update.message.reply_text.call_args[0][0]
    assert "good.com" in text
    assert "bad.com" in text
    assert "2 issues detected" in text
