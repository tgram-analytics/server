"""Funnel handler IDOR regression tests.

CallbackQuery objects are mocked with MagicMock/AsyncMock and the funnel
handlers are called directly, mirroring ``tests/test_alerts.py``.
"""


async def test_start_add_funnel_rejects_foreign_project(session_factory, singleton_user):
    """A non-owner cannot start funnel creation on another tenant's project."""
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text
    from telegram import Message

    from app.bot.handlers.funnels import _start_add_funnel
    from app.bot.states import BotStateService
    from app.models.user import User
    from app.services.projects import create_project

    async with session_factory() as session:
        victim = User(telegram_user_id=999_555)
        session.add(victim)
        await session.flush()
        project, _ = await create_project(
            session, name="victimfnl.com", admin_chat_id=999_555, owner_user_id=victim.id
        )
        await session.commit()
        victim_pid = str(project.id)
        victim_id = victim.id

    # Chat id no other funnel test touches, so the "no state saved" assertion
    # isolates THIS handler's behaviour.
    foreign_chat_id = 333

    query = MagicMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = foreign_chat_id
    query.edit_message_text = AsyncMock()

    await _start_add_funnel(query, victim_pid, singleton_user.id)

    query.edit_message_text.assert_called_once()
    # Ownership refusal — and event names are NOT leaked.
    assert "not found" in query.edit_message_text.call_args[0][0].lower()
    async with session_factory() as session:
        assert await BotStateService(session).get(foreign_chat_id) is None
        await session.execute(
            text("DELETE FROM projects WHERE owner_user_id = :o"), {"o": str(victim_id)}
        )
        await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(victim_id)})
        await session.commit()


async def test_pick_time_window_rejects_foreign_project(session_factory, singleton_user):
    """The completion step re-verifies ownership before writing a funnel row.

    Even if crafted state carries a foreign ``project_id``, the stashed
    ``owner_user_id`` must not own it, so no funnel is created.
    """
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text
    from telegram import Message

    from app.bot.handlers.funnels import _pick_time_window
    from app.bot.states import BotStateService
    from app.models.user import User
    from app.services.funnels import list_funnels
    from app.services.projects import create_project

    async with session_factory() as session:
        victim = User(telegram_user_id=999_555)
        session.add(victim)
        await session.flush()
        project, _ = await create_project(
            session, name="victimfnl2.com", admin_chat_id=999_555, owner_user_id=victim.id
        )
        await session.commit()
        victim_pid = str(project.id)
        victim_project_id = project.id
        victim_id = victim.id

    foreign_chat_id = 334

    # Seed crafted state: foreign project, attacker's owner stashed.
    async with session_factory() as session:
        await BotStateService(session).save(
            foreign_chat_id,
            flow="add_funnel",
            step="window",
            payload={
                "project_id": victim_pid,
                "events": ["a", "b"],
                "owner_user_id": str(singleton_user.id),
            },
        )
        await session.commit()

    query = MagicMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = foreign_chat_id
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    await _pick_time_window(query, "5min", singleton_user.id)

    query.edit_message_text.assert_called_once()
    assert "not found" in query.edit_message_text.call_args[0][0].lower()
    async with session_factory() as session:
        # No funnel written onto the victim's project.
        funnels = await list_funnels(session, project_id=victim_project_id)
        assert funnels == []
        # State cleared.
        assert await BotStateService(session).get(foreign_chat_id) is None
        await session.execute(
            text("DELETE FROM projects WHERE owner_user_id = :o"), {"o": str(victim_id)}
        )
        await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(victim_id)})
        await session.commit()
