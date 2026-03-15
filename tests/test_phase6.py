"""Phase 6 — Telegram bot core tests.

All tests run without hitting the Telegram API.  We build fake Update /
Message / CallbackQuery objects using MagicMock and AsyncMock, then call
handler functions directly and assert on the mocked reply methods.

DB-touching tests use ``db_session`` (for direct queries) together with
``session_factory`` (injected into bot handlers that need their own sessions).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

# ── helpers ───────────────────────────────────────────────────────────────────

ADMIN_ID = 111


def _make_update(chat_id: int = ADMIN_ID, text: str = "/start", args: list[str] | None = None):
    """Build a minimal fake message Update."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = chat_id
    update.message.reply_text = AsyncMock()
    update.message.text = text
    update.callback_query = None
    ctx = MagicMock()
    ctx.args = args or []
    return update, ctx


def _make_callback(chat_id: int = ADMIN_ID, data: str = "proj:some-uuid"):
    """Build a minimal fake CallbackQuery Update."""
    update = MagicMock()
    update.effective_user.id = chat_id
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    ctx = MagicMock()
    return update, ctx


# ── system handlers ───────────────────────────────────────────────────────────


async def test_start_replies_with_welcome():
    from app.bot.handlers.system import start_command

    update, ctx = _make_update(text="/start")
    await start_command(update, ctx)

    update.message.reply_text.assert_called_once()
    text_arg = update.message.reply_text.call_args[0][0]
    assert "welcome" in text_arg.lower()


async def test_help_lists_commands():
    from app.bot.handlers.system import help_command

    update, ctx = _make_update(text="/help")
    await help_command(update, ctx)

    update.message.reply_text.assert_called_once()
    text_arg = update.message.reply_text.call_args[0][0]
    for cmd in ("/add", "/projects", "/help", "/cancel"):
        assert cmd in text_arg


async def test_cancel_clears_state_and_replies():
    from app.bot.handlers.system import cancel_command

    update, ctx = _make_update(text="/cancel")

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = AsyncMock(return_value=None)

    with patch("app.bot.handlers.system.get_session_factory", return_value=lambda: mock_session):
        await cancel_command(update, ctx)

    update.message.reply_text.assert_called_once()
    assert "cancel" in update.message.reply_text.call_args[0][0].lower()


# ── /add command ──────────────────────────────────────────────────────────────


async def test_add_without_name_sends_usage():
    from app.bot.handlers.projects import add_command

    update, ctx = _make_update(text="/add", args=[])
    await add_command(update, ctx)

    update.message.reply_text.assert_called_once()
    text_arg = update.message.reply_text.call_args[0][0]
    assert "usage" in text_arg.lower() or "/add" in text_arg


