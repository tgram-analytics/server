"""Tests for the ⚙️ Settings bot handler."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from telegram import Message

ADMIN_ID = 111


def _make_callback(chat_id: int = ADMIN_ID, data: str = "menu:settings:some-uuid"):
    update = MagicMock()
    update.effective_user.id = chat_id
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.message.chat_id = chat_id
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    ctx = MagicMock()
    return update, ctx


def _make_message(chat_id: int = ADMIN_ID, text: str = "30"):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = chat_id
    update.message.reply_text = AsyncMock()
    update.message.text = text
    update.callback_query = None
    ctx = MagicMock()
    return update, ctx


# ── show_settings_menu ────────────────────────────────────────────────────────


async def test_show_settings_menu_defaults(session_factory, singleton_user):
    """Settings menu shows default retention (90 days) and open allowlist."""
    from app.bot.handlers.settings import show_settings_menu
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="settings-default.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

    query = MagicMock()
    query.edit_message_text = AsyncMock()

    await show_settings_menu(query, pid, singleton_user.id)

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "settings-default.com" in text
    assert "90" in text
    assert "All origins" in text

    keyboard = query.edit_message_text.call_args[1].get("reply_markup")
    assert keyboard is not None
    flat = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("Retention" in label for label in flat)
    assert any("Allowlist" in label for label in flat)


async def test_show_settings_menu_project_not_found(session_factory, singleton_user):
    from app.bot.handlers.settings import show_settings_menu

    query = MagicMock()
    query.edit_message_text = AsyncMock()

    await show_settings_menu(query, str(uuid.uuid4()), singleton_user.id)

    text = query.edit_message_text.call_args[0][0]
    assert "not found" in text.lower()


# ── set_retention flow ────────────────────────────────────────────────────────


async def test_start_set_retention_saves_state(session_factory, singleton_user):
    """Tapping ✏️ Retention saves flow state and shows prompt."""
    from app.bot.handlers.settings import start_set_retention
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="ret-flow.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

    query = MagicMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = ADMIN_ID
    query.edit_message_text = AsyncMock()

    await start_set_retention(query, pid, singleton_user.id)

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "retention" in text.lower()
    assert "days" in text.lower()


async def test_set_retention_updates_database(session_factory, singleton_user):
    """Typing a valid number updates ProjectSettings.retention_days."""
    from sqlalchemy import select

    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.models.settings import ProjectSettings
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="ret-update.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(ADMIN_ID, flow="set_retention", step="value", payload={"project_id": pid})
        await session.commit()

    update, ctx = _make_message(chat_id=ADMIN_ID, text="30")
    await handle_text_message(update, ctx)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "30" in text

    async with session_factory() as session:
        result = await session.execute(
            select(ProjectSettings).where(ProjectSettings.project_id == uuid.UUID(pid))
        )
        ps = result.scalar_one()
        assert ps.retention_days == 30


async def test_set_retention_invalid_input(session_factory, singleton_user):
    """Non-numeric input shows error without updating DB."""
    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="ret-invalid.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(ADMIN_ID, flow="set_retention", step="value", payload={"project_id": pid})
        await session.commit()

    update, ctx = _make_message(chat_id=ADMIN_ID, text="not-a-number")
    await handle_text_message(update, ctx)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "integer" in text.lower()


async def test_set_retention_forever(session_factory, singleton_user):
    """Entering 0 sets retention to 'forever'."""
    from sqlalchemy import select

    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.models.settings import ProjectSettings
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="ret-forever.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(ADMIN_ID, flow="set_retention", step="value", payload={"project_id": pid})
        await session.commit()

    update, ctx = _make_message(chat_id=ADMIN_ID, text="0")
    await handle_text_message(update, ctx)

    text = update.message.reply_text.call_args[0][0]
    assert "forever" in text.lower()

    async with session_factory() as session:
        result = await session.execute(
            select(ProjectSettings).where(ProjectSettings.project_id == uuid.UUID(pid))
        )
        ps = result.scalar_one()
        assert ps.retention_days == 0


# ── set_allowlist flow ────────────────────────────────────────────────────────


async def test_set_allowlist_updates_project(session_factory, singleton_user):
    """Typing domains updates project.domain_allowlist."""
    from sqlalchemy import select

    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.models.project import Project
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="allow-update.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(ADMIN_ID, flow="set_allowlist", step="value", payload={"project_id": pid})
        await session.commit()

    update, ctx = _make_message(chat_id=ADMIN_ID, text="myapp.com, api.myapp.com")
    await handle_text_message(update, ctx)

    text = update.message.reply_text.call_args[0][0]
    assert "myapp.com" in text

    async with session_factory() as session:
        result = await session.execute(select(Project).where(Project.id == uuid.UUID(pid)))
        p = result.scalar_one()
        assert "myapp.com" in p.domain_allowlist
        assert "api.myapp.com" in p.domain_allowlist


async def test_set_allowlist_normalizes_input(session_factory, singleton_user):
    """Mixed scheme/casing/duplicates are collapsed to bare hosts on save."""
    from sqlalchemy import select

    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.models.project import Project
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="normalize.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(ADMIN_ID, flow="set_allowlist", step="value", payload={"project_id": pid})
        await session.commit()

    update, ctx = _make_message(
        chat_id=ADMIN_ID,
        text="https://Foo.com/, foo.com, https://api.foo.com/path, *.staging.foo.com",
    )
    await handle_text_message(update, ctx)

    async with session_factory() as session:
        result = await session.execute(select(Project).where(Project.id == uuid.UUID(pid)))
        p = result.scalar_one()
        assert list(p.domain_allowlist) == [
            "foo.com",
            "api.foo.com",
            "*.staging.foo.com",
        ]


async def test_allow_all_button_clears_allowlist(session_factory, singleton_user):
    """Pressing 'Allow all' button clears the allowlist."""
    from sqlalchemy import select
    from sqlalchemy import update as sql_update

    from app.bot.handlers.settings import handle_allow_all
    from app.models.project import Project
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="allow-clear.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)
        # Pre-set an allowlist
        await session.execute(
            sql_update(Project).where(Project.id == project.id).values(domain_allowlist=["old.com"])
        )
        await session.commit()

    query = MagicMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = ADMIN_ID
    query.edit_message_text = AsyncMock()

    await handle_allow_all(query, pid, singleton_user.id)

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "cleared" in text.lower() or "all origins" in text.lower()

    async with session_factory() as session:
        result = await session.execute(select(Project).where(Project.id == uuid.UUID(pid)))
        p = result.scalar_one()
        assert p.domain_allowlist == []


async def test_handle_allow_all_rejects_foreign_project(session_factory, singleton_user):
    """A non-owner cannot clear another tenant's domain allowlist."""
    from sqlalchemy import select, text

    from app.bot.handlers.settings import handle_allow_all
    from app.models.project import Project
    from app.models.user import User
    from app.services.projects import create_project

    async with session_factory() as session:
        victim = User(telegram_user_id=999_333)
        session.add(victim)
        await session.flush()
        project, _ = await create_project(
            session, name="victim2.com", admin_chat_id=999_333, owner_user_id=victim.id
        )
        project.domain_allowlist = ["https://victim2.com"]
        await session.commit()
        victim_pid = str(project.id)
        victim_id = victim.id

    query = MagicMock()
    query.message = MagicMock(spec=Message)  # handler asserts isinstance(query.message, Message)
    query.message.chat_id = ADMIN_ID
    query.edit_message_text = AsyncMock()

    await handle_allow_all(query, victim_pid, singleton_user.id)

    async with session_factory() as session:
        row = (
            await session.execute(
                select(Project.domain_allowlist).where(Project.id == uuid.UUID(victim_pid))
            )
        ).scalar_one()
        assert row == ["https://victim2.com"]  # untouched
        await session.execute(
            text("DELETE FROM projects WHERE owner_user_id = :o"), {"o": str(victim_id)}
        )
        await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(victim_id)})
        await session.commit()


