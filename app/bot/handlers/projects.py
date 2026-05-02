"""Project management handlers: /add, /projects, and inline project menu."""

from __future__ import annotations

import html
import uuid

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.bot.constants import escape_photo
from app.bot.handlers.alerts import show_alerts_menu
from app.bot.handlers.events import show_events_menu
from app.bot.handlers.funnels import show_funnels_menu
from app.bot.handlers.reports import (
    handle_report_project_pick,
    send_chart_photo,
    send_report_comparison,
    show_reports_menu,
    update_report_chart,
)
from app.bot.handlers.settings import (
    handle_allow_all,
    show_settings_menu,
    start_set_allowlist,
    start_set_retention,
)
from app.bot.handlers.visitors import (
    send_visitors_chart,
    show_visitors_menu,
    update_visitors_period,
)
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.services.projects import create_project, delete_project, get_project, list_projects

# ── /add ──────────────────────────────────────────────────────────────────────


async def add_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None

    if not ctx.args:
        await update.message.reply_text(
            "Usage: /add <i>project-name</i>\n\nExample: <code>/add myapp.com</code>",
            parse_mode="HTML",
        )
        return

    settings = get_settings()
    name = " ".join(ctx.args)

    factory = get_session_factory()
    async with factory() as session:
        project, api_key = await create_project(
            session,
            name=name,
            admin_chat_id=settings.admin_chat_id,
        )
        await session.commit()

    base = settings.webhook_base_url.rstrip("/") or "https://your-server.com"
    env_block = f"TGA_URL={base}\nTGA_API_KEY={api_key}"
    snippet = (
        f"curl -X POST {base}/api/v1/track \\\n"
        f'  -H "Content-Type: application/json" \\\n'
        f'  -d \'{{"api_key": "{api_key}", '
        f'"event_name": "page_view", "session_id": "user-123"}}\''
    )

    await update.message.reply_text(
        f"✅ Project <b>{html.escape(name)}</b> created!\n\n"
        f"⚠️ Save this key — it won't be shown again.\n\n"
        f"<b>Env:</b>\n<tg-spoiler><pre>{env_block}</pre></tg-spoiler>\n\n"
        f"<b>Quickstart:</b>\n<pre>{snippet}</pre>",
        parse_mode="HTML",
    )


# ── /projects ─────────────────────────────────────────────────────────────────


async def projects_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
        [[InlineKeyboardButton(f"📊 {p.name}", callback_data=f"proj:{p.id}")] for p in projects]
    )
    await update.message.reply_text("Select a project:", reply_markup=keyboard)


# ── Inline callback dispatcher ─────────────────────────────────────────────────


async def project_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    await query.answer()

    settings = get_settings()
    admin_chat_id = settings.admin_chat_id

    # Admin-only guard for callbacks (CommandHandler filter doesn't cover these)
    if update.effective_user is None or update.effective_user.id != admin_chat_id:
        return

    data: str = query.data or ""

    if data.startswith("proj:"):
        await _show_project_menu(await escape_photo(query), data[5:], admin_chat_id)

    elif data.startswith("del_ask:"):
        await _ask_delete_confirmation(query, data[8:])

    elif data.startswith("del_yes:"):
        await _confirm_delete(query, data[8:], admin_chat_id)

    elif data.startswith("del_no:"):
        await _show_project_menu(query, data[7:], admin_chat_id)

    elif data.startswith("menu:events:"):
        project_id_str = data[12:]
        await show_events_menu(await escape_photo(query), project_id_str, admin_chat_id)

    elif data.startswith("menu:alerts:"):
        project_id_str = data[12:]
        await show_alerts_menu(await escape_photo(query), project_id_str, admin_chat_id)

    elif data.startswith("menu:reports:"):
        project_id_str = data[13:]
        await show_reports_menu(await escape_photo(query), project_id_str, admin_chat_id)

    elif data.startswith("rpt_chart:"):
        project_id_str = data[10:]
        await send_chart_photo(query, project_id_str, admin_chat_id)

    elif data.startswith("rpt_prd:"):
        # rpt_prd:{project_id}:{period}:{gran}
        parts = data[8:].rsplit(":", 2)
        if len(parts) == 3:
            await update_report_chart(
                query, parts[0], admin_chat_id, period=parts[1], gran=parts[2]
            )

    elif data.startswith("rpt_cmp:"):
        # rpt_cmp:{project_id}:{period}:{gran}
        parts = data[8:].rsplit(":", 2)
        if len(parts) == 3:
            await send_report_comparison(
                query, parts[0], admin_chat_id, period=parts[1], gran=parts[2]
            )

    elif data.startswith("rpt_pp:"):
        project_id_str = data[7:]
        await handle_report_project_pick(query, project_id_str, admin_chat_id, ctx)

    elif data.startswith("menu:funnels:"):
        project_id_str = data[13:]
        await show_funnels_menu(await escape_photo(query), project_id_str, admin_chat_id)

    elif data.startswith("menu:visitors:"):
        project_id_str = data[14:]
        await show_visitors_menu(await escape_photo(query), project_id_str, admin_chat_id)

    elif data.startswith("vis_prd:"):
        # vis_prd:{project_id}:{period}
        parts = data[8:].rsplit(":", 1)
        if len(parts) == 2:
            await update_visitors_period(
                await escape_photo(query), parts[0], admin_chat_id, period=parts[1]
            )

    elif data.startswith("vis_chart:"):
        # vis_chart:{project_id}:{dimension}:{period}
        parts = data[10:].rsplit(":", 2)
        if len(parts) == 3:
            await send_visitors_chart(
                query, parts[0], admin_chat_id, dimension=parts[1], period=parts[2]
            )

    elif data.startswith("menu:settings:"):
        project_id_str = data[14:]
        await show_settings_menu(query, project_id_str, admin_chat_id)

    elif data.startswith("set_ret:"):
        project_id_str = data[8:]
        await start_set_retention(query, project_id_str, admin_chat_id)

    elif data.startswith("set_dom:"):
        project_id_str = data[8:]
        await start_set_allowlist(query, project_id_str, admin_chat_id)

    elif data.startswith("allow_all:"):
        project_id_str = data[10:]
        await handle_allow_all(query, project_id_str, admin_chat_id)

    elif data.startswith("menu:"):
        parts = data.split(":", 2)
        feature = parts[1] if len(parts) > 1 else "unknown"
        await query.edit_message_text(
            f"🚧 <b>{feature.capitalize()}</b> — coming soon!",
            parse_mode="HTML",
        )

    elif data == "back:projects" or data == "home:projects":
        await _show_projects_list(await escape_photo(query), admin_chat_id)

    elif data == "home:reports":
        await _pick_project_for(query, admin_chat_id, feature="reports")

    elif data == "home:alerts":
        await _pick_project_for(query, admin_chat_id, feature="alerts")

    elif data == "home:help":
        from app.bot.handlers.system import _HELP_TEXT

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Back", callback_data="home:start")]]
        )
        await query.edit_message_text(_HELP_TEXT, parse_mode="HTML", reply_markup=keyboard)

    elif data == "home:start":
        from app.bot.handlers.system import _START_KEYBOARD

        await query.edit_message_text(
            "👋 <b>Welcome to tgram-analytics!</b>\n\n"
            "Self-hosted analytics you control via Telegram.",
            parse_mode="HTML",
            reply_markup=_START_KEYBOARD,
        )


