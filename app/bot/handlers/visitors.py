"""Visitors handler: device, browser, and environment breakdowns.

Shows top values for each visitor context dimension ($os, $browser,
$language, $device_type) with percentage breakdowns and optional
bar charts per dimension.
"""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.constants import PERIOD_LABEL, PERIODS
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.services.analytics import events_over_time, top_properties
from app.services.charts import (
    ChartGenerationError,
    generate_bar_chart,
    generate_line_chart,
)
from app.services.projects import get_project

# ── Dimension config ──────────────────────────────────────────────────────────

_DIMENSIONS: list[tuple[str, str, str]] = [
    # (property_key, emoji, display_label)
    ("$os", "🖥", "OS"),
    ("$browser", "🌐", "Browser"),
    ("$language", "🌍", "Language"),
    ("$device_type", "📱", "Device"),
]

_DIM_KEYS = {d[0] for d in _DIMENSIONS}


# ── Keyboard helpers ──────────────────────────────────────────────────────────


def _visitors_keyboard(project_id_str: str, period: str) -> InlineKeyboardMarkup:
    period_row = [
        InlineKeyboardButton(
            f"✓ {p}" if p == period else p,
            callback_data=f"vis_prd:{project_id_str}:{p}",
        )
        for p in PERIODS
    ]
    line_row = [
        InlineKeyboardButton(
            "📈 Visits over time",
            callback_data=f"vis_line:{project_id_str}:{period}",
        )
    ]
    dim_buttons = [
        InlineKeyboardButton(
            f"{emoji} {label}",
            callback_data=f"vis_chart:{project_id_str}:{key}:{period}",
        )
        for key, emoji, label in _DIMENSIONS
    ]
    # Two rows of two so labels don't get truncated on mobile.
    dim_rows = [dim_buttons[:2], dim_buttons[2:]]
    return InlineKeyboardMarkup(
        [
            period_row,
            line_row,
            *dim_rows,
            [InlineKeyboardButton("« Back", callback_data=f"proj:{project_id_str}")],
        ]
    )


# ── Shared data fetcher ──────────────────────────────────────────────────────


async def _build_visitors_text(
    session: AsyncSession,
    project_id: uuid.UUID,
    project_name: str,
    period: str,
) -> str:
    """Query all dimensions and format the text summary."""
    now = datetime.now(UTC)
    delta = PERIODS.get(period, PERIODS["7d"])
    start = now - delta
    period_label = PERIOD_LABEL.get(period, period)

    lines = [
        f"👥 <b>Visitors: {html.escape(project_name)}</b>",
        f"<i>{period_label}</i>",
        "─────────────────",
    ]

    for prop_key, emoji, label in _DIMENSIONS:
        rows = await top_properties(
            session,
            project_id=project_id,
            event_name="pageview",
            property_key=prop_key,
            start=start,
            end=now,
            limit=5,
        )
        lines.append(f"\n{emoji} <b>{label}:</b>")
        if not rows:
            lines.append("  <i>No data</i>")
            continue

        total = sum(r["count"] for r in rows)
        for r in rows:
            pct = round(r["count"] / total * 100) if total else 0
            safe_value = html.escape(str(r["value"]))
            lines.append(f"  • {safe_value}: <b>{r['count']:,}</b> ({pct}%)")

    return "\n".join(lines)


# ── Public handlers ───────────────────────────────────────────────────────────


async def show_visitors_menu(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID, period: str = "7d"
) -> None:
    """Show visitor breakdown text with period and chart buttons."""
    pid = uuid.UUID(project_id_str)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        text = await _build_visitors_text(session, pid, project.name, period)

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=_visitors_keyboard(project_id_str, period),
    )


async def update_visitors_period(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID, period: str
) -> None:
    """Re-render visitors text with a different period."""
    await show_visitors_menu(query, project_id_str, owner_user_id, period)


async def send_visitors_chart(
    query: CallbackQuery,
    project_id_str: str,
    owner_user_id: uuid.UUID,
    dimension: str,
    period: str,
) -> None:
    """Send a bar chart for a single visitor dimension."""
    assert isinstance(query.message, Message)
    if dimension not in _DIM_KEYS:
        await query.answer("Unknown dimension.", show_alert=True)
        return

    pid = uuid.UUID(project_id_str)
    now = datetime.now(UTC)
    delta = PERIODS.get(period, PERIODS["7d"])
    start = now - delta
    period_label = PERIOD_LABEL.get(period, period)
    settings = get_settings()

    # Find the display label for the dimension.
    dim_label = next(label for key, _e, label in _DIMENSIONS if key == dimension)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.answer("❌ Project not found.", show_alert=True)
            return

        rows = await top_properties(
            session,
            project_id=pid,
            event_name="pageview",
            property_key=dimension,
            start=start,
            end=now,
            limit=10,
        )

    back_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "« Back to visitors", callback_data=f"vis_prd:{project_id_str}:{period}"
                )
            ]
        ]
    )

    if not rows:
        await query.answer(f"No {dim_label} data for {period_label}.", show_alert=True)
        return

    try:
        png_bytes = await generate_bar_chart(
            rows,
            title=f"{dim_label} — {period_label}",
            quickchart_url=settings.quickchart_url,
        )
    except ChartGenerationError:
        await query.answer("⚠️ Chart service unavailable.", show_alert=True)
        return

    # Edit current message as anchor, send chart below.
    await query.edit_message_text(
        f"📊 <b>{dim_label}</b> — {period_label}  ↓",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "« Back to visitors", callback_data=f"vis_prd:{project_id_str}:{period}"
                    )
                ]
            ]
        ),
    )
    await query.message.reply_photo(
        photo=png_bytes,
        caption=f"👥 {project.name} · {dim_label} · {period_label}",
        reply_markup=back_keyboard,
    )


async def send_visitors_line_chart(
    query: CallbackQuery,
    project_id_str: str,
    owner_user_id: uuid.UUID,
    period: str,
) -> None:
    """Send a time-series line chart of pageviews for the period."""
    assert isinstance(query.message, Message)

    pid = uuid.UUID(project_id_str)
    now = datetime.now(UTC)
    delta = PERIODS.get(period, PERIODS["7d"])
    start = now - delta
    period_label = PERIOD_LABEL.get(period, period)
    gran = "week" if period == "90d" else "day"
    settings = get_settings()

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.answer("❌ Project not found.", show_alert=True)
            return

        data = await events_over_time(
            session,
            project_id=pid,
            event_name="pageview",
            start=start,
            end=now,
            granularity=gran,
        )

    back_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "« Back to visitors", callback_data=f"vis_prd:{project_id_str}:{period}"
                )
            ]
        ]
    )

    if not data:
        await query.answer(f"No visits for {period_label}.", show_alert=True)
        return

    try:
        png_bytes = await generate_line_chart(
            data,
            title="Visits",
            period_label=period_label,
            quickchart_url=settings.quickchart_url,
        )
    except ChartGenerationError:
        await query.answer("⚠️ Chart service unavailable.", show_alert=True)
        return

    await query.edit_message_text(
        f"📈 <b>Visits</b> — {period_label}  ↓",
        parse_mode="HTML",
        reply_markup=back_keyboard,
    )
    await query.message.reply_photo(
        photo=png_bytes,
        caption=f"👥 {project.name} · Visits · {period_label}",
        reply_markup=back_keyboard,
    )
