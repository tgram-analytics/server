"""Reports handler: analytics summary and on-demand charts.

Supports period switching (7d / 30d / 90d), granularity toggling
(by day / by week), and period-over-period comparison charts.
"""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from app.bot.constants import PERIOD_LABEL, PERIODS
from app.core.database import get_session_factory
from app.models.event import Event
from app.services.analytics import compare_periods, events_over_time
from app.services.charts import ChartGenerationError, generate_comparison_chart, generate_line_chart
from app.services.projects import get_project

# Re-bind module-private aliases so the rest of the file is unchanged.
_PERIODS = PERIODS
_PERIOD_LABEL = PERIOD_LABEL


# ── Keyboard helpers ───────────────────────────────────────────────────────────


def _report_chart_keyboard(project_id_str: str, period: str, gran: str) -> InlineKeyboardMarkup:
    """Inline keyboard attached to a project-level report chart photo."""
    period_row = [
        InlineKeyboardButton(
            f"✓ {p}" if p == period else p,
            callback_data=f"rpt_prd:{project_id_str}:{p}:{gran}",
        )
        for p in _PERIODS
    ]
    gran_row = [
        InlineKeyboardButton(
            f"✓ by {g}" if g == gran else f"by {g}",
            callback_data=f"rpt_prd:{project_id_str}:{period}:{g}",
        )
        for g in ("day", "week")
    ]
    return InlineKeyboardMarkup(
        [
            period_row,
            gran_row,
            [
                InlineKeyboardButton(
                    "⚖️ Compare vs prior period",
                    callback_data=f"rpt_cmp:{project_id_str}:{period}:{gran}",
                )
            ],
            [
                InlineKeyboardButton(
                    "« Back to report", callback_data=f"menu:reports:{project_id_str}"
                )
            ],
        ]
    )


# ── Shared data helper ─────────────────────────────────────────────────────────


async def _get_top_event_data(
    session: AsyncSession,
    project_id: uuid.UUID,
    period: str,
    gran: str,
    now: datetime,
) -> tuple[list[dict[str, Any]], str]:
    """Return (time_series, event_name) for the top event in *period*."""
    start = now - _PERIODS.get(period, timedelta(days=7))

    data = await events_over_time(
        session,
        project_id=project_id,
        event_name="pageview",
        start=start,
        end=now,
        granularity=gran,
    )
    event_name = "pageview"

    if not data:
        top_result = await session.execute(
            select(Event.event_name, func.count().label("cnt"))
            .where(Event.project_id == project_id, Event.timestamp >= start)
            .group_by(Event.event_name)
            .order_by(func.count().desc())
            .limit(1)
        )
        top = top_result.first()
        if top:
            event_name = top.event_name
            data = await events_over_time(
                session,
                project_id=project_id,
                event_name=event_name,
                start=start,
                end=now,
                granularity=gran,
            )

    return data, event_name


# ── Public handlers ────────────────────────────────────────────────────────────


