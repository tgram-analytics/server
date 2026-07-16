"""Event browsing handlers: /events command and per-event actions."""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    Update,
)
from telegram.ext import ContextTypes

from app.bot.auth import requires_user
from app.bot.constants import escape_photo
from app.bot.states import BotStateService
from app.core.database import get_session_factory
from app.models.user import User
from app.services.analytics import (
    compare_periods,
    count_events,
    events_over_time,
    list_event_names,
    list_property_keys,
    list_recent_events,
    top_properties,
)
from app.services.charts import (
    ChartGenerationError,
    generate_comparison_chart,
    generate_line_chart,
    generate_pie_chart,
)
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


@requires_user
async def events_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """Show project list so the user can pick one to browse events."""
    assert update.message is not None

    projects = await list_projects(session, user.id)

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


@requires_user
async def events_callback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """Handle all event-browsing callbacks."""
    query = update.callback_query
    assert query is not None
    await query.answer()

    owner_user_id = user.id
    data: str = query.data or ""

    if data == "back:events":
        await _show_events_list_from_state(await escape_photo(query), owner_user_id)

    elif data.startswith("evt:"):
        event_name = data[4:]
        await _show_event_detail(query, event_name, owner_user_id)

    elif data == "evta:alert":
        await _start_alert_for_event(query, owner_user_id)

    elif data == "evta:chart":
        await _send_event_chart(query, owner_user_id)

    elif data.startswith("evta:prd:"):
        # evta:prd:{period}:{gran}
        parts = data[9:].split(":", 1)
        if len(parts) == 2:
            await _update_event_chart(query, owner_user_id, period=parts[0], gran=parts[1])

    elif data.startswith("evta:cmp:"):
        # evta:cmp:{period}:{gran}
        parts = data[9:].split(":", 1)
        if len(parts) == 2:
            await _send_event_comparison(query, owner_user_id, period=parts[0], gran=parts[1])

    elif data == "evta:pie":
        await _show_pie_property_picker(await escape_photo(query), owner_user_id)

    elif data.startswith("evta:pie_k:"):
        prop_key = data[11:]
        await _send_event_pie_chart(query, owner_user_id, prop_key)

    elif data == "evta:pie_all":
        await _send_full_pie_charts(query, owner_user_id)


# ── Events list ────────────────────────────────────────────────────────────────


async def show_events_menu(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Query distinct event names for a project and display them as buttons."""
    assert isinstance(query.message, Message)
    pid = uuid.UUID(project_id_str)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
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
            f"📭 <b>{html.escape(project.name)}</b> — no events received yet.",
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
        f"📋 <b>Events for {html.escape(project.name)}</b>\n─────────────────\nTap an event to view details:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _show_events_list_from_state(query: CallbackQuery, owner_user_id: uuid.UUID) -> None:
    """Re-show events list using project_id stored in conversation state."""
    assert isinstance(query.message, Message)
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

    await show_events_menu(query, project_id_str, owner_user_id)


# ── History (recent activity) ──────────────────────────────────────────────────


_HISTORY_LIMIT = 50


def _format_relative(now: datetime, ts: datetime) -> str:
    """Compact relative time: '3s', '5m', '2h', '4d'."""
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{max(secs, 0)}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


class _HistoryGroup(TypedDict):
    event_name: str
    count: int
    latest: datetime


def _group_consecutive(events: list[dict[str, Any]]) -> list[_HistoryGroup]:
    """Run-length encode consecutive events with the same name.

    Returns groups preserving input order.
    """
    grouped: list[_HistoryGroup] = []
    for evt in events:
        name: str = evt["event_name"]
        ts: datetime = evt["timestamp"]
        if grouped and grouped[-1]["event_name"] == name:
            grouped[-1]["count"] += 1
        else:
            grouped.append({"event_name": name, "count": 1, "latest": ts})
    return grouped


async def show_history_menu(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Show the last N events for a project, grouping consecutive duplicates."""
    assert isinstance(query.message, Message)
    pid = uuid.UUID(project_id_str)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        events = await list_recent_events(session, project_id=pid, limit=_HISTORY_LIMIT)

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"menu:history:{project_id_str}")],
            [InlineKeyboardButton("« Back", callback_data=f"proj:{project_id_str}")],
        ]
    )

    if not events:
        await query.edit_message_text(
            f"🕘 <b>{html.escape(project.name)}</b> — no events received yet.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    now = datetime.now(UTC)
    groups = _group_consecutive(events)

    lines = [
        f"🕘 <b>Recent activity — {html.escape(project.name)}</b>",
        f"<i>last {len(events)} event{'s' if len(events) != 1 else ''}</i>",
        "─────────────────",
    ]
    for g in groups:
        name = html.escape(g["event_name"])
        count = g["count"]
        rel = _format_relative(now, g["latest"])
        prefix = f"<b>({count})</b> " if count > 1 else ""
        lines.append(f"{prefix}{name}  <i>· {rel}</i>")

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ── Event detail ───────────────────────────────────────────────────────────────


async def _show_event_detail(
    query: CallbackQuery, event_name: str, owner_user_id: uuid.UUID
) -> None:
    """Show stats and action buttons for a specific event."""
    assert isinstance(query.message, Message)
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
        project = await get_project(session, pid, owner_user_id)
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
        f"📋 <b>{html.escape(event_name)}</b>\n"
        f"<i>{html.escape(project.name)}</i>\n"
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
            [InlineKeyboardButton("🥧 Specific Pie Chart", callback_data="evta:pie")],
            [InlineKeyboardButton("🥧📊 Full Pie Charts", callback_data="evta:pie_all")],
            [InlineKeyboardButton("« Back to Events", callback_data="back:events")],
        ]
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


