"""Doctor command: health check across all of the user's projects.

Runs cheap per-project checks and renders a single-message summary so the
admin can spot misconfigured or silent projects at a glance:

* Has the project ever received an event? When was the most recent one?
* Is the domain allowlist effectively open (empty)? An open allowlist
  means any browser origin can POST events to ``/api/v1/track`` — fine
  for a backend-only SDK, dangerous for a publicly-deployed JS snippet.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.auth import requires_user
from app.models.event import Event
from app.models.user import User
from app.services.projects import list_projects

_STALE_THRESHOLD = timedelta(days=7)


def _relative(now: datetime, ts: datetime | None) -> str:
    if ts is None:
        return "never"
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{max(secs, 0)}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


@requires_user
async def doctor_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    assert update.message is not None

    projects = await list_projects(session, user.id)
    if not projects:
        await update.message.reply_text(
            "🩺 <b>Doctor</b>\n\nNo projects yet.\n\nUse /add <i>name</i> to create one.",
            parse_mode="HTML",
        )
        return

    now = datetime.now(UTC)
    sections: list[str] = ["🩺 <b>Doctor — health report</b>", "─────────────────"]
    issues = 0

    for project in projects:
        result = await session.execute(
            select(
                func.count().label("total"),
                func.max(Event.received_at).label("last_seen"),
            ).where(Event.project_id == project.id)
        )
        row = result.one()
        total: int = int(row.total or 0)
        last_seen: datetime | None = row.last_seen

        lines = [f"📊 <b>{html.escape(project.name)}</b>"]

        if total == 0:
            lines.append("  ❌ No events received yet")
            issues += 1
        elif last_seen is None or (now - last_seen) > _STALE_THRESHOLD:
            lines.append(f"  ⚠️ Events: {total:,} (last {_relative(now, last_seen)} — stale)")
            issues += 1
        else:
            lines.append(f"  ✅ Events: {total:,} (last {_relative(now, last_seen)})")

        allowlist = project.domain_allowlist or []
        if not allowlist:
            lines.append("  ⚠️ Origin allowlist: <b>open</b> — any site can POST events")
            issues += 1
        else:
            label = ", ".join(allowlist)
            lines.append(f"  ✅ Origin allowlist: <code>{html.escape(label)}</code>")

        sections.append("\n".join(lines))

    sections.append("─────────────────")
    if issues == 0:
        sections.append("✅ <b>All checks passed.</b>")
    else:
        sections.append(f"⚠️ <b>{issues} issue{'s' if issues != 1 else ''} detected.</b>")

    await update.message.reply_text("\n\n".join(sections), parse_mode="HTML")
