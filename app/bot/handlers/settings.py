"""Settings handler: retention period, domain allowlist, and API key management."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update

from app.bot.states import BotStateService
from app.core.database import get_session_factory
from app.core.security import generate_api_key, hash_api_key
from app.models.bot_conversation_state import BotConversationState
from app.models.project import Project
from app.models.settings import ProjectSettings
from app.services.audit import write_audit
from app.services.events import normalize_origin_entries
from app.services.projects import get_project

# ── Menu display ──────────────────────────────────────────────────────────────


async def show_settings_menu(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Display current project settings with edit buttons."""
    pid = uuid.UUID(project_id_str)

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
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
            [
                InlineKeyboardButton(
                    "🔑 Recreate API key", callback_data=f"key_ask:{project_id_str}"
                ),
            ],
            [InlineKeyboardButton("« Back", callback_data=f"proj:{project_id_str}")],
        ]
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


# ── Flow starters (callback → conversation state) ─────────────────────────────


async def start_set_retention(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Kick off the retention-days conversation flow."""
    assert isinstance(query.message, Message)
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

    from app.core.config import get_settings

    cap = get_settings().max_retention_days
    if cap is None:
        body = (
            "How many days should raw events be kept?\n"
            "Enter <b>0</b> to keep events forever.\n\n"
            "<i>Type a number (e.g. 30, 90, 365):</i>"
        )
    else:
        body = (
            f"How many days should raw events be kept?\n"
            f"This deployment caps retention at <b>{cap} days</b>.\n\n"
            f"<i>Type a number between 1 and {cap}:</i>"
        )

    await query.edit_message_text(
        f"📅 <b>Set retention period</b>\n\n{body}",
        parse_mode="HTML",
    )


def _build_allowlist_prompt(
    project_name: str, project_id_str: str
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the domain-allowlist prompt text + keyboard for a given project."""
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🌍 Allow all origins", callback_data=f"allow_all:{project_id_str}"
                )
            ],
        ]
    )
    text = (
        f"🌐 <b>Domain allowlist for {project_name}</b>\n\n"
        "Enter allowed domains, comma-separated. "
        "Use <code>*.example.com</code> to match any subdomain.\n\n"
        "<i>Example: myapp.com, *.myapp.com</i>\n\n"
        "Only browser traffic is checked — backend SDK calls are "
        "authenticated by the API key alone."
    )
    return text, keyboard


async def _save_allowlist_state(chat_id: int, project_id_str: str) -> None:
    """Persist the conversation state so the next text reply is treated as an allowlist."""
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


async def start_set_allowlist(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Kick off the domain-allowlist conversation flow."""
    assert isinstance(query.message, Message)
    chat_id = query.message.chat_id

    try:
        pid = uuid.UUID(project_id_str)
    except ValueError:
        await query.edit_message_text("❌ Invalid project reference.")
        return

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return
        project_name = project.name

    await _save_allowlist_state(chat_id, project_id_str)

    text, keyboard = _build_allowlist_prompt(project_name, project_id_str)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def prompt_allowlist_after_create(
    message: Message, project_id_str: str, project_name: str
) -> None:
    """Send the domain-allowlist prompt as a fresh message (used right after /add)."""
    await _save_allowlist_state(message.chat_id, project_id_str)
    text, keyboard = _build_allowlist_prompt(project_name, project_id_str)
    await message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_allow_all(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Clear the domain allowlist (allow all origins) via button callback."""
    assert isinstance(query.message, Message)
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


async def handle_set_retention_text(
    update: Update, session: AsyncSession, svc: BotStateService, state: BotConversationState
) -> None:
    """Process the user's retention-days input."""
    assert update.effective_chat is not None
    assert update.message is not None
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    from app.core.config import get_settings

    cap = get_settings().max_retention_days

    try:
        days = int(text)
        if days < 0:
            raise ValueError
    except ValueError:
        if cap is None:
            await update.message.reply_text(
                "⚠️ Please enter a non-negative integer (e.g. 30, 90, 0 for forever)."
            )
        else:
            await update.message.reply_text(f"⚠️ Please enter an integer between 1 and {cap}.")
        return

    # On capped deployments (cloud free tier), 0 = "forever" is not
    # allowed because it would bypass the cap entirely; nor is any
    # value above the cap.
    if cap is not None and (days == 0 or days > cap):
        await update.message.reply_text(
            f"⚠️ This deployment limits retention to <b>{cap} days</b>. "
            f"Please pick a value between 1 and {cap}.",
            parse_mode="HTML",
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


async def handle_set_allowlist_text(
    update: Update, session: AsyncSession, svc: BotStateService, state: BotConversationState
) -> None:
    """Process the user's domain-allowlist input."""
    assert update.effective_chat is not None
    assert update.message is not None
    chat_id = update.effective_chat.id
    raw = (update.message.text or "").strip()

    project_id_str = state.payload.get("project_id", "")
    try:
        pid = uuid.UUID(project_id_str)
    except ValueError:
        await svc.clear(chat_id)
        await update.message.reply_text("❌ Invalid project reference. Please start over.")
        return

    domains = normalize_origin_entries(raw.split(","))

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


# ── API key rotation ──────────────────────────────────────────────────────────


async def ask_recreate_api_key(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Prompt the user to confirm rotating the project's API key."""
    try:
        pid = uuid.UUID(project_id_str)
    except ValueError:
        await query.edit_message_text("❌ Invalid project reference.")
        return

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes, recreate", callback_data=f"key_yes:{project_id_str}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"menu:settings:{project_id_str}"),
            ]
        ]
    )
    await query.edit_message_text(
        "⚠️ <b>Recreate API key?</b>\n\n"
        "The current key will stop working <b>immediately</b>. "
        "You'll need to update every SDK / integration with the new key.\n\n"
        "This cannot be undone.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def confirm_recreate_api_key(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Generate and persist a new API key for the project, then reveal it once."""
    try:
        pid = uuid.UUID(project_id_str)
    except ValueError:
        await query.edit_message_text("❌ Invalid project reference.")
        return

    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, pid, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        new_key = generate_api_key()
        project.api_key_hash = hash_api_key(new_key)
        await write_audit(
            session,
            user_id=owner_user_id,
            action="project.api_key.rotate",
            target_type="project",
            target_id=str(project.id),
            metadata={"name": project.name},
        )
        await session.commit()

    from app.core.config import get_settings

    base = get_settings().webhook_base_url.rstrip("/") or "https://your-server.com"
    env_block = f"TGA_URL={base}\nTGA_API_KEY={new_key}"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "« Back to settings", callback_data=f"menu:settings:{project_id_str}"
                )
            ]
        ]
    )
    await query.edit_message_text(
        "🔑 <b>New API key generated.</b>\n\n"
        "⚠️ Save it now — it won't be shown again.\n\n"
        f"<b>Env:</b>\n<tg-spoiler><pre>{env_block}</pre></tg-spoiler>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
