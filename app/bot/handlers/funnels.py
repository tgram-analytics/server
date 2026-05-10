"""Funnel handlers: create, view, and delete conversion funnels."""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.ext import ContextTypes

from app.bot.auth import requires_user
from app.bot.constants import PERIOD_LABEL, PERIODS, TIME_WINDOW_LABEL, TIME_WINDOWS, escape_photo
from app.bot.states import BotStateService
from app.core.database import get_session_factory
from app.models.user import User
from app.services.analytics import list_event_names
from app.services.charts import ChartGenerationError, generate_funnel_chart
from app.services.funnels import (
    analyze_funnel,
    create_funnel,
    delete_funnel,
    get_funnel,
    list_funnels,
)
from app.services.projects import get_project

# ── Keyboard helpers ─────────────────────────────────────────────────────────


def _funnel_view_keyboard(
    funnel_id_str: str, project_id_str: str, period: str
) -> InlineKeyboardMarkup:
    period_row = [
        InlineKeyboardButton(
            f"✓ {p}" if p == period else p,
            callback_data=f"fnl_view:{funnel_id_str}:{p}",
        )
        for p in PERIODS
    ]
    return InlineKeyboardMarkup(
        [
            period_row,
            [InlineKeyboardButton("🗑 Delete funnel", callback_data=f"fnl_del:{funnel_id_str}")],
            [
                InlineKeyboardButton(
                    "« Back to funnels",
                    callback_data=f"back:funnels:{project_id_str}",
                )
            ],
        ]
    )


# ── Public menu ──────────────────────────────────────────────────────────────


async def show_funnels_menu(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """List saved funnels for a project."""
    pid = uuid.UUID(project_id_str)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        funnels = await list_funnels(session, project_id=pid)

    rows: list[list[InlineKeyboardButton]] = []
    for f in funnels:
        label = f.name if len(f.name) <= 56 else f.name[:53] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"fnl_view:{f.id}:30d")])

    rows.append([InlineKeyboardButton("➕ Add Funnel", callback_data=f"fnl_add:{project_id_str}")])
    rows.append([InlineKeyboardButton("« Back", callback_data=f"proj:{project_id_str}")])

    text = f"🔀 <b>Funnels: {html.escape(project.name)}</b>\n─────────────────"
    if not funnels:
        text += "\n\n<i>No funnels yet. Create one to track conversions.</i>"

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


# ── Callback dispatcher ─────────────────────────────────────────────────────


@requires_user
async def funnel_callback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """Handle all funnel-related callbacks."""
    query = update.callback_query
    assert query is not None
    await query.answer()

    owner_user_id = user.id
    data: str = query.data or ""

    if data.startswith("fnl_add:"):
        project_id_str = data[8:]
        await _start_add_funnel(query, project_id_str)

    elif data.startswith("fnl_evt:"):
        event_name = data[8:]
        await _add_event_to_funnel(query, event_name)

    elif data == "fnl_done":
        await _finalize_events(query)

    elif data.startswith("fnl_win:"):
        window_key = data[8:]
        await _pick_time_window(query, window_key, owner_user_id)

    elif data.startswith("fnl_view:"):
        # fnl_view:{funnel_id}:{period}
        parts = data[9:].rsplit(":", 1)
        if len(parts) == 2:
            await _view_funnel(await escape_photo(query), parts[0], owner_user_id, period=parts[1])

    elif data.startswith("fnl_del:"):
        funnel_id_str = data[8:]
        await _delete_funnel(query, funnel_id_str, owner_user_id)

    elif data.startswith("back:funnels:"):
        project_id_str = data[13:]
        await show_funnels_menu(await escape_photo(query), project_id_str, owner_user_id)


# ── Creation flow ────────────────────────────────────────────────────────────


