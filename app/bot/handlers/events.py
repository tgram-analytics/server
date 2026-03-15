"""Event browsing handlers: /events command and per-event actions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.bot.states import BotStateService
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.services.analytics import count_events, events_over_time, list_event_names
from app.services.charts import ChartGenerationError, generate_line_chart
from app.services.projects import get_project, list_projects

# ── /events command ──────────────────────────────────────────────────────────


async def events_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show project list so the user can pick one to browse events."""
    assert update.message is not None
    settings = get_settings()

    factory = get_session_factory()
    async with factory() as session:
        projects = await list_projects(session, settings.admin_chat_id)

    if not projects:
        await update.message.reply_text(
            "📭 No projects yet.\n\nUse /add <i>name</i> to create one.",
            parse_mode="HTML",
        )
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"📊 {p.name}", callback_data=f"menu:events:{p.id}")]
            for p in projects
        ]
    )
    await update.message.reply_text("Select a project to browse events:", reply_markup=keyboard)


# ── Callback dispatcher ─────────────────────────────────────────────────────


async def events_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all event-browsing callbacks."""
    query = update.callback_query
    assert query is not None
    await query.answer()

    settings = get_settings()
    admin_chat_id = settings.admin_chat_id

    if update.effective_user is None or update.effective_user.id != admin_chat_id:
        return

    data: str = query.data or ""

    if data == "back:events":
        await _show_events_list_from_state(query, admin_chat_id)

    elif data.startswith("evt:"):
        event_name = data[4:]
        await _show_event_detail(query, event_name, admin_chat_id)

    elif data == "evta:alert":
        await _start_alert_for_event(query, admin_chat_id)

    elif data == "evta:chart":
        await _send_event_chart(query, admin_chat_id)


# ── Events list ──────────────────────────────────────────────────────────────


async def show_events_menu(query, project_id_str: str, admin_chat_id: int) -> None:
    """Query distinct event names for a project and display them as buttons."""
    pid = uuid.UUID(project_id_str)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, admin_chat_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        events = await list_event_names(session, project_id=pid)

        # Save project_id in conversation state for subsequent callbacks
        svc = BotStateService(session)
        await svc.save(
            query.message.chat_id,
            flow="events",
            step="list",
            payload={"project_id": project_id_str},
        )
        await session.commit()

    if not events:
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("« Back", callback_data=f"proj:{project_id_str}")],
            ]
        )
        await query.edit_message_text(
            f"📭 <b>{project.name}</b> — no events received yet.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    for evt in events:
        label = f"{evt['event_name']}  ({evt['count']:,})"
        rows.append([InlineKeyboardButton(label, callback_data=f"evt:{evt['event_name']}")])

    rows.append([InlineKeyboardButton("« Back", callback_data=f"proj:{project_id_str}")])

    keyboard = InlineKeyboardMarkup(rows)
    await query.edit_message_text(
        f"📋 <b>Events for {project.name}</b>\n─────────────────\nTap an event to view details:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def _show_events_list_from_state(query, admin_chat_id: int) -> None:
    """Re-show events list using project_id stored in conversation state."""
    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        state = await svc.get(query.message.chat_id)

    if state is None or state.flow != "events":
        await query.edit_message_text("❌ Session expired. Use /events to start again.")
        return

    project_id_str = (state.payload or {}).get("project_id")
    if not project_id_str:
        await query.edit_message_text("❌ Session expired. Use /events to start again.")
        return

    await show_events_menu(query, project_id_str, admin_chat_id)


# ── Event detail ─────────────────────────────────────────────────────────────


async def _show_event_detail(query, event_name: str, admin_chat_id: int) -> None:
    """Show stats and action buttons for a specific event."""
    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        state = await svc.get(query.message.chat_id)

        if state is None or state.flow != "events":
            await query.edit_message_text("❌ Session expired. Use /events to start again.")
            return

        project_id_str = (state.payload or {}).get("project_id")
        if not project_id_str:
            await query.edit_message_text("❌ Session expired. Use /events to start again.")
            return

        pid = uuid.UUID(project_id_str)
        project = await get_project(session, pid, admin_chat_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        # Compute stats
        now = datetime.now(UTC)
        count_24h = await count_events(
            session,
            project_id=pid,
            event_name=event_name,
            start=now - timedelta(hours=24),
            end=now,
        )
        count_7d = await count_events(
            session,
            project_id=pid,
            event_name=event_name,
            start=now - timedelta(days=7),
            end=now,
        )
        count_30d = await count_events(
            session,
            project_id=pid,
            event_name=event_name,
            start=now - timedelta(days=30),
            end=now,
        )

        # Update state with selected event
        await svc.save(
            query.message.chat_id,
            flow="events",
            step="detail",
            payload={"project_id": project_id_str, "event_name": event_name},
        )
        await session.commit()

    text = (
        f"📋 <b>{event_name}</b>\n"
        f"<i>{project.name}</i>\n"
        f"─────────────────\n"
        f"Last 24h: <b>{count_24h:,}</b>\n"
        f"Last 7d: <b>{count_7d:,}</b>\n"
        f"Last 30d: <b>{count_30d:,}</b>"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔔 Add Alert", callback_data="evta:alert"),
                InlineKeyboardButton("📊 Chart (7d)", callback_data="evta:chart"),
            ],
            [InlineKeyboardButton("« Back to Events", callback_data="back:events")],
        ]
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


# ── Actions ──────────────────────────────────────────────────────────────────


async def _start_alert_for_event(query, admin_chat_id: int) -> None:
    """Transition to the add_alert flow, pre-filled with the selected event name."""
    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        state = await svc.get(query.message.chat_id)

        if state is None or state.flow != "events" or state.step != "detail":
            await query.edit_message_text("❌ Session expired. Use /events to start again.")
            return

        payload = state.payload or {}
        project_id_str = payload.get("project_id")
        event_name = payload.get("event_name")

        if not project_id_str or not event_name:
            await query.edit_message_text("❌ Session expired. Use /events to start again.")
            return

        # Transition to add_alert flow at the condition step (skip event_name input)
        await svc.save(
            query.message.chat_id,
            flow="add_alert",
            step="condition",
            payload={"project_id": project_id_str, "event_name": event_name},
        )
        await session.commit()

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Every", callback_data="alert_cond:every"),
                InlineKeyboardButton("Every N", callback_data="alert_cond:every_n"),
                InlineKeyboardButton("Threshold", callback_data="alert_cond:threshold"),
            ]
        ]
    )
    await query.edit_message_text(
        f"📝 <b>Add Alert</b>\n\n"
        f"Event: <b>{event_name}</b>\n\n"
        f"Choose when to notify:\n"
        f"• <b>Every</b> — on every occurrence\n"
        f"• <b>Every N</b> — every Nth occurrence\n"
        f"• <b>Threshold</b> — when count exceeds N per day",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def _send_event_chart(query, admin_chat_id: int) -> None:
    """Generate and send a 7-day line chart for the selected event."""
    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        state = await svc.get(query.message.chat_id)

        if state is None or state.flow != "events" or state.step != "detail":
            await query.edit_message_text("❌ Session expired. Use /events to start again.")
            return

        payload = state.payload or {}
        project_id_str = payload.get("project_id")
        event_name = payload.get("event_name")

        if not project_id_str or not event_name:
            await query.edit_message_text("❌ Session expired. Use /events to start again.")
            return

        pid = uuid.UUID(project_id_str)
        project = await get_project(session, pid, admin_chat_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        now = datetime.now(UTC)
        seven_days_ago = now - timedelta(days=7)
        data = await events_over_time(
            session,
            project_id=pid,
            event_name=event_name,
            start=seven_days_ago,
            end=now,
            granularity="day",
        )

    back_keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("« Back", callback_data="back:events")],
        ]
    )

    if not data:
        await query.edit_message_text(
            f"📭 No data for <b>{event_name}</b> in the last 7 days.",
            parse_mode="HTML",
            reply_markup=back_keyboard,
        )
        return

    settings = get_settings()
    try:
        png_bytes = await generate_line_chart(
            data,
            title=event_name,
            period_label="Last 7 days",
            quickchart_url=settings.quickchart_url,
        )
    except ChartGenerationError:
        await query.edit_message_text(
            "⚠️ Chart service unavailable. Please try again later.",
            reply_markup=back_keyboard,
        )
        return

    await query.edit_message_text(
        f"📊 <b>{event_name}</b> — last 7 days  ↓",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("« Back to Events", callback_data="back:events")],
            ]
        ),
    )
    await query.message.reply_photo(
        photo=png_bytes,
        caption=f"📈 {project.name} · {event_name} · last 7 days",
    )
