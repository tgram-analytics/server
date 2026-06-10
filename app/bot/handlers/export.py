"""Export action: dump a project's raw event data as a CSV document.

Reached via the "📦 Export CSV" button on the project menu (callback
``exp:<project_id>``). Every row of the ``events`` table for the project
is streamed into an in-memory CSV and sent back as a Telegram document.
Properties are serialised as a JSON string column so nothing is lost.

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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from app.bot.auth import requires_user
from app.models.event import Event
from app.models.user import User
from app.services.projects import get_project

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


@requires_user
async def export_callback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """Handle exp:<project_id> — export the project's events as CSV."""
    query = update.callback_query
    assert query is not None
    await query.answer()
    assert isinstance(query.message, Message)

    data: str = query.data or ""

    try:
        pid = uuid.UUID(data[4:])
    except ValueError:
        await query.edit_message_text("❌ Invalid export target.")
        return

    project = await get_project(session, pid, user.id)
    if project is None:
        await query.edit_message_text("❌ Project not found.")
        return

    back_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("« Back", callback_data=f"proj:{pid}")]]
    )

    csv_bytes, count = await _build_csv(session, pid)
    if count == 0:
        await query.edit_message_text(
            f"📭 <b>{html.escape(project.name)}</b> — no events to export.",
            parse_mode="HTML",
            reply_markup=back_keyboard,
        )
        return

    date_str = datetime.now(UTC).strftime("%Y%m%d")
    await query.message.reply_document(
        document=csv_bytes,
        filename=f"{_safe_filename(project.name)}-events-{date_str}.csv",
        caption=f"📦 {project.name} · {count:,} event{'s' if count != 1 else ''}",
    )
    await query.edit_message_text(
        f"📦 <b>{html.escape(project.name)}</b> — export sent ↓",
        parse_mode="HTML",
        reply_markup=back_keyboard,
    )
