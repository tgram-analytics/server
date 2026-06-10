"""KPI handlers: pin events as KPIs and manage the North Star metric."""

from __future__ import annotations

import html
import uuid

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
from app.bot.states import BotStateService
from app.core.database import get_session_factory
from app.models.user import User
from app.services.analytics import list_event_names
from app.services.kpis import add_kpi, get_kpi, list_kpis, remove_kpi, set_north_star
from app.services.projects import get_project

# ── Public menu ──────────────────────────────────────────────────────────────


async def show_kpis_menu(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """List pinned KPIs for a project; ⭐ marks the North Star."""
    pid = uuid.UUID(project_id_str)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        kpis = await list_kpis(session, project_id=pid)

    rows: list[list[InlineKeyboardButton]] = []
    for k in kpis:
        icon = "⭐" if k.is_north_star else "🎯"
        name = k.event_name if len(k.event_name) <= 54 else k.event_name[:51] + "…"
        rows.append([InlineKeyboardButton(f"{icon} {name}", callback_data=f"kpi_act:{k.id}")])

    rows.append([InlineKeyboardButton("➕ Add KPI", callback_data=f"kpi_add:{project_id_str}")])
    rows.append([InlineKeyboardButton("« Back", callback_data=f"proj:{project_id_str}")])

    text = (
        f"🎯 <b>KPIs: {html.escape(project.name)}</b>\n─────────────────\n"
        "KPIs appear at the top of your /digest.\n"
        "⭐ marks your North Star metric."
    )
    if not kpis:
        text += "\n\n<i>No KPIs yet. Pin the events that matter most.</i>"

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


# ── Callback dispatcher ──────────────────────────────────────────────────────


@requires_user
async def kpi_callback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """Handle all KPI-related callbacks."""
    query = update.callback_query
    assert query is not None
    await query.answer()

    owner_user_id = user.id
    data: str = query.data or ""

    if data.startswith("kpi_add:"):
        await _start_add_kpi(query, data[8:], owner_user_id)

    elif data.startswith("kpi_evt:"):
        await _pin_event(query, data[8:], owner_user_id)

    elif data.startswith("kpi_act:"):
        await _show_kpi_actions(query, data[8:], owner_user_id)

    elif data.startswith("kpi_star:"):
        await _make_north_star(query, data[9:], owner_user_id)

    elif data.startswith("kpi_del:"):
        await _remove_kpi(query, data[8:], owner_user_id)

    elif data.startswith("back:kpis:"):
        await show_kpis_menu(query, data[10:], owner_user_id)


# ── Add flow ─────────────────────────────────────────────────────────────────


async def _start_add_kpi(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Show the event picker; project context is kept in conversation state."""
    assert isinstance(query.message, Message)
    chat_id = query.message.chat_id

    pid = uuid.UUID(project_id_str)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        events = await list_event_names(session, project_id=pid)
        already = {k.event_name for k in await list_kpis(session, project_id=pid)}
        events = [e for e in events if e["event_name"] not in already]

        if not events:
            await query.edit_message_text(
                "📭 No more events to pin. Send some events first.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "« Back to KPIs",
                                callback_data=f"back:kpis:{project_id_str}",
                            )
                        ]
                    ]
                ),
            )
            return

        svc = BotStateService(session)
        await svc.save(
            chat_id,
            flow="add_kpi",
            step="event",
            payload={"project_id": project_id_str},
        )
        await session.commit()

    rows = [
        [
            InlineKeyboardButton(
                f"{e['event_name']}  ({e['count']:,})",
                callback_data=f"kpi_evt:{e['event_name']}",
            )
        ]
        for e in events
    ]
    rows.append(
        [InlineKeyboardButton("« Back to KPIs", callback_data=f"back:kpis:{project_id_str}")]
    )

    await query.edit_message_text(
        "🎯 <b>Add KPI</b>\n\nPick the event to pin:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _pin_event(query: CallbackQuery, event_name: str, owner_user_id: uuid.UUID) -> None:
    """Event picked — pin it and return to the KPI menu."""
    assert isinstance(query.message, Message)
    chat_id = query.message.chat_id

    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        state = await svc.get(chat_id)

        if state is None or state.flow != "add_kpi" or state.step != "event":
            await query.edit_message_text("❌ Session expired. Use the KPIs menu to start again.")
            return

        payload = state.payload or {}
        project_id_str = payload.get("project_id")
        if not project_id_str:
            await query.edit_message_text("❌ Session expired.")
            return

        pid = uuid.UUID(project_id_str)
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        await add_kpi(session, project_id=pid, event_name=event_name)
        await svc.clear(chat_id)
        await session.commit()

    await show_kpis_menu(query, project_id_str, owner_user_id)


# ── Actions on an existing KPI ───────────────────────────────────────────────


async def _show_kpi_actions(
    query: CallbackQuery, kpi_id_str: str, owner_user_id: uuid.UUID
) -> None:
    factory = get_session_factory()
    async with factory() as session:
        kpi = await get_kpi(session, uuid.UUID(kpi_id_str))
        if kpi is None:
            await query.edit_message_text("❌ KPI not found.")
            return
        project = await get_project(session, kpi.project_id, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return
        project_id_str = str(kpi.project_id)

    rows = []
    if not kpi.is_north_star:
        rows.append(
            [InlineKeyboardButton("⭐ Set as North Star", callback_data=f"kpi_star:{kpi.id}")]
        )
    rows.append([InlineKeyboardButton("🗑 Remove", callback_data=f"kpi_del:{kpi.id}")])
    rows.append([InlineKeyboardButton("« Back", callback_data=f"back:kpis:{project_id_str}")])

    icon = "⭐" if kpi.is_north_star else "🎯"
    await query.edit_message_text(
        f"{icon} <b>{html.escape(kpi.event_name)}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _make_north_star(query: CallbackQuery, kpi_id_str: str, owner_user_id: uuid.UUID) -> None:
    factory = get_session_factory()
    async with factory() as session:
        kpi = await get_kpi(session, uuid.UUID(kpi_id_str))
        if kpi is None:
            await query.edit_message_text("❌ KPI not found.")
            return
        project = await get_project(session, kpi.project_id, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return
        project_id_str = str(kpi.project_id)
        await set_north_star(session, project_id=kpi.project_id, event_name=kpi.event_name)
        await session.commit()

    await show_kpis_menu(query, project_id_str, owner_user_id)


async def _remove_kpi(query: CallbackQuery, kpi_id_str: str, owner_user_id: uuid.UUID) -> None:
    factory = get_session_factory()
    async with factory() as session:
        kpi = await get_kpi(session, uuid.UUID(kpi_id_str))
        if kpi is None:
            await query.edit_message_text("❌ KPI not found.")
            return
        project = await get_project(session, kpi.project_id, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return
        project_id_str = str(kpi.project_id)
        await remove_kpi(session, kpi.id)
        await session.commit()

    await show_kpis_menu(query, project_id_str, owner_user_id)
