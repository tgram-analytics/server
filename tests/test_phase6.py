"""Phase 6 — Telegram bot core tests.

All tests run without hitting the Telegram API.  We build fake Update /
Message / CallbackQuery objects using MagicMock and AsyncMock, then call
handler functions directly and assert on the mocked reply methods.

DB-touching tests use ``db_session`` (for direct queries) together with
``session_factory`` (injected into bot handlers that need their own sessions).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Message

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
    update.callback_query.message = MagicMock(spec=Message)
    update.callback_query.message.photo = ()  # text message, no photo
    update.callback_query.message.chat_id = chat_id
    ctx = MagicMock()
    return update, ctx


# ── system handlers ───────────────────────────────────────────────────────────


async def test_start_replies_with_welcome(singleton_user):
    """Smoke test for /start.

    Phase 3.3: handlers are wrapped in ``@requires_user`` which opens a real
    DB session and resolves the singleton user. Uses the ``singleton_user``
    fixture to bootstrap ``init_db`` state and ``_singleton_user_id``.
    """
    from app.bot.handlers.system import start_command

    update, ctx = _make_update(text="/start")
    await start_command(update, ctx)

    update.message.reply_text.assert_called_once()
    text_arg = update.message.reply_text.call_args[0][0]
    assert "welcome" in text_arg.lower()


async def test_help_lists_commands(singleton_user):
    from app.bot.handlers.system import help_command

    update, ctx = _make_update(text="/help")
    await help_command(update, ctx)

    update.message.reply_text.assert_called_once()
    text_arg = update.message.reply_text.call_args[0][0]
    for cmd in ("/add", "/projects", "/help", "/cancel"):
        assert cmd in text_arg


async def test_cancel_clears_state_and_replies(singleton_user):
    """/cancel must clear bot state and reply with confirmation.

    Phase 3.3: ``cancel_command`` is decorated with ``@requires_user`` which
    owns the session lifecycle, so the previous ``patch("...get_session_factory")``
    no longer applies. The ``singleton_user`` fixture wires the real test DB
    into the module-level session factory; the handler now runs against it.
    """
    from app.bot.handlers.system import cancel_command

    update, ctx = _make_update(text="/cancel")
    await cancel_command(update, ctx)

    update.message.reply_text.assert_called_once()
    assert "cancel" in update.message.reply_text.call_args[0][0].lower()


# ── /add command ──────────────────────────────────────────────────────────────


async def test_add_without_name_sends_usage(singleton_user):
    from app.bot.handlers.projects import add_command

    update, ctx = _make_update(text="/add", args=[])
    await add_command(update, ctx)

    update.message.reply_text.assert_called_once()
    text_arg = update.message.reply_text.call_args[0][0]
    assert "usage" in text_arg.lower() or "/add" in text_arg


async def test_add_creates_project_and_shows_api_key(db_session, singleton_user):
    """Full DB integration: /add stores a project and replies with the key.

    Phase 3.3: ``@requires_user`` owns auth + session lifecycle. The
    ``singleton_user`` fixture wires ``_singleton_user_id`` and the module
    session factory to the test DB, so no patching is needed beyond
    pinning ``webhook_base_url`` for the deterministic snippet text.
    """
    from sqlalchemy import select

    from app.bot.handlers.projects import add_command
    from app.models.project import Project

    # Use a UUID suffix so repeated test runs don't produce duplicate names
    unique_name = f"mysite-{uuid.uuid4().hex[:8]}.com"
    update, ctx = _make_update(text=f"/add {unique_name}", args=[unique_name])

    with patch("app.bot.handlers.projects.get_settings") as mock_settings:
        mock_settings.return_value.webhook_base_url = "https://example.com"
        await add_command(update, ctx)

    update.message.reply_text.assert_called_once()
    reply_text = update.message.reply_text.call_args[0][0]
    assert unique_name in reply_text
    assert "proj_" in reply_text  # api key shown once

    # Verify the project row exists (committed data is visible across connections)
    await db_session.invalidate()
    result = await db_session.execute(
        select(Project).where(
            Project.owner_user_id == singleton_user.id, Project.name == unique_name
        )
    )
    assert result.scalar_one_or_none() is not None


# ── /projects command ─────────────────────────────────────────────────────────


async def test_projects_with_no_projects_sends_empty_message(singleton_user):
    """Phase 3.3: project ownership is keyed off ``user.id``. The freshly
    bootstrapped ``singleton_user`` has no projects, so the handler hits the
    empty branch — no admin-chat-id mocking required."""
    from app.bot.handlers.projects import projects_command

    update, ctx = _make_update(text="/projects")
    await projects_command(update, ctx)

    update.message.reply_text.assert_called_once()
    assert "no projects" in update.message.reply_text.call_args[0][0].lower()


async def test_projects_shows_keyboard_when_projects_exist(session_factory, singleton_user):
    from app.bot.handlers.projects import projects_command
    from app.services.projects import create_project

    async with session_factory() as session:
        await create_project(
            session, name="alpha.com", admin_chat_id=ADMIN_ID, owner_user_id=singleton_user.id
        )
        await create_project(
            session, name="beta.com", admin_chat_id=ADMIN_ID, owner_user_id=singleton_user.id
        )
        await session.commit()

    update, ctx = _make_update(text="/projects")
    await projects_command(update, ctx)

    update.message.reply_text.assert_called_once()
    keyboard = update.message.reply_text.call_args[1].get("reply_markup")
    assert keyboard is not None
    button_labels = [row[0].text for row in keyboard.inline_keyboard]
    assert any("alpha.com" in label for label in button_labels)
    assert any("beta.com" in label for label in button_labels)


# ── Callback: project menu ─────────────────────────────────────────────────────


async def test_project_menu_shows_action_buttons(session_factory, singleton_user):
    from app.bot.handlers.projects import project_callback
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="menu-test.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        pid = project.id
        await session.commit()

    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"proj:{pid}")
    await project_callback(update, ctx)

    update.callback_query.edit_message_text.assert_called_once()
    keyboard = update.callback_query.edit_message_text.call_args[1].get("reply_markup")
    assert keyboard is not None
    flat_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("Delete" in label for label in flat_labels)
    assert any("Reports" in label for label in flat_labels)


async def test_delete_confirmation_prompt(singleton_user):
    """Phase 3.3: authorization comes from ``@requires_user`` (singleton cache),
    not ``get_settings().admin_chat_id``. The ``del_ask:`` branch only renders
    a confirmation prompt — it never touches the DB — so we just need the
    decorator to find a valid user.
    """
    from app.bot.handlers.projects import project_callback

    pid = str(uuid.uuid4())
    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"del_ask:{pid}")

    await project_callback(update, ctx)

    update.callback_query.edit_message_text.assert_called_once()
    assert "delete" in update.callback_query.edit_message_text.call_args[0][0].lower()


async def test_confirm_delete_removes_project(db_session, session_factory, singleton_user):
    from sqlalchemy import select

    from app.bot.handlers.projects import project_callback
    from app.models.project import Project
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="to-delete.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        pid = project.id
        await session.commit()

    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"del_yes:{pid}")
    await project_callback(update, ctx)

    update.callback_query.edit_message_text.assert_called_once()
    assert "deleted" in update.callback_query.edit_message_text.call_args[0][0].lower()

    await db_session.invalidate()
    result = await db_session.execute(select(Project).where(Project.id == pid))
    assert result.scalar_one_or_none() is None


async def test_non_admin_callback_is_silently_ignored(singleton_user):
    """Unknown callers must NOT have their callback dispatched.

    Phase 3.3: authorization is now owned by ``@requires_user``. When the
    decorator's ``get_current_user`` returns ``None`` (no resolver could
    authorize the caller — here simulated by blanking the singleton
    cache), the callback is short-circuited with
    ``query.answer("Not authorized")`` and ``edit_message_text`` is
    never invoked.
    """
    from app.bot import auth as auth_mod
    from app.bot.handlers.projects import project_callback

    update, ctx = _make_callback(chat_id=999_888, data=f"proj:{uuid.uuid4()}")

    saved = auth_mod._singleton_user_id
    auth_mod._singleton_user_id = None
    try:
        await project_callback(update, ctx)
    finally:
        auth_mod._singleton_user_id = saved

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