async def test_start_set_retention_rejects_foreign_project(session_factory, singleton_user):
    """A caller who does not own the project cannot start the retention flow,
    and no conversation state is saved for them."""
    from app.bot.handlers.settings import start_set_retention
    from app.bot.states import BotStateService
    from app.models.user import User
    from app.services.projects import create_project

    async with session_factory() as session:
        victim = User(telegram_user_id=999_222)
        session.add(victim)
        await session.flush()
        project, _ = await create_project(
            session, name="victim.com", admin_chat_id=999_222, owner_user_id=victim.id
        )
        await session.commit()
        victim_pid = str(project.id)
        victim_id = victim.id

    query = MagicMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = ADMIN_ID
    query.edit_message_text = AsyncMock()

    await start_set_retention(query, victim_pid, singleton_user.id)

    query.edit_message_text.assert_called_once()
    assert "not found" in query.edit_message_text.call_args[0][0].lower()
    async with session_factory() as session:
        svc = BotStateService(session)
        state = await svc.get(ADMIN_ID)
        assert state is None

    async with session_factory() as session:
        from sqlalchemy import text

        await session.execute(
            text("DELETE FROM projects WHERE owner_user_id = :o"), {"o": str(victim_id)}
        )
        await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(victim_id)})
        await session.commit()


async def test_handle_set_retention_text_rejects_foreign_owner(session_factory, singleton_user):
    """The retention text handler re-verifies ownership: an attacker whose
    stashed owner id does not own the target project is refused, the victim's
    retention is left untouched, and the conversation state is cleared."""
    from sqlalchemy import select, text

    from app.bot.handlers.settings import handle_set_retention_text
    from app.bot.states import BotStateService
    from app.models.settings import ProjectSettings
    from app.models.user import User
    from app.services.projects import create_project

    async with session_factory() as session:
        victim = User(telegram_user_id=999_333)
        session.add(victim)
        await session.flush()
        project, _ = await create_project(
            session, name="victim-text.com", admin_chat_id=999_333, owner_user_id=victim.id
        )
        await session.commit()
        victim_pid = str(project.id)
        victim_id = victim.id

    # Attacker (singleton_user) plants conversation state pointing at the
    # victim's project but stamped with their OWN owner id.
    async with session_factory() as session:
        svc = BotStateService(session)
        await svc.save(
            ADMIN_ID,
            flow="set_retention",
            step="value",
            payload={"project_id": victim_pid, "owner_user_id": str(singleton_user.id)},
        )
        await session.commit()

    update, ctx = _make_message(chat_id=ADMIN_ID, text="1")

    async with session_factory() as session:
        svc = BotStateService(session)
        state = await svc.get(ADMIN_ID)
        await handle_set_retention_text(update, session, svc, state)
        # The production auth wrapper commits after the handler returns.
        await session.commit()

    update.message.reply_text.assert_called_once()
    assert "not found" in update.message.reply_text.call_args[0][0].lower()

    async with session_factory() as session:
        # Victim's retention stays at the default (90) — attacker's "1" never landed.
        result = await session.execute(
            select(ProjectSettings).where(ProjectSettings.project_id == uuid.UUID(victim_pid))
        )
        ps = result.scalar_one()
        assert ps.retention_days == 90

        # State was cleared.
        svc = BotStateService(session)
        assert await svc.get(ADMIN_ID) is None

    async with session_factory() as session:
        await session.execute(
            text("DELETE FROM projects WHERE owner_user_id = :o"), {"o": str(victim_id)}
        )
        await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(victim_id)})
        await session.commit()
