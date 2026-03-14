"""Alert management handlers: alerts menu, add/delete/toggle alerts."""

from __future__ import annotations

import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.bot.states import BotStateService
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.models.alert import AlertCondition
from app.services.alerts import create_alert, delete_alert, list_alerts, toggle_alert
from app.services.projects import get_project


def _format_alert_label(alert) -> str:
    """Format an alert for display in the list."""
    status = "✅" if alert.is_active else "⏸️"
    if alert.condition == AlertCondition.every:
        return f"{status} {alert.event_name} (every)"
    elif alert.condition == AlertCondition.every_n:
        return f"{status} {alert.event_name} (every {alert.threshold_n})"
    else:  # threshold
        return f"{status} {alert.event_name} (>{alert.threshold_n}/day)"


async def show_alerts_menu(query, project_id_str: str, admin_chat_id: int) -> None:
    """Display the alerts list for a project with action buttons."""
    factory = get_session_factory()
    async with factory() as session:
        project = await get_project(session, uuid.UUID(project_id_str), admin_chat_id)
        if project is None:
            await query.edit_message_text("❌ Project not found.")
            return

        alerts = await list_alerts(session, project.id)

    rows: list[list[InlineKeyboardButton]] = []

    for alert in alerts:
        label = _format_alert_label(alert)
        toggle_icon = "⏸️" if alert.is_active else "▶️"
        rows.append([
            InlineKeyboardButton(label, callback_data=f"alert_noop:{alert.id}"),
        ])
        rows.append([
            InlineKeyboardButton(
                toggle_icon, callback_data=f"alert_toggle:{alert.id}:{project_id_str}"
            ),
            InlineKeyboardButton(
                "🗑", callback_data=f"alert_del:{alert.id}:{project_id_str}"
            ),
        ])

    rows.append([
        InlineKeyboardButton("➕ Add alert", callback_data=f"alert_add:{project_id_str}")
    ])
    rows.append([
        InlineKeyboardButton("« Back", callback_data=f"proj:{project_id_str}")
    ])

    keyboard = InlineKeyboardMarkup(rows)
    await query.edit_message_text(
        f"🔔 <b>Alerts for {project.name}</b>\n─────────────────",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def alert_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all alert-related callbacks."""
    query = update.callback_query
    assert query is not None
    await query.answer()

    settings = get_settings()
    admin_chat_id = settings.admin_chat_id

    if update.effective_user is None or update.effective_user.id != admin_chat_id:
        return

    data: str = query.data or ""

    if data.startswith("alert_add:"):
        project_id_str = data[10:]
        await _start_add_alert(query, project_id_str)

    elif data.startswith("alert_cond:"):
        condition = data[11:]
        await _handle_condition_choice(query, condition, admin_chat_id)

    elif data.startswith("alert_del:"):
        parts = data[10:].split(":", 1)
        if len(parts) == 2:
            alert_id_str, project_id_str = parts
            await _delete_alert(query, alert_id_str, project_id_str, admin_chat_id)

    elif data.startswith("alert_toggle:"):
        parts = data[13:].split(":", 1)
        if len(parts) == 2:
            alert_id_str, project_id_str = parts
            await _toggle_alert(query, alert_id_str, project_id_str, admin_chat_id)

    elif data.startswith("alert_noop:"):
        pass

    elif data.startswith("back:alerts:"):
        project_id_str = data[12:]
        await show_alerts_menu(query, project_id_str, admin_chat_id)


async def _start_add_alert(query, project_id_str: str) -> None:
    """Start the add-alert conversation flow."""
    chat_id = query.message.chat_id

    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        await svc.save(
            chat_id,
            flow="add_alert",
            step="event_name",
            payload={"project_id": project_id_str},
        )
        await session.commit()

    await query.edit_message_text(
        "📝 <b>Add Alert</b>\n\n"
        "Type the event name you want to monitor:\n\n"
        "<i>Example: signup, purchase, page_view</i>",
        parse_mode="HTML",
    )


async def _handle_condition_choice(query, condition: str, admin_chat_id: int) -> None:
    """Handle condition button click during add-alert flow."""
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
            alert = await create_alert(
                session,
                project_id=uuid.UUID(project_id_str),
                event_name=event_name,
                condition=AlertCondition.every,
            )
            await svc.clear(chat_id)
            await session.commit()

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back to alerts", callback_data=f"back:alerts:{project_id_str}")]
            ])
            await query.edit_message_text(
                f"✅ Alert created!\n\n"
                f"Event: <b>{event_name}</b>\n"
                f"Condition: notify on <b>every</b> occurrence",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            cond_enum = AlertCondition.every_n if condition == "every_n" else AlertCondition.threshold
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
                f"📝 <b>Add Alert</b>\n\n"
                f"Event: <b>{event_name}</b>\n\n"
                f"{prompt}",
                parse_mode="HTML",
            )


async def _delete_alert(query, alert_id_str: str, project_id_str: str, admin_chat_id: int) -> None:
    """Delete an alert and refresh the list."""
    factory = get_session_factory()
    async with factory() as session:
        deleted = await delete_alert(
            session,
            uuid.UUID(alert_id_str),
            uuid.UUID(project_id_str),
        )
        await session.commit()

    if not deleted:
        await query.edit_message_text("❌ Alert not found.")
        return

    await show_alerts_menu(query, project_id_str, admin_chat_id)


async def _toggle_alert(query, alert_id_str: str, project_id_str: str, admin_chat_id: int) -> None:
    """Toggle an alert's active status and refresh the list."""
    factory = get_session_factory()
    async with factory() as session:
        alert = await toggle_alert(
            session,
            uuid.UUID(alert_id_str),
            uuid.UUID(project_id_str),
        )
        await session.commit()

    if alert is None:
        await query.edit_message_text("❌ Alert not found.")
        return

    await show_alerts_menu(query, project_id_str, admin_chat_id)


async def handle_text_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages for multi-step conversation flows."""
    assert update.message is not None
    assert update.effective_chat is not None

    settings = get_settings()
    admin_chat_id = settings.admin_chat_id

    if update.effective_user is None or update.effective_user.id != admin_chat_id:
        return

    chat_id = update.effective_chat.id
    text = update.message.text or ""

    factory = get_session_factory()
    async with factory() as session:
        svc = BotStateService(session)
        state = await svc.get(chat_id)

        if state is None or state.flow != "add_alert":
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
            await session.commit()

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Every", callback_data="alert_cond:every"),
                    InlineKeyboardButton("Every N", callback_data="alert_cond:every_n"),
                    InlineKeyboardButton("Threshold", callback_data="alert_cond:threshold"),
                ]
            ])
            await update.message.reply_text(
                f"📝 <b>Add Alert</b>\n\n"
                f"Event: <b>{event_name}</b>\n\n"
                f"Choose when to notify:\n"
                f"• <b>Every</b> — on every occurrence\n"
                f"• <b>Every N</b> — every Nth occurrence\n"
                f"• <b>Threshold</b> — when count exceeds N per day",
                parse_mode="HTML",
                reply_markup=keyboard,
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
            event_name = payload.get("event_name")
            condition_str = payload.get("condition")

            if not all([project_id_str, event_name, condition_str]):
                await svc.clear(chat_id)
                await session.commit()
                await update.message.reply_text("❌ Invalid state. Please start again from the Alerts menu.")
                return

            condition = AlertCondition.every_n if condition_str == "every_n" else AlertCondition.threshold

            alert = await create_alert(
                session,
                project_id=uuid.UUID(project_id_str),
                event_name=event_name,
                condition=condition,
                threshold_n=threshold_n,
            )
            await svc.clear(chat_id)
            await session.commit()

            if condition == AlertCondition.every_n:
                desc = f"notify every <b>{threshold_n}</b> occurrences"
            else:
                desc = f"notify when exceeds <b>{threshold_n}</b>/day"

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back to alerts", callback_data=f"back:alerts:{project_id_str}")]
            ])
            await update.message.reply_text(
                f"✅ Alert created!\n\n"
                f"Event: <b>{event_name}</b>\n"
                f"Condition: {desc}",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
