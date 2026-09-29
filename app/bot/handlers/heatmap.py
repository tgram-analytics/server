"""Tap heatmap flow: /heatmap -> project -> page -> device -> period -> result.

The flow state lives in ``bot_conversation_state`` (flow ``heatmap``). Page
paths can be longer than Telegram's 64-byte ``callback_data`` limit, so the
page list is stored in the state payload and the buttons carry an index.
Every step re-checks that the project belongs to the caller.

The result is the ranking text (edited into the picker message, with period
toggles) followed by the heatmap PNG as a document. The document is only
sent for one device bucket, when a renderer is configured and the page is
not behind a login.
"""

from __future__ import annotations

import contextlib
import html
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app.bot.auth import requires_user
from app.bot.constants import PERIOD_LABEL, PERIODS
from app.bot.states import BotStateService
from app.core.config import get_settings
from app.models.project import Project
from app.models.user import User
from app.services import taps as taps_svc
from app.services.analytics import top_properties
from app.services.heatmap import (
    build_site_url,
    fetch_screenshot,
    render_heatmap,
    slugify_path,
)
from app.services.projects import get_project, list_projects

logger = logging.getLogger(__name__)

FLOW = "heatmap"
MAX_PAGES = 8
PAGE_WINDOW = timedelta(days=30)
# Device counts span the longest period the result can show.
DEVICE_WINDOW_KEY = "90d"
DEFAULT_PERIOD = "7d"
TOP_ELEMENTS = 10
DEVICE_CHOICES: tuple[str, ...] = (*taps_svc.DEVICES, taps_svc.ALL_DEVICES)
DEVICE_ICON = {"mobile": "📱", "tablet": "📲", "desktop": "🖥", "all": "🌐"}

MAX_PATH_CHARS = 80
MAX_BUTTON_PATH_CHARS = 48

EXPIRED_TEXT = "❌ Session expired. Send /heatmap to start again."
RENDERER_HINT = "ℹ️ Screenshots need the renderer service (see README)."
LOGIN_WALL_NOTE = "🔒 This page needs a login, so there is no screenshot."
NO_SITE_URL_NOTE = "ℹ️ Add your site to the domain allowlist in ⚙️ Settings to get screenshots."
ALL_DEVICES_NOTE = "ℹ️ Pick one device (mobile, tablet or desktop) to get the screenshot."
SHOT_PENDING_NOTE = "📸 Taking the screenshot…"
SHOT_FAILED_NOTE = "⚠️ The screenshot did not work this time. Try again later."
NO_TAPS_HINT = (
    "Tap heatmaps are off by default in the JS SDK. Turn them on with:\n"
    '<code>TGA.init("proj_…", { heatmaps: true })</code>\n'
    "Taps arrive when a visitor leaves or changes the page."
)


# ── Formatting ──────────────────────────────────────────────────────────────


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _header(
    project: Project, path: str, device: str | None = None, period: str | None = None
) -> str:
    parts = [
        "🔥 <b>Heatmap</b>",
        html.escape(project.name),
        f"<code>{html.escape(_short(path, MAX_PATH_CHARS))}</code>",
    ]
    if device is not None:
        parts.append(html.escape(device))
    if period is not None:
        parts.append(html.escape(PERIOD_LABEL.get(period, period)))
    return " · ".join(parts)


def _ranking_text(
    project: Project,
    path: str,
    device: str,
    period: str,
    rows: list[dict[str, Any]],
    total: int,
    scroll_median: float | None,
    note: str | None = None,
) -> str:
    """The result message: top elements with count and share of taps."""
    lines = [_header(project, path, device, period)]
    if total == 0:
        lines.append("")
        lines.append("📭 No taps on this page in this period.")
        lines.append("")
        lines.append(NO_TAPS_HINT)
    else:
        lines.append(f"{total:,} tap{'s' if total != 1 else ''}")
        for i, row in enumerate(rows, start=1):
            label = row.get("label")
            shown = html.escape(str(label)) if label else "<i>(no label)</i>"
            count = int(row["count"])
            pct = round(count / total * 100)
            lines.append(f"{i}. {shown} — {count:,} ({pct}%)")
    if scroll_median is not None:
        lines.append(f"Scroll depth: median {round(scroll_median * 100)}%")
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def _result_keyboard(period: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"✓ {p}" if p == period else p, callback_data=f"hm:prd:{p}")
                for p in PERIODS
            ],
            [InlineKeyboardButton("« Back", callback_data="hm:back:dev")],
        ]
    )


