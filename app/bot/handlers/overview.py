"""Overview handler: panoramic visits chart across all of the user's projects."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

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
from app.bot.constants import PERIOD_LABEL, PERIODS
from app.core.database import get_session_factory
from app.models.project import Project
from app.models.user import User
from app.services.analytics import events_over_time
from app.services.charts import ChartGenerationError, generate_multi_line_chart
from app.services.projects import list_projects


def _overview_keyboard(period: str) -> InlineKeyboardMarkup:
    period_row = [
        InlineKeyboardButton(
            f"✓ {p}" if p == period else p,
            callback_data=f"ovw_prd:{p}",
        )
        for p in PERIODS
    ]
    return InlineKeyboardMarkup([period_row])


def _granularity_for(period: str) -> str:
    return "week" if period == "90d" else "day"


async def _build_series(
    session: AsyncSession,
    projects: list[Project],
    period: str,
) -> tuple[list[dict[str, Any]], int]:
    """Return (chart series, total visits across all projects)."""
    now = datetime.now(UTC)
    start = now - PERIODS.get(period, PERIODS["7d"])
    gran = _granularity_for(period)

    series: list[dict[str, Any]] = []
    total = 0
    for project in projects:
        data = await events_over_time(
            session,
            project_id=project.id,
            event_name="pageview",
            start=start,
            end=now,
            granularity=gran,
        )
        series.append({"label": project.name, "data": data})
        total += sum(row["count"] for row in data)
    return series, total


async def _render_chart(
    series: list[dict[str, Any]],
    period: str,
) -> bytes:
    return await generate_multi_line_chart(
        series,
        title="Visits",
        period_label=PERIOD_LABEL.get(period, period),
    )


@requires_user
async def overview_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,  # noqa: ARG001
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """/overview — multi-line visits chart across every project the user owns."""
    assert update.message is not None

    projects = await list_projects(session, user.id)
    if not projects:
        await update.message.reply_text(
            "📭 No projects yet.\n\nUse /add <i>name</i> to create one.",
            parse_mode="HTML",
        )
        return

    period = "7d"
    series, total = await _build_series(session, projects, period)

    if total == 0:
        await update.message.reply_text(
            f"📭 No visits across your {len(projects)} project(s) in the {PERIOD_LABEL[period]}.",
            reply_markup=_overview_keyboard(period),
        )
        return

    try:
        png_bytes = await _render_chart(series, period)
    except ChartGenerationError:
        await update.message.reply_text("⚠️ Chart service unavailable. Please try again later.")
        return

    await update.message.reply_photo(
        photo=png_bytes,
        caption=f"📈 Visits across {len(projects)} project(s) · {PERIOD_LABEL[period]}",
        reply_markup=_overview_keyboard(period),
    )


async def update_overview_period(
    query: CallbackQuery,
    owner_user_id: uuid.UUID,
    period: str,
) -> None:
    """Edit the chart photo in-place when user toggles 7d/30d/90d."""
    assert isinstance(query.message, Message)

    factory = get_session_factory()
    async with factory() as session:
        projects = await list_projects(session, owner_user_id)
        if not projects:
            await query.answer("No projects.", show_alert=True)
            return
        series, total = await _build_series(session, projects, period)

    if total == 0:
        await query.answer(f"No visits in the {PERIOD_LABEL.get(period, period)}.", show_alert=True)
        return

    try:
        png_bytes = await _render_chart(series, period)
    except ChartGenerationError:
        await query.answer("⚠️ Chart service unavailable.", show_alert=True)
        return

    await query.edit_message_media(
        media=InputMediaPhoto(
            media=png_bytes,
            caption=f"📈 Visits across {len(projects)} project(s) · {PERIOD_LABEL[period]}",
        ),
        reply_markup=_overview_keyboard(period),
    )
