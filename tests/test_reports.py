"""Tests for the 📈 Reports bot handler."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Message

ADMIN_ID = 111


def _make_callback(chat_id: int = ADMIN_ID, data: str = "menu:reports:some-uuid"):
    update = MagicMock()
    update.effective_user.id = chat_id
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.message.chat_id = chat_id
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()
    ctx = MagicMock()
    return update, ctx


# ── show_reports_menu ─────────────────────────────────────────────────────────


async def test_show_reports_menu_no_events(session_factory, singleton_user):
    """Report shows zero counts when no events exist."""
    from app.bot.handlers.reports import show_reports_menu
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="empty-report.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

    query = MagicMock()
    query.edit_message_text = AsyncMock()

    await show_reports_menu(query, pid, singleton_user.id)

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "empty-report.com" in text
    assert "Total events:" in text
    assert "0" in text
    assert "No events" in text


async def test_show_reports_menu_with_events(session_factory, singleton_user):
    """Report shows correct counts and top events when data exists."""
    from app.bot.handlers.reports import show_reports_menu
    from app.models.event import Event
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="busy-report.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = project.id

        # Insert 5 pageviews and 2 signups
        for i in range(5):
            session.add(
                Event(
                    project_id=pid,
                    event_name="pageview",
                    session_id=f"s{i}",
                    properties={},
                    timestamp=datetime.now(UTC) - timedelta(hours=i),
                )
            )
        for i in range(2):
            session.add(
                Event(
                    project_id=pid,
                    event_name="signup",
                    session_id=f"u{i}",
                    properties={},
                    timestamp=datetime.now(UTC) - timedelta(hours=i),
                )
            )
        await session.commit()

    query = MagicMock()
    query.edit_message_text = AsyncMock()

    await show_reports_menu(query, str(pid), singleton_user.id)

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "busy-report.com" in text
    assert "7" in text  # total = 7
    assert "pageview" in text
    assert "signup" in text

    # Check "View Chart" button exists
    keyboard = query.edit_message_text.call_args[1].get("reply_markup")
    assert keyboard is not None
    flat = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("Chart" in label for label in flat)


async def test_show_reports_menu_project_not_found(session_factory, singleton_user):
    """Returns error message if project doesn't belong to user."""
    from app.bot.handlers.reports import show_reports_menu

    query = MagicMock()
    query.edit_message_text = AsyncMock()
    fake_pid = str(uuid.uuid4())

    await show_reports_menu(query, fake_pid, singleton_user.id)

    text = query.edit_message_text.call_args[0][0]
    assert "not found" in text.lower()


# ── send_chart_photo ──────────────────────────────────────────────────────────


async def test_send_chart_photo_no_data(session_factory, singleton_user):
    """Shows 'no data' message when project has no events."""
    from app.bot.handlers.reports import send_chart_photo
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="empty-chart.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock(spec=Message)
    query.message.reply_photo = AsyncMock()

    with patch("app.bot.handlers.reports.get_settings") as mock_cfg:
        mock_cfg.return_value.quickchart_url = "http://quickchart:3400"
        await send_chart_photo(query, pid, singleton_user.id)

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "No event data" in text
    query.message.reply_photo.assert_not_called()


async def test_send_chart_photo_quickchart_unavailable(session_factory, singleton_user):
    """Shows error gracefully when QuickChart is unreachable."""
    from app.bot.handlers.reports import send_chart_photo
    from app.models.event import Event
    from app.services.charts import ChartGenerationError
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="chart-fail.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = project.id
        session.add(
            Event(
                project_id=pid,
                event_name="pageview",
                session_id="s1",
                properties={},
                timestamp=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await session.commit()

    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock(spec=Message)
    query.message.reply_photo = AsyncMock()

    with (
        patch("app.bot.handlers.reports.get_settings") as mock_cfg,
        patch(
            "app.bot.handlers.reports.generate_line_chart", side_effect=ChartGenerationError("down")
        ),
    ):
        mock_cfg.return_value.quickchart_url = "http://quickchart:3400"
        await send_chart_photo(query, str(pid), singleton_user.id)

    text = query.edit_message_text.call_args[0][0]
    assert "unavailable" in text.lower()
    query.message.reply_photo.assert_not_called()


async def test_send_chart_photo_success(session_factory, singleton_user):
    """Sends PNG photo when QuickChart returns successfully."""
    from app.bot.handlers.reports import send_chart_photo
    from app.models.event import Event
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="chart-ok.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = project.id
        session.add(
            Event(
                project_id=pid,
                event_name="pageview",
                session_id="s1",
                properties={},
                timestamp=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await session.commit()

    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock(spec=Message)
    query.message.reply_photo = AsyncMock()

    fake_png = b"\x89PNG\r\n\x1a\nfakepng"

    with (
        patch("app.bot.handlers.reports.get_settings") as mock_cfg,
        patch("app.bot.handlers.reports.generate_line_chart", return_value=fake_png) as mock_chart,
    ):
        mock_cfg.return_value.quickchart_url = "http://quickchart:3400"
        await send_chart_photo(query, str(pid), singleton_user.id)

    mock_chart.assert_called_once()
    query.message.reply_photo.assert_called_once()
    call_kwargs = query.message.reply_photo.call_args
    photo_arg = call_kwargs[1].get("photo") or (call_kwargs[0][0] if call_kwargs[0] else None)
    assert photo_arg == fake_png
