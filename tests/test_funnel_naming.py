"""Custom funnel names.

Funnels used to be auto-named by joining every step with ``→``.  The result
overflowed the chart title, which stretched the PNG canvas sideways and left
the plot as an unreadable sliver.  The creation flow now asks for a short name
(with a Skip that produces a compact fallback), and existing funnels can be
renamed.

CallbackQuery/Update objects are mocked with MagicMock/AsyncMock and the
handlers are called directly, mirroring ``tests/test_funnels.py``.
"""


async def test_time_window_step_asks_for_a_name_instead_of_creating(
    session_factory, singleton_user
):
    """Picking the window no longer writes the funnel — it prompts for a name."""
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text
    from telegram import Message

    from app.bot.handlers.funnels import _pick_time_window
    from app.bot.states import BotStateService
    from app.services.funnels import list_funnels
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session, name="namefnl.com", admin_chat_id=111, owner_user_id=singleton_user.id
        )
        await session.commit()
        pid_str, pid = str(project.id), project.id

    chat_id = 3341
    async with session_factory() as session:
        await BotStateService(session).save(
            chat_id,
            flow="add_funnel",
            step="window",
            payload={
                "project_id": pid_str,
                "events": ["a", "b"],
                "owner_user_id": str(singleton_user.id),
            },
        )
        await session.commit()

    query = MagicMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = chat_id
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    await _pick_time_window(query, "1h", singleton_user.id)

    prompt = query.edit_message_text.call_args[0][0]
    assert "name" in prompt.lower()

    async with session_factory() as session:
        assert await list_funnels(session, project_id=pid) == []
        state = await BotStateService(session).get(chat_id)
        assert state is not None
        assert state.step == "name"
        assert state.payload["window"] == 3600

        await BotStateService(session).clear(chat_id)
        await session.execute(text("DELETE FROM projects WHERE id = :i"), {"i": pid_str})
        await session.commit()


async def test_typed_name_becomes_the_funnel_name(session_factory, singleton_user):
    """The name the user types is stored verbatim, not the joined step chain."""
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text

    from app.bot.handlers.funnels import handle_funnel_name_text
    from app.bot.states import BotStateService
    from app.services.funnels import list_funnels
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session, name="namefnl2.com", admin_chat_id=111, owner_user_id=singleton_user.id
        )
        await session.commit()
        pid_str, pid = str(project.id), project.id

    chat_id = 3342
    async with session_factory() as session:
        await BotStateService(session).save(
            chat_id,
            flow="add_funnel",
            step="name",
            payload={
                "project_id": pid_str,
                "events": ["signup", "paywall_shown", "checkout_done"],
                "owner_user_id": str(singleton_user.id),
                "window": 3600,
            },
        )
        await session.commit()

    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = "  Signup   →  paid  "
    update.message.reply_text = AsyncMock()

    async with session_factory() as session:
        svc = BotStateService(session)
        state = await svc.get(chat_id)
        await handle_funnel_name_text(update, session, svc, state)

    async with session_factory() as session:
        funnels = await list_funnels(session, project_id=pid)
        assert len(funnels) == 1
        # Whitespace is collapsed; the chain is not appended.
        assert funnels[0].name == "Signup → paid"
        assert funnels[0].steps == ["signup", "paywall_shown", "checkout_done"]
        # Flow finished, state cleared.
        assert await BotStateService(session).get(chat_id) is None

        await session.execute(text("DELETE FROM projects WHERE id = :i"), {"i": pid_str})
        await session.commit()


async def test_an_over_long_typed_name_is_truncated(session_factory, singleton_user):
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text

    from app.bot.handlers.funnels import MAX_FUNNEL_NAME_CHARS, handle_funnel_name_text
    from app.bot.states import BotStateService
    from app.services.funnels import list_funnels
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session, name="namefnl6.com", admin_chat_id=111, owner_user_id=singleton_user.id
        )
        await session.commit()
        pid_str, pid = str(project.id), project.id

    chat_id = 3346
    async with session_factory() as session:
        await BotStateService(session).save(
            chat_id,
            flow="add_funnel",
            step="name",
            payload={
                "project_id": pid_str,
                "events": ["a", "b"],
                "owner_user_id": str(singleton_user.id),
                "window": 3600,
            },
        )
        await session.commit()

    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = "x" * 500
    update.message.reply_text = AsyncMock()

    async with session_factory() as session:
        svc = BotStateService(session)
        state = await svc.get(chat_id)
        await handle_funnel_name_text(update, session, svc, state)

    async with session_factory() as session:
        funnels = await list_funnels(session, project_id=pid)
        assert len(funnels) == 1
        assert len(funnels[0].name) <= MAX_FUNNEL_NAME_CHARS

        await session.execute(text("DELETE FROM projects WHERE id = :i"), {"i": pid_str})
        await session.commit()