def _projects_keyboard(projects: list[Project]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"🔥 {p.name}", callback_data=f"hm:proj:{p.id}")] for p in projects]
    )


async def _edit(
    query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Edit the flow message; a click that changes nothing is not an error."""
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


# ── Entry points ────────────────────────────────────────────────────────────


@requires_user
async def heatmap_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """/heatmap: pick a project."""
    assert update.message is not None
    projects = await list_projects(session, user.id)
    if not projects:
        await update.message.reply_text(
            "📭 No projects yet.\n\nUse /add <i>name</i> to create one.",
            parse_mode="HTML",
        )
        return
    await update.message.reply_text(
        "🔥 <b>Tap heatmap</b>\nPick a project:",
        parse_mode="HTML",
        reply_markup=_projects_keyboard(projects),
    )


@requires_user
async def heatmap_callback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """Dispatch every ``hm:`` callback."""
    query = update.callback_query
    assert query is not None
    await query.answer()

    _prefix, _, rest = (query.data or "").partition(":")
    action, _, arg = rest.partition(":")

    if action == "proj":
        await _pick_page(query, session, user.id, arg)
    elif action == "page":
        await _pick_device(query, session, user.id, arg)
    elif action == "dev":
        await _pick_period(query, session, user.id, arg)
    elif action == "prd":
        await _send_heatmap(query, session, user.id, arg)
    elif action == "back":
        await _go_back(query, session, user.id, arg)


# ── Steps ───────────────────────────────────────────────────────────────────


async def _load_state(
    query: CallbackQuery, session: AsyncSession, owner_user_id: uuid.UUID
) -> tuple[dict[str, Any], Project] | None:
    """Return ``(payload, project)`` for the running flow, or reply and return None."""
    assert isinstance(query.message, Message)
    chat_id = query.message.chat_id
    svc = BotStateService(session)
    state = await svc.get(chat_id)
    if state is None or state.flow != FLOW:
        await _edit(query, EXPIRED_TEXT)
        return None
    payload = dict(state.payload or {})
    try:
        pid = uuid.UUID(str(payload.get("project_id", "")))
    except ValueError:
        pid = None
    project = await get_project(session, pid, owner_user_id) if pid else None
    if project is None:
        await svc.clear(chat_id)
        await session.commit()
        await _edit(query, "❌ Project not found.")
        return None
    return payload, project


def _page_of(payload: dict[str, Any]) -> str | None:
    pages = payload.get("pages")
    index = payload.get("page")
    if not isinstance(pages, list) or not isinstance(index, int):
        return None
    if not 0 <= index < len(pages):
        return None
    return str(pages[index])


async def _pick_page(
    query: CallbackQuery, session: AsyncSession, owner_user_id: uuid.UUID, project_id_str: str
) -> None:
    """Step 2: the top pageview URLs of the last 30 days."""
    assert isinstance(query.message, Message)
    chat_id = query.message.chat_id
    try:
        pid = uuid.UUID(project_id_str)
    except ValueError:
        await _edit(query, "❌ Invalid project reference.")
        return
    project = await get_project(session, pid, owner_user_id)
    if project is None:
        await _edit(query, "❌ Project not found.")
        return

    now = datetime.now(UTC)
    rows = await top_properties(
        session,
        project_id=pid,
        event_name="pageview",
        property_key="url",
        start=now - PAGE_WINDOW,
        end=now,
        limit=MAX_PAGES,
    )
    rows = [r for r in rows if r.get("value")]
    back_row = [InlineKeyboardButton("« Projects", callback_data="hm:back:proj")]
    if not rows:
        await BotStateService(session).clear(chat_id)
        await session.commit()
        await _edit(
            query,
            f"🔥 <b>Heatmap</b> · {html.escape(project.name)}\n\n"
            "📭 No pageviews in the last 30 days, so there is no page to pick.",
            InlineKeyboardMarkup([back_row]),
        )
        return

    pages = [str(r["value"]) for r in rows]
    await BotStateService(session).save(
        chat_id, flow=FLOW, step="page", payload={"project_id": str(pid), "pages": pages}
    )
    await session.commit()

    buttons = [
        [
            InlineKeyboardButton(
                f"{_short(page, MAX_BUTTON_PATH_CHARS)}  ({int(r['count']):,})",
                callback_data=f"hm:page:{i}",
            )
        ]
        for i, (page, r) in enumerate(zip(pages, rows, strict=True))
    ]
    buttons.append(back_row)
    await _edit(
        query,
        f"🔥 <b>Heatmap</b> · {html.escape(project.name)}\n"
        "Pick a page (most viewed in the last 30 days):",
        InlineKeyboardMarkup(buttons),
    )


async def _pick_device(
    query: CallbackQuery, session: AsyncSession, owner_user_id: uuid.UUID, index_str: str
) -> None:
    """Step 3: device bucket, with tap counts over the longest period."""
    assert isinstance(query.message, Message)
    loaded = await _load_state(query, session, owner_user_id)
    if loaded is None:
        return
    payload, project = loaded
    # "hm:back:dev" passes no index: keep the page already in the state.
    with contextlib.suppress(ValueError):
        payload["page"] = int(index_str)
    path = _page_of(payload)
    if path is None:
        await _edit(query, EXPIRED_TEXT)
        return

    now = datetime.now(UTC)
    counts = await taps_svc.device_counts(
        session,
        project_id=project.id,
        path=path,
        start=now - PERIODS[DEVICE_WINDOW_KEY],
        end=now,
    )
    total = sum(counts.values())
    payload.pop("device", None)
    await BotStateService(session).save(
        query.message.chat_id, flow=FLOW, step="device", payload=payload
    )
    await session.commit()

    back_row = [InlineKeyboardButton("« Pages", callback_data="hm:back:page")]
    if total == 0:
        await _edit(
            query,
            f"{_header(project, path)}\n\n📭 No taps on this page yet.\n\n{NO_TAPS_HINT}",
            InlineKeyboardMarkup([back_row]),
        )
        return

    # Default = the bucket with the most taps (ties: mobile, tablet, desktop).
    default = max(taps_svc.DEVICES, key=lambda d: counts.get(d, 0))

    def button(device: str, n: int) -> InlineKeyboardButton:
        mark = "✓ " if device == default else ""
        return InlineKeyboardButton(
            f"{mark}{DEVICE_ICON[device]} {device} ({n:,})", callback_data=f"hm:dev:{device}"
        )

    rows = [
        [button("mobile", counts.get("mobile", 0)), button("tablet", counts.get("tablet", 0))],
        [button("desktop", counts.get("desktop", 0)), button("all", total)],
        back_row,
    ]
    await _edit(
        query,
        f"{_header(project, path)}\nPick a device (taps in the last 90 days):",
        InlineKeyboardMarkup(rows),
    )


async def _pick_period(
    query: CallbackQuery, session: AsyncSession, owner_user_id: uuid.UUID, device: str
) -> None:
    """Step 4: period, 7 days highlighted."""
    assert isinstance(query.message, Message)
    if device not in DEVICE_CHOICES:
        await _edit(query, EXPIRED_TEXT)
        return
    loaded = await _load_state(query, session, owner_user_id)
    if loaded is None:
        return
    payload, project = loaded
    path = _page_of(payload)
    if path is None:
        await _edit(query, EXPIRED_TEXT)
        return
    payload["device"] = device
    await BotStateService(session).save(
        query.message.chat_id, flow=FLOW, step="period", payload=payload
    )
    await session.commit()

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"✓ {p}" if p == DEFAULT_PERIOD else p, callback_data=f"hm:prd:{p}"
                )
                for p in PERIODS
            ],
            [InlineKeyboardButton("« Back", callback_data="hm:back:dev")],
        ]
    )
    await _edit(query, f"{_header(project, path, device)}\nPick a period:", keyboard)


async def _send_heatmap(
    query: CallbackQuery, session: AsyncSession, owner_user_id: uuid.UUID, period: str
) -> None:
    """Step 5: ranking text first, then the heatmap PNG as a document."""
    assert isinstance(query.message, Message)
    if period not in PERIODS:
        await _edit(query, EXPIRED_TEXT)
        return
    loaded = await _load_state(query, session, owner_user_id)
    if loaded is None:
        return
    payload, project = loaded
    path = _page_of(payload)
    device = payload.get("device")
    if path is None or device not in DEVICE_CHOICES:
        await _edit(query, EXPIRED_TEXT)
        return
    assert isinstance(device, str)

    payload["period"] = period
    await BotStateService(session).save(
        query.message.chat_id, flow=FLOW, step="result", payload=payload
    )

    now = datetime.now(UTC)
    window: dict[str, Any] = {
        "project_id": project.id,
        "path": path,
        "device": device,
        "start": now - PERIODS[period],
        "end": now,
    }
    total = await taps_svc.count_taps(session, **window)
    rows = await taps_svc.top_elements(session, **window, limit=TOP_ELEMENTS) if total else []
    scroll = await taps_svc.scroll_depth_median(session, **window)

    # Decide whether a screenshot is possible before any slow network call.
    site_url: str | None = None
    note: str | None = None
    if total == 0:
        note = None
    elif device == taps_svc.ALL_DEVICES:
        note = ALL_DEVICES_NOTE
    elif not get_settings().screenshot_url.strip():
        note = RENDERER_HINT
    else:
        site_url = build_site_url(project, path)
        if site_url is None:
            note = NO_SITE_URL_NOTE

    points: list[tuple[float, int]] = []
    median_vw: int | None = None
    if site_url is not None:
        points = await taps_svc.tap_points(session, **window)
        median_vw = await taps_svc.median_viewport_width(session, **window)
    # Release the DB connection before the screenshot request (seconds).
    await session.commit()

    keyboard = _result_keyboard(period)

    def text(final_note: str | None) -> str:
        return _ranking_text(project, path, device, period, rows, total, scroll, final_note)

    if site_url is None:
        await _edit(query, text(note), keyboard)
        return

    await _edit(query, text(SHOT_PENDING_NOTE), keyboard)

    shot = await fetch_screenshot(site_url, device, median_vw)
    final_note: str | None
    if shot is None:
        final_note = SHOT_FAILED_NOTE
    elif shot.login_wall:
        final_note = LOGIN_WALL_NOTE
    else:
        final_note = None
        try:
            png = await render_heatmap(shot, points)
            await query.message.reply_document(
                document=png,
                filename=f"heatmap-{slugify_path(path)}-{device}-{period}.png",
                caption=(
                    f"🔥 {_short(project.name, 64)} · {_short(path, MAX_PATH_CHARS)}"
                    f" · {device} · {PERIOD_LABEL[period]}"
                ),
            )
        except Exception:
            logger.warning("heatmap image could not be sent", exc_info=True)
            final_note = SHOT_FAILED_NOTE

    await _edit(query, text(final_note), keyboard)


async def _go_back(
    query: CallbackQuery, session: AsyncSession, owner_user_id: uuid.UUID, target: str
) -> None:
    if target == "proj":
        assert isinstance(query.message, Message)
        await BotStateService(session).clear(query.message.chat_id)
        await session.commit()
        projects = await list_projects(session, owner_user_id)
        if not projects:
            await _edit(query, "📭 No projects yet.\n\nUse /add <i>name</i> to create one.")
            return
        await _edit(query, "🔥 <b>Tap heatmap</b>\nPick a project:", _projects_keyboard(projects))
        return

    loaded = await _load_state(query, session, owner_user_id)
    if loaded is None:
        return
    payload, project = loaded
    if target == "page":
        await _pick_page(query, session, owner_user_id, str(project.id))
    elif target == "dev":
        await _pick_device(query, session, owner_user_id, str(payload.get("page", "")))
