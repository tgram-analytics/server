"""Settings handler: retention period and domain allowlist management."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy import update as sql_update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.states import BotStateService
from app.core.database import get_session_factory
from app.models.project import Project
from app.models.settings import ProjectSettings
from app.services.projects import get_project

# ── Menu display ──────────────────────────────────────────────────────────────


async def show_settings_menu(query, project_id_str: str, admin_chat_id: int) -> None:
    """Display current project settings with edit buttons."""
    pid = uuid.UUID(project_id_str)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, admin_chat_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        settings_result = await session.execute(
            select(ProjectSettings).where(ProjectSettings.project_id == pid)
        )
        ps = settings_result.scalar_one_or_none()

    retention = ps.retention_days if ps else 90
    retention_label = f"{retention} days" if retention > 0 else "Forever"

    allowlist = project.domain_allowlist or []
    allowlist_label = ", ".join(allowlist) if allowlist else "All origins allowed"

    text = (
        f"⚙️ <b>Settings: {project.name}</b>\n"
        "─────────────────\n"
        f"📅 Retention: <b>{retention_label}</b>\n"
        f"🌐 Allowlist: <b>{allowlist_label}</b>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Retention", callback_data=f"set_ret:{project_id_str}"),
                InlineKeyboardButton("🌐 Allowlist", callback_data=f"set_dom:{project_id_str}"),
            ],
            [InlineKeyboardButton("« Back", callback_data=f"proj:{project_id_str}")],
        ]
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


# ── Flow starters (callback → conversation state) ─────────────────────────────


async def start_set_retention(query, project_id_str: str, admin_chat_id: int) -> None:
    """Kick off the retention-days conversation flow."""
    chat_id = query.message.chat_id

    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        await svc.save(
            chat_id,
            flow="set_retention",
            step="value",
            payload={"project_id": project_id_str},
        )
        await session.commit()

    await query.edit_message_text(
        "📅 <b>Set retention period</b>\n\n"
        "How many days should raw events be kept?\n"
        "Enter <b>0</b> to keep events forever.\n\n"
        "<i>Type a number (e.g. 30, 90, 365):</i>",
        parse_mode="HTML",
    )


async def start_set_allowlist(query, project_id_str: str, admin_chat_id: int) -> None:
    """Kick off the domain-allowlist conversation flow."""
    chat_id = query.message.chat_id

    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        await svc.save(
            chat_id,
            flow="set_allowlist",
            step="value",
            payload={"project_id": project_id_str},
        )
        await session.commit()

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🌍 Allow all origins", callback_data=f"allow_all:{project_id_str}"
                )
            ],
        ]
    )
    await query.edit_message_text(
        "🌐 <b>Domain allowlist</b>\n\n"
        "Enter allowed domains, comma-separated.\n\n"
        "<i>Example: myapp.com, api.myapp.com</i>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def handle_allow_all(query, project_id_str: str, admin_chat_id: int) -> None:
    """Clear the domain allowlist (allow all origins) via button callback."""
    chat_id = query.message.chat_id

    try:
        pid = uuid.UUID(project_id_str)
    except ValueError:
        await query.edit_message_text("❌ Invalid project reference.")
        return

    factory = get_session_factory()
    async with factory() as session:
        # Clear conversation state if any
        svc = BotStateService(session)
        await svc.clear(chat_id)

        # Clear the allowlist
        await session.execute(
            sql_update(Project).where(Project.id == pid).values(domain_allowlist=[])
        )
        await session.commit()

    await query.edit_message_text(
        "✅ Allowlist cleared — all origins allowed.",
        parse_mode="HTML",
    )


# ── Text-message handlers (called from alerts.handle_text_message) ────────────


async def handle_set_retention_text(update, session, svc, state) -> None:
    """Process the user's retention-days input."""
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    try:
        days = int(text)
        if days < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Please enter a non-negative integer (e.g. 30, 90, 0 for forever)."
        )
        return

    project_id_str = state.payload.get("project_id", "")
    try:
        pid = uuid.UUID(project_id_str)
    except ValueError:
        await svc.clear(chat_id)
        await update.message.reply_text("❌ Invalid project reference. Please start over.")
        return

    ps_result = await session.execute(
        select(ProjectSettings).where(ProjectSettings.project_id == pid)
    )
    ps = ps_result.scalar_one_or_none()
    if ps:
        ps.retention_days = days
    else:
        session.add(ProjectSettings(project_id=pid, retention_days=days))
    await svc.clear(chat_id)
    await session.commit()

    label = f"{days} days" if days > 0 else "forever"
    await update.message.reply_text(
        f"✅ Retention set to <b>{label}</b>.",
        parse_mode="HTML",
    )


async def handle_set_allowlist_text(update, session, svc, state) -> None:
    """Process the user's domain-allowlist input."""
    chat_id = update.effective_chat.id
    raw = (update.message.text or "").strip()

    project_id_str = state.payload.get("project_id", "")
    try:
        pid = uuid.UUID(project_id_str)
    except ValueError:
        await svc.clear(chat_id)
        await update.message.reply_text("❌ Invalid project reference. Please start over.")
        return

    domains = [d.strip() for d in raw.split(",") if d.strip()]

    await session.execute(
        sql_update(Project).where(Project.id == pid).values(domain_allowlist=domains)
    )
    await svc.clear(chat_id)
    await session.commit()

    if domains:
        label = ", ".join(domains)
        msg = f"✅ Allowlist updated:\n<code>{label}</code>"
    else:
        msg = "✅ Allowlist cleared — all origins allowed."
    await update.message.reply_text(msg, parse_mode="HTML")
