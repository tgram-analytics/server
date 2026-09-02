"""Alert management handlers: alerts menu, add/delete/toggle alerts."""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime, timedelta
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
from app.bot.states import BotStateService
from app.core.database import get_session_factory
from app.models.alert import Alert, AlertCondition
from app.models.user import User
from app.services.alerts import (
    create_alert,
    delete_alert,
    disable_alert,
    get_active_alerts_across_projects,
    get_alert,
    list_alerts,
    mute_alert,
    toggle_alert,
)
from app.services.analytics import (
    events_over_time,
    list_event_names,
    list_property_keys,
    top_properties,
)
from app.services.charts import ChartGenerationError, generate_line_chart, generate_pie_chart
from app.services.projects import get_project


def condition_keyboard() -> InlineKeyboardMarkup:
    """Every / Every N / Threshold buttons for the add-alert flow (``alert_cond:*``)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Every", callback_data="alert_cond:every"),
                InlineKeyboardButton("Every N", callback_data="alert_cond:every_n"),
                InlineKeyboardButton("Threshold", callback_data="alert_cond:threshold"),
            ]
        ]
    )


def condition_prompt(event_name: str) -> str:
    """HTML text asking which condition to use for *event_name* (Add Alert header + bullet legend)."""
    return (
        f"📝 <b>Add Alert</b>\n\n"
        f"Event: <b>{html.escape(event_name)}</b>\n\n"
        f"Choose when to notify:\n"
        f"• <b>Every</b> — on every occurrence\n"
        f"• <b>Every N</b> — every Nth occurrence\n"
        f"• <b>Threshold</b> — when count exceeds N per day"
    )


def _format_alert_label(alert: Alert) -> str:
    """Format an alert for display in the list."""
    status = "✅" if alert.is_active else "⏸️"
    if alert.condition == AlertCondition.every:
        return f"{status} {alert.event_name} (every)"
    elif alert.condition == AlertCondition.every_n:
        return f"{status} {alert.event_name} (every {alert.threshold_n})"
    else:  # threshold
        return f"{status} {alert.event_name} (>{alert.threshold_n}/day)"


async def show_alerts_menu(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Display the alerts list for a project with action buttons."""
    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, uuid.UUID(project_id_str), owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        alerts = await list_alerts(session, project.id)

    rows: list[list[InlineKeyboardButton]] = []

    for alert in alerts:
        label = _format_alert_label(alert)
        toggle_icon = "⏸️" if alert.is_active else "▶️"
        aid = str(alert.id)
        rows.append(
            [
                InlineKeyboardButton(label, callback_data="alert_noop"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(toggle_icon, callback_data=f"alert_t:{aid}"),
                InlineKeyboardButton("🗑", callback_data=f"alert_d:{aid}"),
            ]
        )

    rows.append([InlineKeyboardButton("➕ Add alert", callback_data=f"alert_add:{project_id_str}")])
    rows.append([InlineKeyboardButton("« Back", callback_data=f"proj:{project_id_str}")])

    keyboard = InlineKeyboardMarkup(rows)
    await query.edit_message_text(
        f"🔔 <b>Alerts for {html.escape(project.name)}</b>\n─────────────────",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@requires_user
async def alerts_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """Handle /alerts — list all active alerts across all projects."""
    assert update.message is not None

    rows = await get_active_alerts_across_projects(session, user.id)

    if not rows:
        await update.message.reply_text("No active alerts.", parse_mode="HTML")
        return

    # Group by project name
    by_project: dict[str, list[Alert]] = {}
    for alert, project_name in rows:
        by_project.setdefault(project_name, []).append(alert)

    lines = ["🔔 <b>Active Alerts</b>\n"]
    for project_name, alerts in by_project.items():
        lines.append(f"📁 <b>{html.escape(project_name)}</b>")
        for alert in alerts:
            if alert.condition == AlertCondition.every:
                desc = "every occurrence"
            elif alert.condition == AlertCondition.every_n:
                desc = f"every {alert.threshold_n} occurrences"
            else:
                desc = f">{alert.threshold_n}/day"
            lines.append(f"  • {html.escape(alert.event_name)} ({desc})")
        lines.append("")

    total = sum(len(v) for v in by_project.values())
    lines.append(f"<i>Total: {total} active alert{'s' if total != 1 else ''}</i>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@requires_user
async def alert_callback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """Handle all alert-related callbacks."""
    query = update.callback_query
    assert query is not None

    owner_user_id = user.id
    data: str = query.data or ""

    if data.startswith("alert_meta:"):
        from app.bot.event_meta_cache import get as _get_meta

        token = data[11:]
        text = _get_meta(token)
        await query.answer(
            text=text or "Details expired — re-trigger the event to see them.",
            show_alert=True,
        )
        return

    await query.answer()

    if data.startswith("alert_add:"):
        project_id_str = data[10:]
        await _start_add_alert(query, project_id_str, owner_user_id)

    elif data.startswith("alert_cond:"):
        condition = data[11:]
        await _handle_condition_choice(query, condition, owner_user_id)

    elif data.startswith("alert_d:"):
        alert_id_str = data[8:]
        await _delete_alert(query, alert_id_str, owner_user_id)

    elif data.startswith("alert_t:"):
        alert_id_str = data[8:]
        await _toggle_alert(query, alert_id_str, owner_user_id)

    elif data.startswith("alert_sil:"):
        rest = data[10:]  # "{alert_id}" or "{alert_id}:{hours}"
        if ":" in rest:
            alert_id_str, hours_str = rest.split(":", 1)
            await _apply_silence(query, alert_id_str, int(hours_str), owner_user_id)
        else:
            await _show_silence_picker(query, rest)

    elif data.startswith("alert_dis:"):
        alert_id_str = data[10:]
        await _disable_alert_from_notification(query, alert_id_str, owner_user_id)

    elif data.startswith("alert_pie:"):
        rest = data[10:]  # "{alert_id}" or "{alert_id}:{keys_token}"
        if ":" in rest:
            alert_id_str, keys_token = rest.split(":", 1)
        else:
            alert_id_str, keys_token = rest, None
        await _send_charts_from_notification(
            query, alert_id_str, owner_user_id, keys_token=keys_token
        )

    elif data.startswith("alert_ev:"):
        event_name = data[9:]
        await _pick_event_for_alert(query, event_name, owner_user_id)

    elif data == "alert_noop":
        pass

    elif data.startswith("back:alerts:"):
        project_id_str = data[12:]
        assert isinstance(query.message, Message)
        factory = get_session_factory()
        async with factory() as session:
            await BotStateService(session).clear(query.message.chat_id)
            await session.commit()
        await show_alerts_menu(query, project_id_str, owner_user_id)


async def _start_add_alert(
    query: CallbackQuery, project_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Start the add-alert conversation flow.

    Verifies the caller owns ``project_id_str`` BEFORE seeding conversation
    state; otherwise a caller could begin creating an alert on another
    tenant's project (IDOR). The verified owner is stashed in the payload so
    the completion path can re-verify before the final ``create_alert``.
    """
    assert isinstance(query.message, Message)
    chat_id = query.message.chat_id

    try:
        pid = uuid.UUID(project_id_str)
    except ValueError:
        await query.edit_message_text("❌ Invalid project reference.")
        return

    factory = get_session_factory()
    async with factory() as session:
        if await get_project(session, pid, owner_user_id) is None:
            await query.edit_message_text("❌ Project not found.")
            return

        events = await list_event_names(session, project_id=pid)

        svc = BotStateService(session)
        await svc.save(
            chat_id,
            flow="add_alert",
            step="event_name",
            payload={"project_id": project_id_str, "owner_user_id": str(owner_user_id)},
        )
        await session.commit()

    rows: list[list[InlineKeyboardButton]] = []
    for evt in events:
        event_name = evt["event_name"]
        cb = f"alert_ev:{event_name}"
        if len(cb.encode()) > 64:
            continue
        label = f"{event_name}  ({evt['count']:,})"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])

    rows.append([InlineKeyboardButton("« Back", callback_data=f"back:alerts:{project_id_str}")])

    if rows[:-1]:
        text = "📝 <b>Add Alert</b>\n\nTap the event to monitor, or type a custom event name:"
    else:
        text = (
            "📝 <b>Add Alert</b>\n\n"
            "No events received yet — type the event name you want to monitor:\n\n"
            "<i>Example: signup, purchase, pageview</i>"
        )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _pick_event_for_alert(
    query: CallbackQuery, event_name: str, owner_user_id: uuid.UUID
) -> None:
    """Handle tapping an event button during the add-alert event-picker step."""
    assert isinstance(query.message, Message)
    chat_id = query.message.chat_id

    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        state = await svc.get(chat_id)

        if state is None or state.flow != "add_alert" or state.step != "event_name":
            await query.edit_message_text("❌ No active alert creation. Use the Alerts menu.")
            return

        payload = state.payload or {}
        project_id_str = payload.get("project_id")

        if not project_id_str or (
            await get_project(session, uuid.UUID(project_id_str), owner_user_id) is None
        ):
            await svc.clear(chat_id)
            await session.commit()
            await query.edit_message_text("❌ Project not found.")
            return

        payload["event_name"] = event_name
        await svc.save(
            chat_id,
            flow="add_alert",
            step="condition",
            payload=payload,
        )
        await session.commit()

    await query.edit_message_text(
        condition_prompt(event_name), parse_mode="HTML", reply_markup=condition_keyboard()
    )


async def _handle_condition_choice(
    query: CallbackQuery, condition: str, owner_user_id: uuid.UUID
) -> None:
    """Handle condition button click during add-alert flow."""
    assert isinstance(query.message, Message)
    chat_id = query.message.chat_id

    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        state = await svc.get(chat_id)

        if state is None or state.flow != "add_alert" or state.step != "condition":
            await query.edit_message_text("❌ No active alert creation. Use the Alerts menu.")
            return

        payload = state.payload or {}
        event_name = payload.get("event_name")
        project_id_str = payload.get("project_id")

        if not event_name or not project_id_str:
            await svc.clear(chat_id)
            await session.commit()
            await query.edit_message_text("❌ Invalid state. Please start again.")
            return

        if condition == "every":
            owner_raw = payload.get("owner_user_id")
            if (
                owner_raw
                and await get_project(session, uuid.UUID(project_id_str), uuid.UUID(owner_raw))
                is None
            ):
                await svc.clear(chat_id)
                await session.commit()
                await query.edit_message_text("❌ Project not found.")
                return

            await create_alert(
                session,
                project_id=uuid.UUID(project_id_str),
                event_name=event_name,
                condition=AlertCondition.every,
            )
            await svc.clear(chat_id)
            await session.commit()

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "« Back to alerts", callback_data=f"back:alerts:{project_id_str}"
                        )
                    ]
                ]
            )
            await query.edit_message_text(
                f"✅ Alert created!\n\n"
                f"Event: <b>{html.escape(event_name)}</b>\n"
                f"Condition: notify on <b>every</b> occurrence",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            payload["condition"] = condition
            await svc.save(
                chat_id,
                flow="add_alert",
                step="threshold_n",
                payload=payload,
            )
            await session.commit()

            if condition == "every_n":
                prompt = "Enter the number N (notify every Nth event):"
            else:
                prompt = "Enter the threshold (notify when exceeded per day):"

            await query.edit_message_text(
                f"📝 <b>Add Alert</b>\n\nEvent: <b>{html.escape(event_name)}</b>\n\n{prompt}",
                parse_mode="HTML",
            )


async def _delete_alert(query: CallbackQuery, alert_id_str: str, owner_user_id: uuid.UUID) -> None:
    """Delete an alert and refresh the list."""
    factory = get_session_factory()
    async with factory() as session:
        alert = await get_alert(session, uuid.UUID(alert_id_str))
        if alert is None:
            await query.edit_message_text("❌ Alert not found.")
            return

        project = await get_project(session, alert.project_id, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Alert not found.")
            return

        project_id_str = str(alert.project_id)
        await delete_alert(session, alert.id, alert.project_id)
        await session.commit()

    await show_alerts_menu(query, project_id_str, owner_user_id)


async def _toggle_alert(query: CallbackQuery, alert_id_str: str, owner_user_id: uuid.UUID) -> None:
    """Toggle an alert's active status and refresh the list."""
    factory = get_session_factory()
    async with factory() as session:
        alert = await get_alert(session, uuid.UUID(alert_id_str))
        if alert is None:
            await query.edit_message_text("❌ Alert not found.")
            return

        project = await get_project(session, alert.project_id, owner_user_id)
        if project is None:
            await query.edit_message_text("❌ Alert not found.")
            return

        project_id_str = str(alert.project_id)
        await toggle_alert(session, alert.id, alert.project_id)
        await session.commit()

    await show_alerts_menu(query, project_id_str, owner_user_id)


@requires_user
async def handle_text_message(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    user: User,
    session: AsyncSession,
) -> None:
    """Handle text messages for multi-step conversation flows."""
    assert update.message is not None
    assert update.effective_chat is not None

    chat_id = update.effective_chat.id
    text = update.message.text or ""

    svc = BotStateService(session)
    state = await svc.get(chat_id)

    if state is None:
        return

    # Dispatch to the appropriate conversation flow handler
    if state.flow in ("set_retention", "set_allowlist"):
        from app.bot.handlers.settings import (
            handle_set_allowlist_text,
            handle_set_retention_text,
        )

        if state.flow == "set_retention":
            await handle_set_retention_text(update, session, svc, state)
        else:
            await handle_set_allowlist_text(update, session, svc, state)
        return

    if state.flow in ("add_funnel", "rename_funnel"):
        if state.step != "name":
            return
        from app.bot.handlers.funnels import handle_funnel_name_text

        await handle_funnel_name_text(update, session, svc, state)
        return

    if state.flow != "add_alert":
        return

    payload = state.payload or {}

    if state.step == "event_name":
        event_name = text.strip()
        if not event_name:
            await update.message.reply_text("❌ Event name cannot be empty. Try again:")
            return

        payload["event_name"] = event_name
        await svc.save(
            chat_id,
            flow="add_alert",
            step="condition",
            payload=payload,
        )

        await update.message.reply_text(
            condition_prompt(event_name),
            parse_mode="HTML",
            reply_markup=condition_keyboard(),
        )

    elif state.step == "threshold_n":
        try:
            threshold_n = int(text.strip())
            if threshold_n < 1:
                raise ValueError()
        except ValueError:
            await update.message.reply_text("❌ Please enter a positive integer:")
            return

        project_id_str = payload.get("project_id")
        event_name_val: str | None = payload.get("event_name")
        condition_str = payload.get("condition")

        if not all([project_id_str, event_name_val, condition_str]):
            await svc.clear(chat_id)
            await update.message.reply_text(
                "❌ Invalid state. Please start again from the Alerts menu."
            )
            return

        assert project_id_str is not None
        assert event_name_val is not None

        condition = (
            AlertCondition.every_n if condition_str == "every_n" else AlertCondition.threshold
        )

        owner_raw = payload.get("owner_user_id")
        if (
            owner_raw
            and await get_project(session, uuid.UUID(project_id_str), uuid.UUID(owner_raw)) is None
        ):
            await svc.clear(chat_id)
            await update.message.reply_text(
                "❌ Project not found. Please start again from the Alerts menu."
            )
            return

        await create_alert(
            session,
            project_id=uuid.UUID(project_id_str),
            event_name=event_name_val,
            condition=condition,
            threshold_n=threshold_n,
        )
        await svc.clear(chat_id)

        if condition == AlertCondition.every_n:
            desc = f"notify every <b>{threshold_n}</b> occurrences"
        else:
            desc = f"notify when exceeds <b>{threshold_n}</b>/day"

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "« Back to alerts", callback_data=f"back:alerts:{project_id_str}"
                    )
                ]
            ]
        )
        await update.message.reply_text(
            f"✅ Alert created!\n\nEvent: <b>{html.escape(event_name_val)}</b>\nCondition: {desc}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )


async def _show_silence_picker(query: CallbackQuery, alert_id_str: str) -> None:
    """Show duration picker for silencing an alert from a notification message."""
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1h", callback_data=f"alert_sil:{alert_id_str}:1"),
                InlineKeyboardButton("24h", callback_data=f"alert_sil:{alert_id_str}:24"),
                InlineKeyboardButton("7gg", callback_data=f"alert_sil:{alert_id_str}:168"),
            ]
        ]
    )
    await query.edit_message_reply_markup(reply_markup=keyboard)