# ── Private helpers ────────────────────────────────────────────────────────────


async def _pick_project_for(query: CallbackQuery, admin_chat_id: int, *, feature: str) -> None:
    """Show a project picker that routes to the given feature menu."""
    factory = get_session_factory()
    async with factory() as session:
        projects = await list_projects(session, admin_chat_id)

    if not projects:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Back", callback_data="home:start")]]
        )
        await query.edit_message_text(
            "📭 No projects yet.\n\nUse /add <i>name</i> to create one.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    if len(projects) == 1:
        # Skip picker — go straight to the feature menu
        pid = str(projects[0].id)
        if feature == "reports":
            await show_reports_menu(query, pid, admin_chat_id)
        elif feature == "alerts":
            await show_alerts_menu(query, pid, admin_chat_id)
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"📊 {p.name}", callback_data=f"menu:{feature}:{p.id}")]
            for p in projects
        ]
        + [[InlineKeyboardButton("« Back", callback_data="home:start")]]
    )
    await query.edit_message_text(f"Select a project for {feature}:", reply_markup=keyboard)


async def _show_projects_list(query: CallbackQuery, admin_chat_id: int) -> None:
    """Re-display the projects list via callback (for « Back button)."""
    factory = get_session_factory()
    async with factory() as session:
        projects = await list_projects(session, admin_chat_id)

    if not projects:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("« Home", callback_data="home:start")]]
        )
        await query.edit_message_text(
            "📭 No projects yet.\n\nUse /add <i>name</i> to create one.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"📊 {p.name}", callback_data=f"proj:{p.id}")] for p in projects]
        + [[InlineKeyboardButton("« Home", callback_data="home:start")]]
    )
    await query.edit_message_text("Select a project:", reply_markup=keyboard)


async def _show_project_menu(query: CallbackQuery, project_id_str: str, admin_chat_id: int) -> None:
    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, uuid.UUID(project_id_str), admin_chat_id)

    if project is None:
        await query.edit_message_text("❌ Project not found.")
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📋 Events", callback_data=f"menu:events:{project_id_str}"),
                InlineKeyboardButton("📈 Reports", callback_data=f"menu:reports:{project_id_str}"),
            ],
            [
                InlineKeyboardButton(
                    "👥 Visitors", callback_data=f"menu:visitors:{project_id_str}"
                ),
                InlineKeyboardButton("🔔 Alerts", callback_data=f"menu:alerts:{project_id_str}"),
            ],
            [
                InlineKeyboardButton("🔀 Funnels", callback_data=f"menu:funnels:{project_id_str}"),
                InlineKeyboardButton("⚙️ Settings", callback_data=f"menu:settings:{project_id_str}"),
            ],
            [
                InlineKeyboardButton("🗑 Delete", callback_data=f"del_ask:{project_id_str}"),
                InlineKeyboardButton("« Back", callback_data="back:projects"),
            ],
        ]
    )
    await query.edit_message_text(
        f"📊 <b>{html.escape(project.name)}</b>\n─────────────────\nWhat would you like to do?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def _ask_delete_confirmation(query: CallbackQuery, project_id_str: str) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes, delete", callback_data=f"del_yes:{project_id_str}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"del_no:{project_id_str}"),
            ]
        ]
    )
    await query.edit_message_text(
        "⚠️ <b>Delete project?</b>\n\n"
        "This will permanently remove the project and <b>all its events</b>. "
        "This cannot be undone.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def _confirm_delete(query: CallbackQuery, project_id_str: str, admin_chat_id: int) -> None:
    factory = get_session_factory()
    async with factory() as session:
        deleted = await delete_project(session, uuid.UUID(project_id_str), admin_chat_id)
        await session.commit()

    if deleted:
        await query.edit_message_text("✅ Project deleted.")
    else:
        await query.edit_message_text("❌ Project not found.")