async def show_reports_menu(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Show a 7-day analytics text summary for the project."""
    pid = uuid.UUID(project_id_str)
    now = datetime.now(UTC)
    seven_days_ago = now - timedelta(days=7)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        total_result = await session.execute(
            select(func.count())
            .select_from(Event)
            .where(Event.project_id == pid, Event.timestamp >= seven_days_ago)
        )
        total = total_result.scalar_one()

        sessions_result = await session.execute(
            select(func.count(func.distinct(Event.session_id)))
            .select_from(Event)
            .where(Event.project_id == pid, Event.timestamp >= seven_days_ago)
        )
        unique_sessions = sessions_result.scalar_one()

        top_result = await session.execute(
            select(Event.event_name, func.count().label("cnt"))
            .where(Event.project_id == pid, Event.timestamp >= seven_days_ago)
            .group_by(Event.event_name)
            .order_by(func.count().desc())
            .limit(5)
        )
        top_events = top_result.all()

    period = f"{seven_days_ago.strftime('%-d %b')} – {now.strftime('%-d %b')}"
    lines = [
        f"📈 <b>Report: {html.escape(project.name)}</b>",
        f"<i>{period}</i>",
        "─────────────────",
        f"📊 Total events: <b>{total:,}</b>",
        f"👤 Unique sessions: <b>{unique_sessions:,}</b>",
    ]

    if top_events:
        lines.append("\n<b>Top events (7 days):</b>")
        for row in top_events:
            lines.append(f"  • {html.escape(row.event_name)}: <b>{row.cnt:,}</b>")
    else:
        lines.append("\n<i>No events in the last 7 days.</i>")

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 View Chart", callback_data=f"rpt_chart:{project_id_str}")],
            [InlineKeyboardButton("« Back", callback_data=f"proj:{project_id_str}")],
        ]
    )
    await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


async def send_chart_photo(
    query: CallbackQuery,
    project_id_str: str,
    owner_user_id: uuid.UUID,
    period: str = "7d",
    gran: str = "day",
) -> None:
    """Generate and send the report chart as a new photo reply.

    Called when the user first opens the chart from the report menu.
    Attaches period-switching and comparison buttons to the photo.
    """
    assert isinstance(query.message, Message)
    pid = uuid.UUID(project_id_str)
    now = datetime.now(UTC)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        data, chart_event = await _get_top_event_data(session, pid, period, gran, now)

    period_label = _PERIOD_LABEL.get(period, period)
    back_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("« Back to report", callback_data=f"menu:reports:{project_id_str}")]]
    )

    if not data:
        await query.edit_message_text(
            "📭 No event data available for chart.",
            reply_markup=back_keyboard,
        )
        return

    try:
        png_bytes = await generate_line_chart(
            data,
            title=chart_event,
            period_label=period_label,
        )
    except ChartGenerationError:
        await query.edit_message_text(
            "⚠️ Chart service unavailable. Please try again later.",
            reply_markup=back_keyboard,
        )
        return

    # Edit original message to a nav anchor, then send photo below it
    await query.edit_message_text(
        f"📊 <b>{html.escape(chart_event)}</b> — {period_label}  ↓",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "« Back to report", callback_data=f"menu:reports:{project_id_str}"
                    )
                ]
            ]
        ),
    )
    await query.message.reply_photo(
        photo=png_bytes,
        caption=f"📈 {project.name} · {chart_event} · {period_label}",
        reply_markup=_report_chart_keyboard(project_id_str, period, gran),
    )


async def update_report_chart(
    query: CallbackQuery,
    project_id_str: str,
    owner_user_id: uuid.UUID,
    period: str,
    gran: str,
) -> None:
    """Edit the existing chart photo in-place with a new period/granularity."""
    pid = uuid.UUID(project_id_str)
    now = datetime.now(UTC)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.answer("❌ Project not found.", show_alert=True)
            return

        data, chart_event = await _get_top_event_data(session, pid, period, gran, now)

    period_label = _PERIOD_LABEL.get(period, period)

    if not data:
        await query.answer(f"No data for {period_label}.", show_alert=True)
        return

    try:
        png_bytes = await generate_line_chart(
            data,
            title=chart_event,
            period_label=period_label,
        )
    except ChartGenerationError:
        await query.answer("⚠️ Chart service unavailable.", show_alert=True)
        return

    await query.edit_message_media(
        media=InputMediaPhoto(
            media=png_bytes,
            caption=f"📈 {project.name} · {chart_event} · {period_label}",
        ),
        reply_markup=_report_chart_keyboard(project_id_str, period, gran),
    )


async def send_report_comparison(
    query: CallbackQuery,
    project_id_str: str,
    owner_user_id: uuid.UUID,
    period: str,
    gran: str,
) -> None:
    """Edit the chart photo to show current vs prior period comparison."""
    pid = uuid.UUID(project_id_str)
    now = datetime.now(UTC)

    delta = _PERIODS.get(period, timedelta(days=7))
    period_label = _PERIOD_LABEL.get(period, period)
    current_start = now - delta
    previous_start = current_start - delta

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.answer("❌ Project not found.", show_alert=True)
            return

        data_current, chart_event = await _get_top_event_data(session, pid, period, gran, now)
        if not data_current:
            await query.answer(f"No data for {period_label}.", show_alert=True)
            return

        data_previous = await events_over_time(
            session,
            project_id=pid,
            event_name=chart_event,
            start=previous_start,
            end=current_start,
            granularity=gran,
        )

        cmp = await compare_periods(
            session,
            project_id=pid,
            event_name=chart_event,
            current_start=current_start,
            current_end=now,
            previous_start=previous_start,
            previous_end=current_start,
        )

    delta_pct = cmp["delta_pct"]
    if delta_pct is None:
        delta_str = "vs prior period (no prior data)"
    elif delta_pct >= 0:
        delta_str = f"+{delta_pct:.1f}% vs prior period"
    else:
        delta_str = f"{delta_pct:.1f}% vs prior period"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "← Back to chart",
                    callback_data=f"rpt_prd:{project_id_str}:{period}:{gran}",
                )
            ],
            [
                InlineKeyboardButton(
                    "« Back to report", callback_data=f"menu:reports:{project_id_str}"
                )
            ],
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
            caption=f"📊 {project.name} · {chart_event} · {delta_str}",
        ),
        reply_markup=keyboard,
    )