async def test_add_creates_project_and_shows_api_key(db_session, session_factory):
    """Full DB integration: /add stores a project and replies with the key."""
    from sqlalchemy import select

    from app.bot.handlers.projects import add_command
    from app.models.project import Project

    # Use a UUID suffix so repeated test runs don't produce duplicate names
    unique_name = f"mysite-{uuid.uuid4().hex[:8]}.com"
    update, ctx = _make_update(text=f"/add {unique_name}", args=[unique_name])

    with (
        patch("app.bot.handlers.projects.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.projects.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
        mock_settings.return_value.webhook_base_url = "https://example.com"
        await add_command(update, ctx)

    update.message.reply_text.assert_called_once()
    reply_text = update.message.reply_text.call_args[0][0]
    assert unique_name in reply_text
    assert "proj_" in reply_text  # api key shown once

    # Verify the project row exists (committed data is visible across connections)
    await db_session.invalidate()
    result = await db_session.execute(
        select(Project).where(Project.admin_chat_id == ADMIN_ID, Project.name == unique_name)
    )
    assert result.scalar_one_or_none() is not None


# ── /projects command ─────────────────────────────────────────────────────────


async def test_projects_with_no_projects_sends_empty_message(db_session, session_factory):
    from app.bot.handlers.projects import projects_command

    update, ctx = _make_update(text="/projects")

    with (
        patch("app.bot.handlers.projects.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.projects.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = 999_999  # no projects for this chat id
        await projects_command(update, ctx)

    update.message.reply_text.assert_called_once()
    assert "no projects" in update.message.reply_text.call_args[0][0].lower()


async def test_projects_shows_keyboard_when_projects_exist(db_session, session_factory):
    from app.bot.handlers.projects import projects_command
    from app.services.projects import create_project

    async with session_factory() as session:
        await create_project(session, name="alpha.com", admin_chat_id=ADMIN_ID)
        await create_project(session, name="beta.com", admin_chat_id=ADMIN_ID)
        await session.commit()

    update, ctx = _make_update(text="/projects")

    with (
        patch("app.bot.handlers.projects.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.projects.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
        await projects_command(update, ctx)

    update.message.reply_text.assert_called_once()
    keyboard = update.message.reply_text.call_args[1].get("reply_markup")
    assert keyboard is not None
    button_labels = [row[0].text for row in keyboard.inline_keyboard]
    assert any("alpha.com" in label for label in button_labels)
    assert any("beta.com" in label for label in button_labels)


# ── Callback: project menu ─────────────────────────────────────────────────────


async def test_project_menu_shows_action_buttons(db_session, session_factory):
    from app.bot.handlers.projects import project_callback
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="menu-test.com", admin_chat_id=ADMIN_ID)
        pid = project.id
        await session.commit()

    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"proj:{pid}")

    with (
        patch("app.bot.handlers.projects.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.projects.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
        await project_callback(update, ctx)

    update.callback_query.edit_message_text.assert_called_once()
    keyboard = update.callback_query.edit_message_text.call_args[1].get("reply_markup")
    assert keyboard is not None
    flat_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("Delete" in label for label in flat_labels)
    assert any("Reports" in label for label in flat_labels)


async def test_delete_confirmation_prompt():
    from app.bot.handlers.projects import project_callback

    pid = str(uuid.uuid4())
    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"del_ask:{pid}")

    with patch("app.bot.handlers.projects.get_settings") as mock_settings:
        mock_settings.return_value.admin_chat_id = ADMIN_ID
        await project_callback(update, ctx)

    update.callback_query.edit_message_text.assert_called_once()
    assert "delete" in update.callback_query.edit_message_text.call_args[0][0].lower()


async def test_confirm_delete_removes_project(db_session, session_factory):
    from sqlalchemy import select

    from app.bot.handlers.projects import project_callback
    from app.models.project import Project
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="to-delete.com", admin_chat_id=ADMIN_ID)
        pid = project.id
        await session.commit()

    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"del_yes:{pid}")

    with (
        patch("app.bot.handlers.projects.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.projects.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
        await project_callback(update, ctx)

    update.callback_query.edit_message_text.assert_called_once()
    assert "deleted" in update.callback_query.edit_message_text.call_args[0][0].lower()

    await db_session.invalidate()
    result = await db_session.execute(select(Project).where(Project.id == pid))
    assert result.scalar_one_or_none() is None


async def test_non_admin_callback_is_silently_ignored():
    """Callbacks from non-admin users must be dropped (no message edit)."""
    from app.bot.handlers.projects import project_callback

    update, ctx = _make_callback(chat_id=999_888, data=f"proj:{uuid.uuid4()}")

    with patch("app.bot.handlers.projects.get_settings") as mock_settings:
        mock_settings.return_value.admin_chat_id = ADMIN_ID
        await project_callback(update, ctx)

    update.callback_query.answer.assert_called_once()
    update.callback_query.edit_message_text.assert_not_called()


# ── Webhook endpoint ───────────────────────────────────────────────────────────


async def test_webhook_wrong_token_returns_403(client):
    resp = await client.post("/webhook/wrong-token", json={"update_id": 1})
    assert resp.status_code == 403


async def test_webhook_correct_token_dispatches_update(client):
    """A POST with the correct token is accepted and process_update is called.

    The ``client`` fixture sets TELEGRAM_BOT_TOKEN to the value below, so
    that is the token FastAPI's DI resolves from get_settings().
    """
    from app.bot import setup as bot_setup

    # Must match the token set by the ``client`` fixture in conftest
    TEST_TOKEN = "1234567890:test-token-for-testing-only"

    mock_app = MagicMock()
    mock_app.bot = MagicMock()
    mock_app.process_update = AsyncMock()

    with (
        patch.object(bot_setup, "_application", mock_app),
        patch("app.api.webhook.Update") as mock_update_cls,
    ):
        mock_update_cls.de_json = MagicMock(return_value=MagicMock())
        resp = await client.post(f"/webhook/{TEST_TOKEN}", json={"update_id": 42})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mock_app.process_update.assert_called_once()