# ── Actions ────────────────────────────────────────────────────────────────────


async def _start_alert_for_event(query: CallbackQuery, owner_user_id: uuid.UUID) -> None:
    """Transition to the add_alert flow, pre-filled with the selected event name."""
    assert isinstance(query.message, Message)
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
            payload={
                "project_id": project_id_str,
                "event_name": event_name,
                "owner_user_id": str(owner_user_id),
            },
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
        f"Event: <b>{html.escape(event_name)}</b>\n\n"
        f"Choose when to notify:\n"
        f"• <b>Every</b> — on every occurrence\n"
        f"• <b>Every N</b> — every Nth occurrence\n"
        f"• <b>Threshold</b> — when count exceeds N per day",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def _send_event_chart(
    query: CallbackQuery, owner_user_id: uuid.UUID, period: str = "7d", gran: str = "day"
) -> None:
    """Generate and send a line chart for the selected event as a new photo reply."""
    assert isinstance(query.message, Message)
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
        project = await get_project(session, pid, owner_user_id)
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
            f"📭 No data for <b>{html.escape(event_name)}</b> in the {period_label}.",
            parse_mode="HTML",
            reply_markup=back_keyboard,
        )
        return

    try:
        png_bytes = await generate_line_chart(
            data,
            title=event_name,
            period_label=period_label,
        )
    except ChartGenerationError:
        await query.edit_message_text(
            "⚠️ Chart service unavailable. Please try again later.",
            reply_markup=back_keyboard,
        )
        return

    await query.edit_message_text(
        f"📊 <b>{html.escape(event_name)}</b> — {period_label}  ↓",
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


async def _update_event_chart(
    query: CallbackQuery, owner_user_id: uuid.UUID, period: str, gran: str
) -> None:
    """Edit the existing event chart photo in-place with a new period/granularity."""
    assert isinstance(query.message, Message)
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
        project = await get_project(session, pid, owner_user_id)
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

    try:
        png_bytes = await generate_line_chart(
            data,
            title=event_name,
            period_label=period_label,
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


async def _send_event_comparison(
    query: CallbackQuery, owner_user_id: uuid.UUID, period: str, gran: str
) -> None:
    """Edit the event chart photo to show current vs prior period comparison."""
    assert isinstance(query.message, Message)
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
        project = await get_project(session, pid, owner_user_id)
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

    try:
        png_bytes = await generate_comparison_chart(
            data_current,
            data_previous,
            label_a=f"Current ({period_label})",
            label_b=f"Prior {period_label}",
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


# ── Pie chart ─────────────────────────────────────────────────────────────────


async def _show_pie_property_picker(query: CallbackQuery, owner_user_id: uuid.UUID) -> None:
    """List property keys for the current event so the user can pick one for a pie chart."""
    assert isinstance(query.message, Message)
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
        now = datetime.now(UTC)
        keys = await list_property_keys(
            session,
            project_id=pid,
            event_name=event_name,
            start=now - timedelta(days=30),
            end=now,
        )

    if not keys:
        await query.answer(f"No properties found for {event_name}.", show_alert=True)
        return

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(k, callback_data=f"evta:pie_k:{k}")] for k in keys
    ]
    rows.append([InlineKeyboardButton("« Back", callback_data=f"evt:{event_name}")])

    await query.edit_message_text(
        f"🥧 <b>Pie chart for {html.escape(event_name)}</b>\n\nPick a property to break down:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _send_event_pie_chart(
    query: CallbackQuery, owner_user_id: uuid.UUID, property_key: str
) -> None:
    """Generate and send a pie chart for the selected event + property."""
    assert isinstance(query.message, Message)
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
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        now = datetime.now(UTC)
        rows = await top_properties(
            session,
            project_id=pid,
            event_name=event_name,
            property_key=property_key,
            start=now - timedelta(days=30),
            end=now,
            limit=10,
        )

    back_keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("← Pick another property", callback_data="evta:pie")],
            [InlineKeyboardButton("« Back to Events", callback_data="back:events")],
        ]
    )

    if not rows:
        await query.answer(f"No data for property '{property_key}'.", show_alert=True)
        return

    # Reshape from {"value": ..., "count": ...} to {"source": ..., "count": ...}
    pie_data = [{"source": r["value"], "count": r["count"]} for r in rows]

    try:
        png_bytes = await generate_pie_chart(
            pie_data,
            title=f"{event_name} · {property_key}",
        )
    except ChartGenerationError:
        await query.answer("⚠️ Chart service unavailable.", show_alert=True)
        return

    await query.edit_message_text(
        f"🥧 <b>{html.escape(event_name)}</b> · {html.escape(property_key)}  ↓",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("← Pick another property", callback_data="evta:pie")],
                [InlineKeyboardButton("« Back to Events", callback_data="back:events")],
            ]
        ),
    )
    await query.message.reply_photo(
        photo=png_bytes,
        caption=f"🥧 {project.name} · {event_name} · {property_key}",
        reply_markup=back_keyboard,
    )


