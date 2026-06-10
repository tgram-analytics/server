"""Export command: dump raw event data as CSV documents.

/export shows a project picker (plus an "All projects" option). Selecting
a target streams every row of the ``events`` table for that project into
an in-memory CSV and sends it back as a Telegram document. Properties are
serialised as a JSON string column so nothing is lost.

Exports are built in memory — fine for self-host scale, but Telegram caps
bot uploads at 50 MB per document; very large projects will fail to send.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from app.bot.auth import requires_user
from app.models.event import Event
from app.models.project import Project
from app.models.user import User
from app.services.projects import get_project, list_projects

_CSV_COLUMNS = [
    "id",
    "event_name",
    "timestamp",
    "received_at",
    "session_id",
    "url",
    "referrer",
    "visitor_hash",
    "browser",
    "os",
    "device_type",
    "properties",
]


def _safe_filename(name: str) -> str:
    """Strip characters that are awkward in filenames, keep dots/dashes."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "project"


def _isoformat(ts: datetime | None) -> str:
    return ts.isoformat() if ts is not None else ""


async def _build_csv(session: AsyncSession, project_id: uuid.UUID) -> tuple[bytes, int]:
    """Serialise all events of a project to CSV bytes; returns (data, row count)."""
    result = await session.stream(
        select(Event).where(Event.project_id == project_id).order_by(Event.timestamp)
    )

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS)
    writer.writeheader()
    count = 0
    async for (event,) in result:
        writer.writerow(
            {
                "id": str(event.id),
                "event_name": event.event_name,
                "timestamp": _isoformat(event.timestamp),
                "received_at": _isoformat(event.received_at),
                "session_id": event.session_id,
                "url": event.url or "",
                "referrer": event.referrer or "",
                "visitor_hash": event.visitor_hash or "",
                "browser": event.browser or "",
                "os": event.os or "",
                "device_type": event.device_type or "",
                "properties": json.dumps(event.properties or {}, sort_keys=True),
            }
        )
        count += 1
    return buf.getvalue().encode("utf-8"), count


async def _send_project_export(message: Message, session: AsyncSession, project: Project) -> bool:
    """Build and send the CSV for one project. Returns True if a file was sent."""
    data, count = await _build_csv(session, project.id)
    if count == 0:
        return False

    date_str = datetime.now(UTC).strftime("%Y%m%d")
    await message.reply_document(
        document=data,
        filename=f"{_safe_filename(project.name)}-events-{date_str}.csv",
        caption=f"📦 {project.name} · {count:,} event{'s' if count != 1 else ''}",
    )
    return True


# ── /export command ────────────────────────────────────────────────────────────


@requires_user
async def export_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """Show project list so the user can pick what to export."""
    assert update.message is not None

    projects = await list_projects(session, user.id)

    if not projects:
        await update.message.reply_text(
            "📭 No projects yet.\n\nUse /add <i>name</i> to create one.",
            parse_mode="HTML",
        )
        return

    rows = [[InlineKeyboardButton(f"📦 {p.name}", callback_data=f"exp:{p.id}")] for p in projects]
    rows.append([InlineKeyboardButton("🗃 All projects", callback_data="exp:all")])

    await update.message.reply_text(
        "Select a project to export as CSV:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ── Callback ───────────────────────────────────────────────────────────────────


@requires_user
async def export_callback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """Handle export target selection: exp:all or exp:<project_id>."""
    query = update.callback_query
    assert query is not None
    await query.answer()
    assert isinstance(query.message, Message)

    data: str = query.data or ""

    if data == "exp:all":
        await _export_all(query, session, user.id)
        return

    try:
        pid = uuid.UUID(data[4:])
    except ValueError:
        await query.edit_message_text("❌ Invalid export target.")
        return

    project = await get_project(session, pid, user.id)
    if project is None:
        await query.edit_message_text("❌ Project not found.")
        return

    sent = await _send_project_export(query.message, session, project)
    if sent:
        await query.edit_message_text(
            f"📦 <b>{html.escape(project.name)}</b> — export sent ↓",
            parse_mode="HTML",
        )
    else:
        await query.edit_message_text(
            f"📭 <b>{html.escape(project.name)}</b> — no events to export.",
            parse_mode="HTML",
        )


async def _export_all(
    query: CallbackQuery, session: AsyncSession, owner_user_id: uuid.UUID
) -> None:
    """Send one CSV per project that has events."""
    assert isinstance(query.message, Message)

    projects = await list_projects(session, owner_user_id)
    if not projects:
        await query.edit_message_text("📭 No projects to export.")
        return

    await query.edit_message_text(
        f"📦 Exporting {len(projects)} project{'s' if len(projects) != 1 else ''}…"
    )

    sent = 0
    for project in projects:
        if await _send_project_export(query.message, session, project):
            sent += 1

    if sent == 0:
        await query.edit_message_text("📭 No events to export in any project.")
