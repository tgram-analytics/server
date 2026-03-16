"""Event browsing handlers: /events command and per-event actions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import ContextTypes

from app.bot.states import BotStateService
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.services.analytics import compare_periods, count_events, events_over_time, list_event_names
from app.services.charts import ChartGenerationError, generate_comparison_chart, generate_line_chart
from app.services.projects import get_project, list_projects

# ── Constants ─────────────────────────────────────────────────────────────────

_PERIODS: dict[str, timedelta] = {
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}

_PERIOD_LABEL: dict[str, str] = {
    "7d": "last 7 days",
    "30d": "last 30 days",
    "90d": "last 90 days",
}


# ── Keyboard helper ────────────────────────────────────────────────────────────


def _event_chart_keyboard(period: str, gran: str) -> InlineKeyboardMarkup:
    """Inline keyboard for a per-event chart photo.

    Period switching and comparison use session state for project_id/event_name.
    """
    period_row = [
        InlineKeyboardButton(
            f"✓ {p}" if p == period else p,
            callback_data=f"evta:prd:{p}:{gran}",
        )
        for p in _PERIODS
    ]
    gran_row = [
        InlineKeyboardButton(
            f"✓ by {g}" if g == gran else f"by {g}",
            callback_data=f"evta:prd:{period}:{g}",
        )
        for g in ("day", "week")
    ]
    return InlineKeyboardMarkup(
        [
            period_row,
            gran_row,
            [
                InlineKeyboardButton(
                    "⚖️ Compare vs prior period", callback_data=f"evta:cmp:{period}:{gran}"
                )
            ],
            [InlineKeyboardButton("« Back to Events", callback_data="back:events")],
        ]
    )


# ── /events command ────────────────────────────────────────────────────────────


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


# ── Callback dispatcher ────────────────────────────────────────────────────────


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

    elif data.startswith("evta:prd:"):
        # evta:prd:{period}:{gran}
        parts = data[9:].split(":", 1)
        if len(parts) == 2:
            await _update_event_chart(query, admin_chat_id, period=parts[0], gran=parts[1])

    elif data.startswith("evta:cmp:"):
        # evta:cmp:{period}:{gran}
        parts = data[9:].split(":", 1)
        if len(parts) == 2:
            await _send_event_comparison(query, admin_chat_id, period=parts[0], gran=parts[1])


# ── Events list ────────────────────────────────────────────────────────────────


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
            [[InlineKeyboardButton("« Back", callback_data=f"proj:{project_id_str}")]]
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

    await query.edit_message_text(
        f"📋 <b>Events for {project.name}</b>\n─────────────────\nTap an event to view details:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
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


# ── Event detail ───────────────────────────────────────────────────────────────


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


# ── Actions ────────────────────────────────────────────────────────────────────


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


async def _send_event_chart(
    query, admin_chat_id: int, period: str = "7d", gran: str = "day"
) -> None:
    """Generate and send a line chart for the selected event as a new photo reply."""
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
        data = await events_over_time(
            session,
            project_id=pid,
            event_name=event_name,
            start=now - _PERIODS.get(period, timedelta(days=7)),
            end=now,
            granularity=gran,
        )

    period_label = _PERIOD_LABEL.get(period, period)
    back_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("« Back to Events", callback_data="back:events")]]
    )

    if not data:
        await query.edit_message_text(
            f"📭 No data for <b>{event_name}</b> in the {period_label}.",
            parse_mode="HTML",
            reply_markup=back_keyboard,
        )
        return

    settings = get_settings()
    try:
        png_bytes = await generate_line_chart(
            data,
            title=event_name,
            period_label=period_label,
            quickchart_url=settings.quickchart_url,
        )
    except ChartGenerationError:
        await query.edit_message_text(
            "⚠️ Chart service unavailable. Please try again later.",
            reply_markup=back_keyboard,
        )
        return

    await query.edit_message_text(
        f"📊 <b>{event_name}</b> — {period_label}  ↓",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Back to Events", callback_data="back:events")]]
        ),
    )
    await query.message.reply_photo(
        photo=png_bytes,
        caption=f"📈 {project.name} · {event_name} · {period_label}",
        reply_markup=_event_chart_keyboard(period, gran),
    )


async def _update_event_chart(query, admin_chat_id: int, period: str, gran: str) -> None:
    """Edit the existing event chart photo in-place with a new period/granularity."""
    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        state = await svc.get(query.message.chat_id)

        if state is None or state.flow != "events" or state.step != "detail":
            await query.answer("❌ Session expired.", show_alert=True)
            return

        payload = state.payload or {}
        project_id_str = payload.get("project_id")
        event_name = payload.get("event_name")

        if not project_id_str or not event_name:
            await query.answer("❌ Session expired.", show_alert=True)
            return

        pid = uuid.UUID(project_id_str)
        project = await get_project(session, pid, admin_chat_id)
        if project is None:
            await query.answer("❌ Project not found.", show_alert=True)
            return

        now = datetime.now(UTC)
        data = await events_over_time(
            session,
            project_id=pid,
            event_name=event_name,
            start=now - _PERIODS.get(period, timedelta(days=7)),
            end=now,
            granularity=gran,
        )

    period_label = _PERIOD_LABEL.get(period, period)

    if not data:
        await query.answer(f"No data for {period_label}.", show_alert=True)
        return

    settings = get_settings()
    try:
        png_bytes = await generate_line_chart(
            data,
            title=event_name,
            period_label=period_label,
            quickchart_url=settings.quickchart_url,
        )
    except ChartGenerationError:
        await query.answer("⚠️ Chart service unavailable.", show_alert=True)
        return

    await query.edit_message_media(
        media=InputMediaPhoto(
            media=png_bytes,
            caption=f"📈 {project.name} · {event_name} · {period_label}",
        ),
        reply_markup=_event_chart_keyboard(period, gran),
    )


async def _send_event_comparison(query, admin_chat_id: int, period: str, gran: str) -> None:
    """Edit the event chart photo to show current vs prior period comparison."""
    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        state = await svc.get(query.message.chat_id)

        if state is None or state.flow != "events" or state.step != "detail":
            await query.answer("❌ Session expired.", show_alert=True)
            return

        payload = state.payload or {}
        project_id_str = payload.get("project_id")
        event_name = payload.get("event_name")

        if not project_id_str or not event_name:
            await query.answer("❌ Session expired.", show_alert=True)
            return

        pid = uuid.UUID(project_id_str)
        project = await get_project(session, pid, admin_chat_id)
        if project is None:
            await query.answer("❌ Project not found.", show_alert=True)
            return

        delta = _PERIODS.get(period, timedelta(days=7))
        now = datetime.now(UTC)
        current_start = now - delta
        previous_start = current_start - delta

        data_current = await events_over_time(
            session,
            project_id=pid,
            event_name=event_name,
            start=current_start,
            end=now,
            granularity=gran,
        )
        if not data_current:
            await query.answer(f"No data for {_PERIOD_LABEL.get(period, period)}.", show_alert=True)
            return

        data_previous = await events_over_time(
            session,
            project_id=pid,
            event_name=event_name,
            start=previous_start,
            end=current_start,
            granularity=gran,
        )

        cmp = await compare_periods(
            session,
            project_id=pid,
            event_name=event_name,
            current_start=current_start,
            current_end=now,
            previous_start=previous_start,
            previous_end=current_start,
        )

    period_label = _PERIOD_LABEL.get(period, period)
    delta_pct = cmp["delta_pct"]
    if delta_pct is None:
        delta_str = "vs prior period (no prior data)"
    elif delta_pct >= 0:
        delta_str = f"+{delta_pct:.1f}% vs prior period"
    else:
        delta_str = f"{delta_pct:.1f}% vs prior period"

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("← Back to chart", callback_data=f"evta:prd:{period}:{gran}")],
            [InlineKeyboardButton("« Back to Events", callback_data="back:events")],
        ]
    )

    settings = get_settings()
    try:
        png_bytes = await generate_comparison_chart(
            data_current,
            data_previous,
            label_a=f"Current ({period_label})",
            label_b=f"Prior {period_label}",
            quickchart_url=settings.quickchart_url,
        )
    except ChartGenerationError:
        await query.answer("⚠️ Chart service unavailable.", show_alert=True)
        return

    await query.edit_message_media(
        media=InputMediaPhoto(
            media=png_bytes,
            caption=f"📊 {project.name} · {event_name} · {delta_str}",
        ),
        reply_markup=keyboard,
    )
