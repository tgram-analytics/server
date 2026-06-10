"""Tests for the 📦 /export bot handler."""

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from telegram import Message

ADMIN_ID = 111


def _make_message():
    update = MagicMock()
    update.effective_chat.id = ADMIN_ID
    update.effective_user.id = ADMIN_ID
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    ctx = MagicMock()
    return update, ctx


def _make_callback(data: str):
    update = MagicMock()
    update.effective_chat.id = ADMIN_ID
    update.effective_user.id = ADMIN_ID
    update.message = None
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = ADMIN_ID
    query.message.reply_text = AsyncMock()
    query.message.reply_document = AsyncMock()
    update.callback_query = query
    ctx = MagicMock()
    return update, ctx, query


def _parse_csv(call):
    """Decode the CSV bytes passed to reply_document into a list of dicts."""
    document: bytes = call.kwargs["document"]
    reader = csv.DictReader(io.StringIO(document.decode("utf-8")))
    return list(reader)


async def test_export_no_projects(session_factory, singleton_user):
    """No projects → friendly empty-state message."""
    from app.bot.handlers.export import export_command

    update, ctx = _make_message()
    await export_command(update, ctx)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "No projects" in text


async def test_export_shows_project_picker(session_factory, singleton_user):
    """With projects → inline keyboard with one button per project + All projects."""
    from app.bot.handlers.export import export_command
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="picker.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = project.id

    update, ctx = _make_message()
    await export_command(update, ctx)

    update.message.reply_text.assert_called_once()
    keyboard = update.message.reply_text.call_args[1]["reply_markup"]
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert f"exp:{pid}" in callbacks
    assert "exp:all" in callbacks


async def test_export_sends_csv_document(session_factory, singleton_user):
    """Selecting a project sends a CSV document with all event rows."""
    from app.bot.handlers.export import export_callback
    from app.services.events import insert_event
    from app.services.projects import create_project

    ts = datetime.now(UTC) - timedelta(hours=1)
    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="export-me.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = project.id

        await insert_event(
            session,
            project_id=pid,
            event_name="purchase",
            session_id="s1",
            properties={"plan": "pro"},
            timestamp=ts,
        )
        await insert_event(
            session,
            project_id=pid,
            event_name="signup",
            session_id="s2",
            properties={},
            timestamp=ts + timedelta(minutes=5),
        )
        await session.commit()

    update, ctx, query = _make_callback(f"exp:{pid}")
    await export_callback(update, ctx)

    query.message.reply_document.assert_called_once()
    call = query.message.reply_document.call_args
    assert call.kwargs["filename"].endswith(".csv")
    assert "export-me.com" in call.kwargs["filename"]

    rows = _parse_csv(call)
    assert len(rows) == 2
    # Ordered by timestamp ascending.
    assert rows[0]["event_name"] == "purchase"
    assert rows[1]["event_name"] == "signup"
    assert rows[0]["session_id"] == "s1"
    assert json.loads(rows[0]["properties"]) == {"plan": "pro"}
    # All raw-event columns are present.
    expected_cols = {
        "id",
        "event_name",
        "timestamp",
        "received_at",
        "session_id",
        "url",
        "referrer",
        "visitor_hash",
        "browser",
        "os",
        "device_type",
        "properties",
    }
    assert expected_cols <= set(rows[0].keys())


async def test_export_empty_project(session_factory, singleton_user):
    """Project with no events → message instead of a document."""
    from app.bot.handlers.export import export_callback
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="empty.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = project.id

    update, ctx, query = _make_callback(f"exp:{pid}")
    await export_callback(update, ctx)

    query.message.reply_document.assert_not_called()
    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "no events" in text.lower()


async def test_export_all_projects(session_factory, singleton_user):
    """exp:all sends one CSV per project that has events."""
    from app.bot.handlers.export import export_callback
    from app.services.events import insert_event
    from app.services.projects import create_project

    async with session_factory() as session:
        first, _ = await create_project(
            session,
            name="one.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        second, _ = await create_project(
            session,
            name="two.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        # Third project with no events should be skipped, not exported.
        await create_project(
            session,
            name="silent.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()

        for project in (first, second):
            await insert_event(
                session,
                project_id=project.id,
                event_name="pageview",
                session_id="s1",
                properties={},
                timestamp=datetime.now(UTC),
            )
        await session.commit()

    update, ctx, query = _make_callback("exp:all")
    await export_callback(update, ctx)

    assert query.message.reply_document.call_count == 2
    filenames = [c.kwargs["filename"] for c in query.message.reply_document.call_args_list]
    assert any("one.com" in f for f in filenames)
    assert any("two.com" in f for f in filenames)


async def test_export_unknown_project(session_factory, singleton_user):
    """Unknown project id → error message, no document."""
    import uuid

    from app.bot.handlers.export import export_callback

    update, ctx, query = _make_callback(f"exp:{uuid.uuid4()}")
    await export_callback(update, ctx)

    query.message.reply_document.assert_not_called()
    query.edit_message_text.assert_called_once()
    assert "not found" in query.edit_message_text.call_args[0][0].lower()