async def test_empty_name_reprompts_without_creating(session_factory, singleton_user):
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text

    from app.bot.handlers.funnels import handle_funnel_name_text
    from app.bot.states import BotStateService
    from app.services.funnels import list_funnels
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session, name="namefnl7.com", admin_chat_id=111, owner_user_id=singleton_user.id
        )
        await session.commit()
        pid_str, pid = str(project.id), project.id

    chat_id = 3347
    async with session_factory() as session:
        await BotStateService(session).save(
            chat_id,
            flow="add_funnel",
            step="name",
            payload={
                "project_id": pid_str,
                "events": ["a", "b"],
                "owner_user_id": str(singleton_user.id),
                "window": 3600,
            },
        )
        await session.commit()

    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = "   "
    update.message.reply_text = AsyncMock()

    async with session_factory() as session:
        svc = BotStateService(session)
        state = await svc.get(chat_id)
        await handle_funnel_name_text(update, session, svc, state)

    assert "empty" in update.message.reply_text.call_args[0][0].lower()

    async with session_factory() as session:
        assert await list_funnels(session, project_id=pid) == []
        # Still waiting on a name.
        state = await BotStateService(session).get(chat_id)
        assert state is not None and state.step == "name"

        await BotStateService(session).clear(chat_id)
        await session.execute(text("DELETE FROM projects WHERE id = :i"), {"i": pid_str})
        await session.commit()


async def test_skipped_name_is_short_not_the_whole_chain(session_factory, singleton_user):
    """Skipping still yields a compact name — the old flow joined every step."""
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text
    from telegram import Message

    from app.bot.handlers.funnels import _skip_funnel_name
    from app.bot.states import BotStateService
    from app.services.funnels import list_funnels
    from app.services.projects import create_project

    steps = ["signup", "onboarding_completed", "app_opened", "paywall_shown", "app_job_done"]

    async with session_factory() as session:
        project, _ = await create_project(
            session, name="namefnl3.com", admin_chat_id=111, owner_user_id=singleton_user.id
        )
        await session.commit()
        pid_str, pid = str(project.id), project.id

    chat_id = 3343
    async with session_factory() as session:
        await BotStateService(session).save(
            chat_id,
            flow="add_funnel",
            step="name",
            payload={
                "project_id": pid_str,
                "events": steps,
                "owner_user_id": str(singleton_user.id),
                "window": 300,
            },
        )
        await session.commit()

    query = MagicMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = chat_id
    query.edit_message_text = AsyncMock()

    await _skip_funnel_name(query)

    async with session_factory() as session:
        funnels = await list_funnels(session, project_id=pid)
        assert len(funnels) == 1
        name = funnels[0].name
        assert name == "signup → app_job_done (5 steps)"
        assert "onboarding_completed" not in name

        await session.execute(text("DELETE FROM projects WHERE id = :i"), {"i": pid_str})
        await session.commit()