async def _apply_silence(
    query: CallbackQuery, alert_id_str: str, hours: int, owner_user_id: uuid.UUID
) -> None:
    """Apply a silence period to an alert and confirm in the message."""
    factory = get_session_factory()
    async with factory() as session:
        alert = await get_alert(session, uuid.UUID(alert_id_str))
        if alert is None:
            await query.answer("Alert not found.", show_alert=True)
            return
        # Verify the alert belongs to a project owned by this user.
        project = await get_project(session, alert.project_id, owner_user_id)
        if project is None:
            await query.answer("Alert not found.", show_alert=True)
            return
        # mute_alert re-fetches internally; pass the same ID.
        await mute_alert(session, alert.id, hours)
        await session.commit()

    if hours == 1:
        label = "1 ora"
    elif hours == 24:
        label = "24 ore"
    else:
        label = "7 giorni"

    await query.answer(f"🔕 Silenziato per {label}.", show_alert=False)
    await query.edit_message_reply_markup(reply_markup=None)


async def _disable_alert_from_notification(
    query: CallbackQuery, alert_id_str: str, owner_user_id: uuid.UUID
) -> None:
    """Disable an alert from a notification message button."""
    factory = get_session_factory()
    async with factory() as session:
        alert = await get_alert(session, uuid.UUID(alert_id_str))
        if alert is None:
            await query.answer("Alert not found.", show_alert=True)
            return
        # Verify the alert belongs to a project owned by this user.
        project = await get_project(session, alert.project_id, owner_user_id)
        if project is None:
            await query.answer("Alert not found.", show_alert=True)
            return
        # disable_alert re-fetches internally; pass the same ID.
        await disable_alert(session, alert.id)
        await session.commit()

    await query.answer("🚫 Alert disabilitato.", show_alert=False)
    await query.edit_message_reply_markup(reply_markup=None)