async def _start_add_funnel(query: CallbackQuery, project_id_str: str) -> None:
    """Step 1: show the event picker directly — no name input required."""
    assert isinstance(query.message, Message)
    chat_id = query.message.chat_id

    pid = uuid.UUID(project_id_str)

    factory = get_session_factory()
    async with factory() as session:
        events = await list_event_names(session, project_id=pid)

        if not events:
            await query.edit_message_text(
                "📭 No events recorded yet. Send some events first, then create a funnel.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "« Back to funnels",
                                callback_data=f"back:funnels:{project_id_str}",
                            )
                        ]
                    ]
                ),
            )
            return

        svc = BotStateService(session)
        await svc.save(
            chat_id,
            flow="add_funnel",
            step="events",
            payload={"project_id": project_id_str, "events": []},
        )
        await session.commit()

    keyboard = _event_picker_keyboard(events, selected=[])
    await query.edit_message_text(
        "🔀 <b>New Funnel</b>\n\n"
        "Tap events in the order users go through them.\n"
        "Selected: <i>none</i>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


def _event_picker_keyboard(
    events: list[dict[str, Any]], selected: list[str]
) -> InlineKeyboardMarkup:
    """Build keyboard with available events and a Done button."""
    rows: list[list[InlineKeyboardButton]] = []
    for evt in events:
        name = evt["event_name"]
        # Show how many times this event appears in the sequence
        times = selected.count(name)
        label = f"{'✓ ' * times}{name}  ({evt['count']:,})"
        rows.append([InlineKeyboardButton(label, callback_data=f"fnl_evt:{name}")])
    if len(selected) >= 2:
        rows.append([InlineKeyboardButton("✅ Done", callback_data="fnl_done")])
    return InlineKeyboardMarkup(rows)


async def _add_event_to_funnel(query: CallbackQuery, event_name: str) -> None:
    """Append an event to the funnel sequence and re-render the picker."""
    assert isinstance(query.message, Message)
    chat_id = query.message.chat_id

    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        state = await svc.get(chat_id)

        if state is None or state.flow != "add_funnel" or state.step != "events":
            await query.edit_message_text(
                "❌ Session expired. Use the Funnels menu to start again."
            )
            return

        payload = state.payload or {}
        selected: list[str] = payload.get("events", [])
        selected.append(event_name)
        payload["events"] = selected

        project_id_str = payload.get("project_id")
        if not project_id_str:
            await query.edit_message_text("❌ Session expired.")
            return

        pid = uuid.UUID(project_id_str)
        events = await list_event_names(session, project_id=pid)

        await svc.save(chat_id, flow="add_funnel", step="events", payload=payload)
        await session.commit()

    steps_text = " → ".join(html.escape(s) for s in selected) if selected else "<i>none</i>"
    keyboard = _event_picker_keyboard(events, selected)

    await query.edit_message_text(
        f"🔀 <b>New Funnel</b>\n\nSelected: {steps_text}\n\nTap more events or press Done.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def _finalize_events(query: CallbackQuery) -> None:
    """Events selected — now pick the time window."""
    assert isinstance(query.message, Message)
    chat_id = query.message.chat_id

    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        state = await svc.get(chat_id)

        if state is None or state.flow != "add_funnel" or state.step != "events":
            await query.edit_message_text("❌ Session expired.")
            return

        payload = state.payload or {}
        selected: list[str] = payload.get("events", [])

        if len(selected) < 2:
            await query.answer("Select at least 2 events.", show_alert=True)
            return

        await svc.save(chat_id, flow="add_funnel", step="window", payload=payload)
        await session.commit()

    steps_text = " → ".join(html.escape(s) for s in selected)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=f"fnl_win:{key}")
                for key, label in [("5min", "5 min"), ("1h", "1 hour")]
            ],
            [
                InlineKeyboardButton(label, callback_data=f"fnl_win:{key}")
                for key, label in [("24h", "24 hours"), ("7d", "7 days")]
            ],
        ]
    )

    await query.edit_message_text(
        f"🔀 <b>New Funnel</b>\n\n"
        f"Steps: {steps_text}\n\n"
        "How long should a user have to complete the funnel?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def _pick_time_window(
    query: CallbackQuery, window_key: str, owner_user_id: uuid.UUID
) -> None:
    """Time window selected — create the funnel and show first results."""
    assert isinstance(query.message, Message)
    chat_id = query.message.chat_id

    window_seconds = TIME_WINDOWS.get(window_key)
    if window_seconds is None:
        await query.answer("Invalid time window.", show_alert=True)
        return

    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        state = await svc.get(chat_id)

        if state is None or state.flow != "add_funnel" or state.step != "window":
            await query.edit_message_text("❌ Session expired.")
            return

        payload = state.payload or {}
        project_id_str = payload.get("project_id")
        steps: list[str] = payload.get("events", [])

        if not project_id_str or len(steps) < 2:
            await svc.clear(chat_id)
            await session.commit()
            await query.edit_message_text("❌ Invalid state. Please start again.")
            return

        funnel_name = " → ".join(steps)
        pid = uuid.UUID(project_id_str)
        funnel = await create_funnel(
            session,
            project_id=pid,
            name=funnel_name,
            steps=steps,
            time_window=window_seconds,
        )
        await svc.clear(chat_id)
        await session.commit()

    window_label = TIME_WINDOW_LABEL.get(window_key, window_key)
    steps_text = " → ".join(html.escape(s) for s in steps)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📊 View Results",
                    callback_data=f"fnl_view:{funnel.id}:30d",
                )
            ],
            [
                InlineKeyboardButton(
                    "« Back to funnels",
                    callback_data=f"back:funnels:{project_id_str}",
                )
            ],
        ]
    )

    await query.edit_message_text(
        f"✅ <b>Funnel created!</b>\n\n"
        f"Name: <b>{html.escape(funnel_name)}</b>\n"
        f"Steps: {steps_text}\n"
        f"Time window: {window_label}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ── View & delete ────────────────────────────────────────────────────────────


async def _view_funnel(
    query: CallbackQuery,
    funnel_id_str: str,
    owner_user_id: uuid.UUID,
    period: str = "30d",
) -> None:
    """Run funnel analysis and send a chart."""
    assert isinstance(query.message, Message)

    factory = get_session_factory()
    async with factory() as session:
        funnel = await get_funnel(session, uuid.UUID(funnel_id_str))
        if funnel is None:
            await query.edit_message_text("❌ Funnel not found.")
            return

        project = await get_project(session, funnel.project_id, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        now = datetime.now(UTC)
        delta = PERIODS.get(period, PERIODS["30d"])
        start = now - delta

        data = await analyze_funnel(session, funnel=funnel, start=start, end=now)

    period_label = PERIOD_LABEL.get(period, period)
    project_id_str = str(funnel.project_id)
    window_label = _seconds_to_label(funnel.time_window)

    if not data or all(r["count"] == 0 for r in data):
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "« Back to funnels",
                        callback_data=f"back:funnels:{project_id_str}",
                    )
                ]
            ]
        )
        await query.edit_message_text(
            f"📭 <b>{html.escape(funnel.name)}</b> — no data for {period_label}.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    try:
        png_bytes = await generate_funnel_chart(
            data,
            title=f"{funnel.name} — {period_label} (window: {window_label})",
        )
    except ChartGenerationError:
        await query.edit_message_text(
            "⚠️ Chart service unavailable. Please try again later.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "« Back",
                            callback_data=f"back:funnels:{project_id_str}",
                        )
                    ]
                ]
            ),
        )
        return

    # Build summary text
    first_count = data[0]["count"]
    summary_lines = []
    for i, row in enumerate(data):
        pct = round(row["count"] / first_count * 100) if first_count else 0
        safe_step = html.escape(str(row["step"]))
        if i == 0:
            summary_lines.append(f"  {safe_step}: <b>{row['count']:,}</b>")
        else:
            summary_lines.append(f"  → {safe_step}: <b>{row['count']:,}</b> ({pct}%)")

    caption = f"🔀 {project.name} · {funnel.name} · {period_label}\n⏱ Window: {window_label}"

    # Edit current message as anchor
    await query.edit_message_text(
        f"🔀 <b>{html.escape(funnel.name)}</b> — {period_label}\n"
        f"⏱ {window_label}\n"
        f"─────────────────\n" + "\n".join(summary_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "« Back to funnels",
                        callback_data=f"back:funnels:{project_id_str}",
                    )
                ]
            ]
        ),
    )
    await query.message.reply_photo(
        photo=png_bytes,
        caption=caption,
        reply_markup=_funnel_view_keyboard(funnel_id_str, project_id_str, period),
    )


async def _delete_funnel(
    query: CallbackQuery, funnel_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Delete a funnel and return to the funnels list."""
    factory = get_session_factory()
    async with factory() as session:
        funnel = await get_funnel(session, uuid.UUID(funnel_id_str))
        if funnel is None:
            await query.edit_message_text("❌ Funnel not found.")
            return

        project = await get_project(session, funnel.project_id, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        project_id_str = str(funnel.project_id)
        await delete_funnel(session, funnel.id)
        await session.commit()

    await show_funnels_menu(await escape_photo(query), project_id_str, owner_user_id)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _seconds_to_label(seconds: int) -> str:
    """Convert a time window in seconds to a human-readable label."""
    for key, val in TIME_WINDOWS.items():
        if val == seconds:
            return TIME_WINDOW_LABEL[key]
    if seconds < 3600:
        return f"{seconds // 60}min"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