async def test_skip_rejects_a_foreign_project(session_factory, singleton_user):
    """Crafted state cannot write a funnel onto another tenant's project."""
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text
    from telegram import Message

    from app.bot.handlers.funnels import _skip_funnel_name
    from app.bot.states import BotStateService
    from app.models.user import User
    from app.services.funnels import list_funnels
    from app.services.projects import create_project

    async with session_factory() as session:
        victim = User(telegram_user_id=999_557)
        session.add(victim)
        await session.flush()
        project, _ = await create_project(
            session, name="victimfnl8.com", admin_chat_id=999_557, owner_user_id=victim.id
        )
        await session.commit()
        victim_id, victim_pid, victim_pid_str = victim.id, project.id, str(project.id)

    chat_id = 3348
    async with session_factory() as session:
        await BotStateService(session).save(
            chat_id,
            flow="add_funnel",
            step="name",
            payload={
                "project_id": victim_pid_str,
                "events": ["a", "b"],
                "owner_user_id": str(singleton_user.id),
                "window": 3600,
            },
        )
        await session.commit()

    query = MagicMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = chat_id
    query.edit_message_text = AsyncMock()

    await _skip_funnel_name(query)

    async with session_factory() as session:
        assert await list_funnels(session, project_id=victim_pid) == []
        assert await BotStateService(session).get(chat_id) is None

        await session.execute(
            text("DELETE FROM projects WHERE owner_user_id = :o"), {"o": str(victim_id)}
        )
        await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(victim_id)})
        await session.commit()


async def test_rename_updates_an_existing_funnel(session_factory, singleton_user):
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text

    from app.bot.handlers.funnels import handle_funnel_name_text
    from app.bot.states import BotStateService
    from app.services.funnels import create_funnel, get_funnel
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session, name="namefnl4.com", admin_chat_id=111, owner_user_id=singleton_user.id
        )
        await session.flush()
        funnel = await create_funnel(
            session,
            project_id=project.id,
            name="signup → a → b → c → d",
            steps=["signup", "a", "b", "c", "d"],
            time_window=3600,
        )
        await session.commit()
        pid_str, fid = str(project.id), funnel.id

    chat_id = 3344
    async with session_factory() as session:
        await BotStateService(session).save(
            chat_id,
            flow="rename_funnel",
            step="name",
            payload={
                "funnel_id": str(fid),
                "project_id": pid_str,
                "owner_user_id": str(singleton_user.id),
            },
        )
        await session.commit()

    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = "Activation"
    update.message.reply_text = AsyncMock()

    async with session_factory() as session:
        svc = BotStateService(session)
        state = await svc.get(chat_id)
        await handle_funnel_name_text(update, session, svc, state)

    async with session_factory() as session:
        refreshed = await get_funnel(session, fid)
        assert refreshed is not None
        assert refreshed.name == "Activation"
        assert await BotStateService(session).get(chat_id) is None

        await session.execute(text("DELETE FROM projects WHERE id = :i"), {"i": pid_str})
        await session.commit()


async def test_rename_rejects_a_foreign_funnel(session_factory, singleton_user):
    """A crafted rename payload cannot touch another tenant's funnel."""
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy import text

    from app.bot.handlers.funnels import handle_funnel_name_text
    from app.bot.states import BotStateService
    from app.models.user import User
    from app.services.funnels import create_funnel, get_funnel
    from app.services.projects import create_project

    async with session_factory() as session:
        victim = User(telegram_user_id=999_556)
        session.add(victim)
        await session.flush()
        project, _ = await create_project(
            session, name="victimfnl5.com", admin_chat_id=999_556, owner_user_id=victim.id
        )
        await session.flush()
        funnel = await create_funnel(
            session,
            project_id=project.id,
            name="victim funnel",
            steps=["a", "b"],
            time_window=3600,
        )
        await session.commit()
        victim_id, pid_str, fid = victim.id, str(project.id), funnel.id

    chat_id = 3345
    async with session_factory() as session:
        await BotStateService(session).save(
            chat_id,
            flow="rename_funnel",
            step="name",
            payload={
                "funnel_id": str(fid),
                "project_id": pid_str,
                "owner_user_id": str(singleton_user.id),
            },
        )
        await session.commit()

    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = "pwned"
    update.message.reply_text = AsyncMock()

    async with session_factory() as session:
        svc = BotStateService(session)
        state = await svc.get(chat_id)
        await handle_funnel_name_text(update, session, svc, state)

    assert "not found" in update.message.reply_text.call_args[0][0].lower()

    async with session_factory() as session:
        unchanged = await get_funnel(session, fid)
        assert unchanged is not None
        assert unchanged.name == "victim funnel"
        assert await BotStateService(session).get(chat_id) is None

        await session.execute(
            text("DELETE FROM projects WHERE owner_user_id = :o"), {"o": str(victim_id)}
        )
        await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(victim_id)})
        await session.commit()