async def _send_charts_from_notification(
    query: CallbackQuery, alert_id_str: str, owner_user_id: uuid.UUID, keys_token: str | None = None
) -> None:
    """Send charts for the alert's event as replies to the notification message.

    Sends a 7-day line chart plus one pie chart per property key (30-day
    window, top 10 values), bundled into Telegram media groups.

    ``keys_token`` resolves (via the event-meta cache) to the property keys
    carried by the triggering event; their pie charts are sent first, ahead
    of the line chart and pies for the event's other historical properties.
    """
    priority_keys: list[str] = []
    if keys_token:
        from app.bot.event_meta_cache import get as _get_keys

        stored = _get_keys(keys_token)
        if stored:
            priority_keys = [k for k in stored.split(",") if k]
    assert isinstance(query.message, Message)
    factory = get_session_factory()
    async with factory() as session:
        alert = await get_alert(session, uuid.UUID(alert_id_str))
        if alert is None:
            await query.answer("Alert not found.", show_alert=True)
            return
        # Verify the alert belongs to a project owned by this user.
        project = await get_project(session, alert.project_id, owner_user_id)
        if project is None:
            await query.answer("Alert not found.", show_alert=True)
            return

        event_name = alert.event_name
        now = datetime.now(UTC)

        line_data = await events_over_time(
            session,
            project_id=project.id,
            event_name=event_name,
            start=now - timedelta(days=7),
            end=now,
            granularity="day",
        )

        # Batch all DB reads inside this session, then close it before
        # the (slow, CPU-bound) chart fan-out.
        pie_start = now - timedelta(days=30)
        keys = await list_property_keys(
            session,
            project_id=project.id,
            event_name=event_name,
            start=pie_start,
            end=now,
        )
        # The triggering event's own properties come first, in notification
        # order; the event's other historical properties follow.
        if priority_keys:
            key_set = set(keys)
            front = [k for k in priority_keys if k in key_set]
            keys = front + [k for k in keys if k not in set(front)]

        key_rows: list[tuple[str, list[dict[str, Any]]]] = []
        for key in keys:
            rows = await top_properties(
                session,
                project_id=project.id,
                event_name=event_name,
                property_key=key,
                start=pie_start,
                end=now,
                limit=10,
            )
            if rows:
                key_rows.append((key, rows))

    priority_set = set(priority_keys)
    priority_pies: list[tuple[bytes, str]] = []
    other_pies: list[tuple[bytes, str]] = []
    for key, rows in key_rows:
        pie_data = [{"source": r["value"], "count": r["count"]} for r in rows]
        try:
            png = await generate_pie_chart(pie_data, title=f"{event_name} · {key}")
        except ChartGenerationError:
            continue
        total = sum(int(r["count"]) for r in rows)
        caption = f"🥧 {project.name} · {event_name} · {key}\n📈 {total:,} events"
        (priority_pies if key in priority_set else other_pies).append((png, caption))

    line_items: list[tuple[bytes, str]] = []
    if line_data:
        try:
            png = await generate_line_chart(
                line_data,
                title=event_name,
                period_label="last 7 days",
            )
            line_items.append((png, f"📈 {project.name} · {event_name} · last 7 days"))
        except ChartGenerationError:
            pass

    items = priority_pies + line_items + other_pies

    if not items:
        await query.answer(f"No chart data yet for {event_name}.", show_alert=True)
        return

    # Telegram requires a media group to have 2-10 items. Chunk by 10, and
    # fall back to reply_photo when a chunk has only 1 item.
    for chunk_start in range(0, len(items), 10):
        chunk = items[chunk_start : chunk_start + 10]
        if len(chunk) == 1:
            png, caption = chunk[0]
            await query.message.reply_photo(photo=png, caption=caption)
        else:
            await query.message.reply_media_group(
                media=[InputMediaPhoto(media=p, caption=c) for p, c in chunk]
            )
