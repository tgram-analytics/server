"""Tests for the onboarding flow (system /start branching + onb: callbacks)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.bot.handlers.onboarding import (
    _SDK_RENDERERS,
    _parse_uuid,
    onboarding_callback,
    post_create_keyboard,
    send_first_run_welcome,
)

# ── Pure helpers (no DB / no Telegram) ────────────────────────────────────────


def test_parse_uuid_valid() -> None:
    val = uuid.uuid4()
    assert _parse_uuid(str(val)) == val


def test_parse_uuid_invalid_returns_none() -> None:
    assert _parse_uuid("not-a-uuid") is None
    assert _parse_uuid("") is None


def test_post_create_keyboard_has_test_and_three_sdks() -> None:
    pid = uuid.uuid4()
    kb = post_create_keyboard(pid)
    rows = kb.inline_keyboard
    # Row 0: test event
    assert "test event" in rows[0][0].text.lower()
    assert rows[0][0].callback_data == f"onb:test:{pid}"
    # Row 1: three SDK buttons
    sdks = [b.callback_data for b in rows[1]]
    assert sdks == [
        f"onb:sdk:js:{pid}",
        f"onb:sdk:py:{pid}",
        f"onb:sdk:dart:{pid}",
    ]


def test_sdk_renderers_cover_three_languages() -> None:
    assert set(_SDK_RENDERERS) == {"js", "py", "dart"}


def test_js_snippet_contains_npm_and_init() -> None:
    body = _SDK_RENDERERS["js"]("https://example.com")
    assert "npm install tgram-analytics" in body
    assert "TGA.init" in body
    assert "https://example.com" in body
    assert "&lt;YOUR_API_KEY&gt;" in body  # placeholder, not real key


def test_python_snippet_contains_pip_and_track() -> None:
    body = _SDK_RENDERERS["py"]("https://example.com")
    assert "pip install tgram-analytics" in body
    assert "from tgram_analytics import TGA" in body
    assert "&lt;YOUR_API_KEY&gt;" in body


def test_dart_snippet_contains_pub_add_and_track() -> None:
    body = _SDK_RENDERERS["dart"]("https://example.com")
    assert "flutter pub add tgram_analytics" in body
    assert "&lt;YOUR_API_KEY&gt;" in body


# ── send_first_run_welcome ────────────────────────────────────────────────────


async def test_first_run_welcome_named() -> None:
    msg = AsyncMock()
    await send_first_run_welcome(msg, "Leo")

    msg.reply_text.assert_awaited_once()
    text = msg.reply_text.await_args[0][0]
    assert "Welcome, Leo" in text
    kwargs = msg.reply_text.await_args.kwargs
    assert kwargs["parse_mode"] == "HTML"
    # Has a "create my first project" button
    rows = kwargs["reply_markup"].inline_keyboard
    flat = [b.callback_data for row in rows for b in row]
    assert "onb:create" in flat
    assert "onb:how" in flat


async def test_first_run_welcome_falls_back_to_there() -> None:
    msg = AsyncMock()
    await send_first_run_welcome(msg, "")
    text = msg.reply_text.await_args[0][0]
    assert "Welcome, there" in text


# ── onboarding_callback dispatcher ────────────────────────────────────────────


def _make_query(data: str):
    """Build a callback Update + ctx for the dispatcher."""
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.id = 1
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update, MagicMock()


async def test_onb_create_replies_with_add_instructions(singleton_user) -> None:
    update, ctx = _make_query("onb:create")
    await onboarding_callback(update, ctx)

    update.callback_query.answer.assert_awaited()
    update.callback_query.edit_message_text.assert_awaited_once()
    text = update.callback_query.edit_message_text.await_args[0][0]
    assert "/add" in text


async def test_onb_how_explains_three_steps(singleton_user) -> None:
    update, ctx = _make_query("onb:how")
    await onboarding_callback(update, ctx)

    text = update.callback_query.edit_message_text.await_args[0][0]
    assert "How it works" in text
    # Has the back-to-create button
    kb = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "onb:create" in cbs


async def test_onb_test_with_invalid_uuid_replies_error(singleton_user) -> None:
    update, ctx = _make_query("onb:test:not-a-uuid")
    await onboarding_callback(update, ctx)
    text = update.callback_query.edit_message_text.await_args[0][0]
    assert "Invalid" in text


async def test_onb_test_fires_event_for_owned_project(
    db_session, session_factory, singleton_user
) -> None:
    """End-to-end: test-event button inserts an Event row."""
    from app.services.events import insert_event  # noqa: F401 - sanity import
    from app.services.projects import create_project

    # Create a project owned by the singleton user.
    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="onb-test.com",
            admin_chat_id=singleton_user.telegram_user_id,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        project_id = project.id

    update, ctx = _make_query(f"onb:test:{project_id}")
    await onboarding_callback(update, ctx)

    # The handler should have replied with a "Test event sent" message.
    text = update.callback_query.edit_message_text.await_args[0][0]
    assert "Test event sent" in text
    assert "onb-test.com" in text

    # And inserted exactly one Event row with event_name='test'.
    from sqlalchemy import select

    from app.models.event import Event

    async with session_factory() as session:
        result = await session.execute(select(Event).where(Event.project_id == project_id))
        events = list(result.scalars().all())
        assert len(events) == 1
        assert events[0].event_name == "test"
        assert events[0].session_id == "bot-onboarding"


async def test_onb_test_for_non_owned_project_replies_not_found(
    db_session, session_factory, singleton_user
) -> None:
    """User can't fire a test event for a project they don't own."""
    fake_project_id = uuid.uuid4()
    update, ctx = _make_query(f"onb:test:{fake_project_id}")
    await onboarding_callback(update, ctx)

    text = update.callback_query.edit_message_text.await_args[0][0]
    assert "not found" in text.lower() or "no longer yours" in text.lower()


async def test_onb_sdk_unknown_lang_replies_error(singleton_user) -> None:
    pid = uuid.uuid4()
    update, ctx = _make_query(f"onb:sdk:rust:{pid}")
    await onboarding_callback(update, ctx)
    text = update.callback_query.edit_message_text.await_args[0][0]
    assert "Invalid" in text


async def test_onb_sdk_renders_snippet_for_owned_project(
    db_session, session_factory, singleton_user
) -> None:
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="onb-sdk.com",
            admin_chat_id=singleton_user.telegram_user_id,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        project_id = project.id

    update, ctx = _make_query(f"onb:sdk:js:{project_id}")
    await onboarding_callback(update, ctx)

    text = update.callback_query.edit_message_text.await_args[0][0]
    assert "JavaScript" in text
    assert "npm install tgram-analytics" in text
    assert "&lt;YOUR_API_KEY&gt;" in text


async def test_onb_unknown_callback_silently_ignored(singleton_user) -> None:
    """Unknown onb: variants don't crash."""
    update, ctx = _make_query("onb:totally-unknown")
    await onboarding_callback(update, ctx)  # must not raise