async def _send_full_pie_charts(query: CallbackQuery, owner_user_id: uuid.UUID) -> None:
    """Generate one pie chart per property for the current event."""
    assert isinstance(query.message, Message)
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
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        now = datetime.now(UTC)
        start = now - timedelta(days=30)
        end = now

        keys = await list_property_keys(
            session,
            project_id=pid,
            event_name=event_name,
            start=start,
            end=end,
        )

        if not keys:
            await query.answer(f"No properties found for {event_name}.", show_alert=True)
            return

        # Batch all DB reads inside this session, then close it before
        # the (slow, HTTP-bound) chart fan-out.
        key_rows: list[tuple[str, list[dict[str, Any]]]] = []
        for key in keys:
            rows = await top_properties(
                session,
                project_id=pid,
                event_name=event_name,
                property_key=key,
                start=start,
                end=end,
                limit=10,
            )
            if rows:
                key_rows.append((key, rows))

    back_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("« Back to Events", callback_data="back:events")]]
    )

    await query.edit_message_text(
        f"🥧📊 <b>{html.escape(event_name)}</b> · sending {len(key_rows)} chart(s)…",
        parse_mode="HTML",
        reply_markup=back_keyboard,
    )

    if not key_rows:
        await query.edit_message_text(
            f"⚠️ No charts could be generated for {html.escape(event_name)}.",
            parse_mode="HTML",
            reply_markup=back_keyboard,
        )
        return

    # Generate every chart up front (skipping ChartGenerationError silently),
    # then bundle them into Telegram media groups so the chat sees one album
    # card per 10 properties instead of one message per property.
    items: list[tuple[bytes, str]] = []
    for key, rows in key_rows:
        pie_data = [{"source": r["value"], "count": r["count"]} for r in rows]
        try:
            png = await generate_pie_chart(pie_data, title=f"{event_name} · {key}")
        except ChartGenerationError:
            continue
        total = sum(int(r["count"]) for r in rows)
        caption = f"🥧 {project.name} · {event_name} · {key}\n📈 {total:,} events"
        items.append((png, caption))

    if not items:
        await query.edit_message_text(
            f"⚠️ All chart generations failed for {html.escape(event_name)}.",
            parse_mode="HTML",
            reply_markup=back_keyboard,
        )
        return

    # Telegram requires a media group to have 2-10 items. Chunk by 10, and
    # fall back to reply_photo when a chunk has only 1 item.
    for chunk_start in range(0, len(items), 10):
        chunk = items[chunk_start : chunk_start + 10]
        if len(chunk) == 1:
            png, caption = chunk[0]
            await query.message.reply_photo(photo=png, caption=caption)
        else:
            await query.message.reply_media_group(
                media=[InputMediaPhoto(media=p, caption=c) for p, c in chunk]
            )
