"""Tests for the ⚙️ Settings bot handler."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

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


async def test_show_settings_menu_defaults(db_session, session_factory):
    """Settings menu shows default retention (90 days) and open allowlist."""
    from app.bot.handlers.settings import show_settings_menu
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session, name="settings-default.com", admin_chat_id=ADMIN_ID
        )
        await session.commit()
        pid = str(project.id)

    query = MagicMock()
    query.edit_message_text = AsyncMock()

    with patch("app.bot.handlers.settings.get_session_factory", return_value=session_factory):
        await show_settings_menu(query, pid, ADMIN_ID)

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


async def test_show_settings_menu_project_not_found(db_session, session_factory):
    from app.bot.handlers.settings import show_settings_menu

    query = MagicMock()
    query.edit_message_text = AsyncMock()

    with patch("app.bot.handlers.settings.get_session_factory", return_value=session_factory):
        await show_settings_menu(query, str(uuid.uuid4()), ADMIN_ID)

    text = query.edit_message_text.call_args[0][0]
    assert "not found" in text.lower()


# ── set_retention flow ────────────────────────────────────────────────────────


async def test_start_set_retention_saves_state(db_session, session_factory):
    """Tapping ✏️ Retention saves flow state and shows prompt."""
    from app.bot.handlers.settings import start_set_retention
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="ret-flow.com", admin_chat_id=ADMIN_ID)
        await session.commit()
        pid = str(project.id)

    query = MagicMock()
    query.message.chat_id = ADMIN_ID
    query.edit_message_text = AsyncMock()

    with patch("app.bot.handlers.settings.get_session_factory", return_value=session_factory):
        await start_set_retention(query, pid, ADMIN_ID)

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "retention" in text.lower()
    assert "days" in text.lower()


async def test_set_retention_updates_database(db_session, session_factory):
    """Typing a valid number updates ProjectSettings.retention_days."""
    from sqlalchemy import select

    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.models.settings import ProjectSettings
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="ret-update.com", admin_chat_id=ADMIN_ID)
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(ADMIN_ID, flow="set_retention", step="value", payload={"project_id": pid})
        await session.commit()

    update, ctx = _make_message(chat_id=ADMIN_ID, text="30")

    with (
        patch("app.bot.handlers.alerts.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.alerts.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
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


async def test_set_retention_invalid_input(db_session, session_factory):
    """Non-numeric input shows error without updating DB."""
    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="ret-invalid.com", admin_chat_id=ADMIN_ID)
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(ADMIN_ID, flow="set_retention", step="value", payload={"project_id": pid})
        await session.commit()

    update, ctx = _make_message(chat_id=ADMIN_ID, text="not-a-number")

    with (
        patch("app.bot.handlers.alerts.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.alerts.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
        await handle_text_message(update, ctx)

    text = update.message.reply_text.call_args[0][0]
    assert "integer" in text.lower()


async def test_set_retention_forever(db_session, session_factory):
    """Entering 0 sets retention to 'forever'."""
    from sqlalchemy import select

    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.models.settings import ProjectSettings
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="ret-forever.com", admin_chat_id=ADMIN_ID)
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(ADMIN_ID, flow="set_retention", step="value", payload={"project_id": pid})
        await session.commit()

    update, ctx = _make_message(chat_id=ADMIN_ID, text="0")

    with (
        patch("app.bot.handlers.alerts.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.alerts.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
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


async def test_set_allowlist_updates_project(db_session, session_factory):
    """Typing domains updates project.domain_allowlist."""
    from sqlalchemy import select

    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.models.project import Project
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="allow-update.com", admin_chat_id=ADMIN_ID)
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(ADMIN_ID, flow="set_allowlist", step="value", payload={"project_id": pid})
        await session.commit()

    update, ctx = _make_message(chat_id=ADMIN_ID, text="myapp.com, api.myapp.com")

    with (
        patch("app.bot.handlers.alerts.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.alerts.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
        await handle_text_message(update, ctx)

    text = update.message.reply_text.call_args[0][0]
    assert "myapp.com" in text

    async with session_factory() as session:
        result = await session.execute(select(Project).where(Project.id == uuid.UUID(pid)))
        p = result.scalar_one()
        assert "myapp.com" in p.domain_allowlist
        assert "api.myapp.com" in p.domain_allowlist


async def test_allow_all_button_clears_allowlist(db_session, session_factory):
    """Pressing 'Allow all' button clears the allowlist."""
    from sqlalchemy import select
    from sqlalchemy import update as sql_update

    from app.bot.handlers.settings import handle_allow_all
    from app.models.project import Project
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="allow-clear.com", admin_chat_id=ADMIN_ID)
        await session.commit()
        pid = str(project.id)
        # Pre-set an allowlist
        await session.execute(
            sql_update(Project).where(Project.id == project.id).values(domain_allowlist=["old.com"])
        )
        await session.commit()

    query = MagicMock()
    query.message.chat_id = ADMIN_ID
    query.edit_message_text = AsyncMock()

    with patch("app.bot.handlers.settings.get_session_factory", return_value=session_factory):
        await handle_allow_all(query, pid, ADMIN_ID)

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "cleared" in text.lower() or "all origins" in text.lower()

    async with session_factory() as session:
        result = await session.execute(select(Project).where(Project.id == uuid.UUID(pid)))
        p = result.scalar_one()
        assert p.domain_allowlist == []
