"""Alert feature tests — CRUD service, bot handlers, conversation flow, notifications.

Tests follow the same patterns as test_phase6.py: fake Update / Message /
CallbackQuery objects with MagicMock/AsyncMock, handlers called directly.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Message

from app.models.alert import AlertCondition

# ── helpers ───────────────────────────────────────────────────────────────────

ADMIN_ID = 111


def _make_update(chat_id: int = ADMIN_ID, text: str = "/start", args: list[str] | None = None):
    """Build a minimal fake message Update."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = chat_id
    update.message.reply_text = AsyncMock()
    update.message.text = text
    update.message.chat_id = chat_id
    update.callback_query = None
    ctx = MagicMock()
    ctx.args = args or []
    return update, ctx


def _make_callback(chat_id: int = ADMIN_ID, data: str = "alert_add:some-uuid"):
    """Build a minimal fake CallbackQuery Update."""
    update = MagicMock()
    update.effective_user.id = chat_id
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.message = MagicMock(spec=Message)
    update.callback_query.message.chat_id = chat_id
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    ctx = MagicMock()
    return update, ctx


# ── Alert CRUD service tests ───────────────────────────────────────────────────


async def test_create_alert_with_every_condition(db_session, session_factory, singleton_user):
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="test-alerts.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()

        alert = await create_alert(
            session,
            project_id=project.id,
            event_name="signup",
            condition=AlertCondition.every,
        )
        await session.commit()

        assert alert.event_name == "signup"
        assert alert.condition == AlertCondition.every
        assert alert.threshold_n is None
        assert alert.is_active is True


async def test_create_alert_with_every_n_condition(db_session, session_factory, singleton_user):
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="test-every-n.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()

        alert = await create_alert(
            session,
            project_id=project.id,
            event_name="purchase",
            condition=AlertCondition.every_n,
            threshold_n=50,
        )
        await session.commit()

        assert alert.event_name == "purchase"
        assert alert.condition == AlertCondition.every_n
        assert alert.threshold_n == 50


async def test_create_alert_with_threshold_condition(db_session, session_factory, singleton_user):
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="test-threshold.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()

        alert = await create_alert(
            session,
            project_id=project.id,
            event_name="error",
            condition=AlertCondition.threshold,
            threshold_n=100,
        )
        await session.commit()

        assert alert.event_name == "error"
        assert alert.condition == AlertCondition.threshold
        assert alert.threshold_n == 100


async def test_list_alerts_returns_project_alerts(db_session, session_factory, singleton_user):
    from app.services.alerts import create_alert, list_alerts
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="list-test.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()

        await create_alert(
            session, project_id=project.id, event_name="event1", condition=AlertCondition.every
        )
        await create_alert(
            session, project_id=project.id, event_name="event2", condition=AlertCondition.every
        )
        await session.commit()

        alerts = await list_alerts(session, project.id)
        assert len(alerts) == 2
        event_names = {a.event_name for a in alerts}
        assert event_names == {"event1", "event2"}


async def test_delete_alert(db_session, session_factory, singleton_user):
    from app.services.alerts import create_alert, delete_alert, list_alerts
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="delete-test.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()

        alert = await create_alert(
            session, project_id=project.id, event_name="to_delete", condition=AlertCondition.every
        )
        await session.commit()

        deleted = await delete_alert(session, alert.id, project.id)
        await session.commit()

        assert deleted is True
        alerts = await list_alerts(session, project.id)
        assert len(alerts) == 0


async def test_delete_alert_not_found(db_session, session_factory, singleton_user):
    from app.services.alerts import delete_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="delete-nf.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()

        deleted = await delete_alert(session, uuid.uuid4(), project.id)
        assert deleted is False


async def test_toggle_alert(db_session, session_factory, singleton_user):
    from app.services.alerts import create_alert, toggle_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="toggle-test.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()

        alert = await create_alert(
            session, project_id=project.id, event_name="toggle_me", condition=AlertCondition.every
        )
        await session.commit()
        assert alert.is_active is True

        toggled = await toggle_alert(session, alert.id, project.id)
        await session.commit()
        assert toggled is not None
        assert toggled.is_active is False

        toggled_back = await toggle_alert(session, alert.id, project.id)
        await session.commit()
        assert toggled_back.is_active is True


# ── Bot callback handlers ──────────────────────────────────────────────────────


async def test_alerts_menu_shows_alerts_list(session_factory, singleton_user):
    from app.bot.handlers.alerts import show_alerts_menu
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="alerts-menu.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await create_alert(
            session, project_id=project.id, event_name="signup", condition=AlertCondition.every
        )
        await session.commit()
        pid = str(project.id)

    query = MagicMock()
    query.edit_message_text = AsyncMock()

    # show_alerts_menu now takes ``owner_user_id: uuid.UUID`` (Phase 3.3)
    # and reads ``get_session_factory`` from the module-level cache wired
    # by the ``singleton_user`` fixture, so no patching is required.
    await show_alerts_menu(query, pid, singleton_user.id)

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "Alerts" in text
    keyboard = query.edit_message_text.call_args[1].get("reply_markup")
    assert keyboard is not None
    flat_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("signup" in label for label in flat_labels)
    assert any("Add alert" in label for label in flat_labels)


async def test_alert_add_starts_conversation(session_factory, singleton_user):
    from app.bot.handlers.alerts import alert_callback
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="add-conv.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"alert_add:{pid}")
    await alert_callback(update, ctx)

    update.callback_query.edit_message_text.assert_called_once()
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "event name" in text.lower()


async def test_start_add_alert_rejects_foreign_project(session_factory, singleton_user):
    """A non-owner cannot begin creating an alert on another tenant's project."""
    from sqlalchemy import text
    from telegram import Message

    from app.bot.handlers.alerts import _start_add_alert
    from app.bot.states import BotStateService
    from app.models.user import User
    from app.services.projects import create_project

    async with session_factory() as session:
        victim = User(telegram_user_id=999_444)
        session.add(victim)
        await session.flush()
        project, _ = await create_project(
            session, name="victim3.com", admin_chat_id=999_444, owner_user_id=victim.id
        )
        await session.commit()
        victim_pid = str(project.id)
        victim_id = victim.id

    # Use a chat id no other test touches so the "no state saved" assertion
    # isolates THIS handler's behaviour (ADMIN_ID=111 is polluted by other
    # tests that seed add-alert state and never clear it).
    foreign_chat_id = 222

    query = MagicMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = foreign_chat_id
    query.edit_message_text = AsyncMock()

    await _start_add_alert(query, victim_pid, singleton_user.id)

    query.edit_message_text.assert_called_once()
    assert "not found" in query.edit_message_text.call_args[0][0].lower()
    async with session_factory() as session:
        assert await BotStateService(session).get(foreign_chat_id) is None
        await session.execute(
            text("DELETE FROM projects WHERE owner_user_id = :o"), {"o": str(victim_id)}
        )
        await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": str(victim_id)})
        await session.commit()


async def test_alert_delete_removes_alert(session_factory, singleton_user):
    from app.bot.handlers.alerts import alert_callback
    from app.services.alerts import create_alert, list_alerts
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="del-alert.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        alert = await create_alert(
            session, project_id=project.id, event_name="to_del", condition=AlertCondition.every
        )
        await session.commit()
        pid = str(project.id)
        aid = str(alert.id)

    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"alert_d:{aid}")
    await alert_callback(update, ctx)

    async with session_factory() as session:
        alerts = await list_alerts(session, uuid.UUID(pid))
        assert len(alerts) == 0


async def test_alert_toggle_changes_active_status(session_factory, singleton_user):
    from app.bot.handlers.alerts import alert_callback
    from app.services.alerts import create_alert, get_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="toggle-alert.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        alert = await create_alert(
            session, project_id=project.id, event_name="toggle_ev", condition=AlertCondition.every
        )
        await session.commit()
        pid = str(project.id)
        aid = str(alert.id)
        assert alert.is_active is True

    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"alert_t:{aid}")
    await alert_callback(update, ctx)

    async with session_factory() as session:
        alert_after = await get_alert(session, uuid.UUID(aid), uuid.UUID(pid))
        assert alert_after.is_active is False


async def test_non_admin_alert_callback_ignored(singleton_user):
    """Unknown callers must NOT have their alert callback dispatched.

    Phase 3.3: authorization is owned by ``@requires_user``. When the
    decorator's resolver returns ``None`` (singleton cache unset — the
    caller could not be authorized), the callback short-circuits before
    any ``edit_message_text`` call is made. ``alert_callback``
    previously consulted ``get_settings().admin_chat_id`` directly; that
    branch is gone so we no longer patch it.
    """
    from app.bot import auth as auth_mod
    from app.bot.handlers.alerts import alert_callback

    update, ctx = _make_callback(chat_id=999_888, data="alert_add:some-uuid")

    saved = auth_mod._singleton_user_id
    auth_mod._singleton_user_id = None
    try:
        await alert_callback(update, ctx)
    finally:
        auth_mod._singleton_user_id = saved

    update.callback_query.edit_message_text.assert_not_called()


# ── Text message handler (conversation flow) ───────────────────────────────────


async def test_text_handler_captures_event_name(session_factory, singleton_user):
    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="text-ev.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(ADMIN_ID, flow="add_alert", step="event_name", payload={"project_id": pid})
        await session.commit()

    update, ctx = _make_update(chat_id=ADMIN_ID, text="signup")
    await handle_text_message(update, ctx)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "signup" in text
    keyboard = update.message.reply_text.call_args[1].get("reply_markup")
    assert keyboard is not None
    flat_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert "Every" in flat_labels
    assert "Every N" in flat_labels
    assert "Threshold" in flat_labels


async def test_text_handler_captures_threshold_and_creates_alert(session_factory, singleton_user):
    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.services.alerts import list_alerts
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="text-thr.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(
            ADMIN_ID,
            flow="add_alert",
            step="threshold_n",
            payload={"project_id": pid, "event_name": "purchase", "condition": "every_n"},
        )
        await session.commit()

    update, ctx = _make_update(chat_id=ADMIN_ID, text="50")
    await handle_text_message(update, ctx)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Alert created" in text
    assert "purchase" in text
    assert "50" in text

    async with session_factory() as session:
        alerts = await list_alerts(session, uuid.UUID(pid))
        assert len(alerts) == 1
        assert alerts[0].event_name == "purchase"
        assert alerts[0].condition == AlertCondition.every_n
        assert alerts[0].threshold_n == 50


async def test_text_handler_rejects_invalid_threshold(session_factory, singleton_user):
    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="text-inv.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(
            ADMIN_ID,
            flow="add_alert",
            step="threshold_n",
            payload={"project_id": pid, "event_name": "error", "condition": "threshold"},
        )
        await session.commit()

    update, ctx = _make_update(chat_id=ADMIN_ID, text="not-a-number")
    await handle_text_message(update, ctx)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "positive integer" in text.lower()


async def test_condition_every_creates_alert_immediately(session_factory, singleton_user):
    from app.bot.handlers.alerts import alert_callback
    from app.bot.states import BotStateService
    from app.services.alerts import list_alerts
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="cond-every.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(
            ADMIN_ID,
            flow="add_alert",
            step="condition",
            payload={"project_id": pid, "event_name": "click"},
        )
        await session.commit()

    update, ctx = _make_callback(chat_id=ADMIN_ID, data="alert_cond:every")
    await alert_callback(update, ctx)

    update.callback_query.edit_message_text.assert_called_once()
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "Alert created" in text
    assert "every" in text.lower()

    async with session_factory() as session:
        alerts = await list_alerts(session, uuid.UUID(pid))
        assert len(alerts) == 1
        assert alerts[0].condition == AlertCondition.every


# ── Alert notification tests ───────────────────────────────────────────────────


async def test_alert_notification_sent_on_fire(db_session, session_factory, singleton_user):
    """When an alert fires, a Telegram message should be sent."""
    from app.api.ingestion import _run_alert_evaluation
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="notify-test.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await create_alert(
            session,
            project_id=project.id,
            event_name="notify_event",
            condition=AlertCondition.every,
        )
        await session.commit()
        pid = project.id

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    with (
        patch("app.api.ingestion.get_session_factory", return_value=session_factory),
        patch("app.bot.setup.get_bot", return_value=mock_bot),
    ):
        await _run_alert_evaluation(pid, "notify_event")

    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args[1]
    assert call_kwargs["chat_id"] == ADMIN_ID
    assert "notify_event" in call_kwargs["text"]
    assert "notify-test.com" in call_kwargs["text"]


async def test_alert_notification_message_varies_by_condition(
    db_session, session_factory, singleton_user
):
    """Different conditions produce different notification messages."""
    from app.api.ingestion import _run_alert_evaluation
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="msg-vary.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await create_alert(
            session,
            project_id=project.id,
            event_name="ev_every_n",
            condition=AlertCondition.every_n,
            threshold_n=10,
        )
        await session.commit()
        pid = project.id

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    with (
        patch("app.api.ingestion.get_session_factory", return_value=session_factory),
        patch("app.bot.setup.get_bot", return_value=mock_bot),
    ):
        for _ in range(10):
            await _run_alert_evaluation(pid, "ev_every_n")

    assert mock_bot.send_message.call_count == 1
    text = mock_bot.send_message.call_args[1]["text"]
    assert "10" in text
    assert "times" in text


async def test_alert_notification_includes_more_charts_button(
    db_session, session_factory, singleton_user
):
    """The notification keyboard exposes a '📊 More charts' button → alert_pie:{id}."""
    from app.api.ingestion import _run_alert_evaluation
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="charts-btn.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        alert = await create_alert(
            session,
            project_id=project.id,
            event_name="abandon_reason",
            condition=AlertCondition.every,
        )
        await session.commit()
        pid = project.id
        aid = str(alert.id)

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    with (
        patch("app.api.ingestion.get_session_factory", return_value=session_factory),
        patch("app.bot.setup.get_bot", return_value=mock_bot),
    ):
        await _run_alert_evaluation(pid, "abandon_reason", {"reason": "price"})

    mock_bot.send_message.assert_called_once()
    keyboard = mock_bot.send_message.call_args[1]["reply_markup"]
    buttons = {btn.text: btn.callback_data for row in keyboard.inline_keyboard for btn in row}
    # The event carries a custom property, so the callback also embeds a
    # token resolving to the property keys to prioritize.
    charts_cb = buttons.get("📊 More charts")
    assert charts_cb is not None and charts_cb.startswith(f"alert_pie:{aid}:")
    # Existing buttons are still present.
    assert f"alert_sil:{aid}" in buttons.values()
    assert f"alert_dis:{aid}" in buttons.values()


async def test_alert_pie_callback_sends_line_and_pie_charts(session_factory, singleton_user):
    """alert_pie:{id} replies with a media group: 7d line chart + one pie per property."""
    from datetime import UTC, datetime, timedelta

    from app.bot.handlers.alerts import alert_callback
    from app.services.alerts import create_alert
    from app.services.events import insert_event
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="pie-notify.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        alert = await create_alert(
            session,
            project_id=project.id,
            event_name="abandon_reason",
            condition=AlertCondition.every,
        )
        seed = [{"reason": "price"}, {"reason": "price"}, {"reason": "ux"}]
        for i, props in enumerate(seed):
            await insert_event(
                session,
                project_id=project.id,
                event_name="abandon_reason",
                session_id=f"s{i}",
                properties=props,
                timestamp=datetime.now(UTC) - timedelta(hours=i + 1),
            )
        await session.commit()
        aid = str(alert.id)

    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"alert_pie:{aid}")
    update.callback_query.message.reply_photo = AsyncMock()
    update.callback_query.message.reply_media_group = AsyncMock()

    with (
        patch(
            "app.bot.handlers.alerts.generate_line_chart",
            new=AsyncMock(return_value=b"LINE_PNG"),
        ),
        patch(
            "app.bot.handlers.alerts.generate_pie_chart",
            new=AsyncMock(return_value=b"PIE_PNG"),
        ),
    ):
        await alert_callback(update, ctx)

    # Line chart + one pie (single property) → one two-item media group.
    msg = update.callback_query.message
    assert msg.reply_media_group.call_count == 1
    assert msg.reply_photo.call_count == 0
    media = msg.reply_media_group.call_args[1].get("media")
    assert media is not None and len(media) == 2
    captions = " | ".join(m.caption or "" for m in media)
    assert "last 7 days" in captions
    assert "reason" in captions
    assert "3 events" in captions
    # The notification message itself is left untouched.
    update.callback_query.edit_message_text.assert_not_called()


async def test_alert_pie_prioritizes_triggering_event_properties(session_factory, singleton_user):
    """The pie chart for the notification's own property leads the album.

    Full flow: the notification embeds a token with the triggering event's
    property keys ("reason"); tapping the button must send the reason pie
    first, then the line chart, then pies for other historical properties
    ("plan"). Meta keys ($timezone) never get a priority pie.
    """
    from datetime import UTC, datetime, timedelta

    from app.api.ingestion import _run_alert_evaluation
    from app.bot.handlers.alerts import alert_callback
    from app.services.alerts import create_alert
    from app.services.events import insert_event
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="pie-priority.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await create_alert(
            session,
            project_id=project.id,
            event_name="abandon_reason",
            condition=AlertCondition.every,
        )
        # "plan" sorts before "reason" alphabetically, so a leading reason
        # pie proves the priority ordering (not list_property_keys order).
        seed = [
            {"reason": "price", "plan": "pro"},
            {"reason": "ux", "plan": "free"},
        ]
        for i, props in enumerate(seed):
            await insert_event(
                session,
                project_id=project.id,
                event_name="abandon_reason",
                session_id=f"s{i}",
                properties=props,
                timestamp=datetime.now(UTC) - timedelta(hours=i + 1),
            )
        await session.commit()
        pid = project.id

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    with (
        patch("app.api.ingestion.get_session_factory", return_value=session_factory),
        patch("app.bot.setup.get_bot", return_value=mock_bot),
    ):
        await _run_alert_evaluation(
            pid, "abandon_reason", {"$timezone": "Europe/Chisinau", "reason": "price"}
        )

    keyboard = mock_bot.send_message.call_args[1]["reply_markup"]
    charts_cb = next(
        btn.callback_data
        for row in keyboard.inline_keyboard
        for btn in row
        if btn.text == "📊 More charts"
    )

    update, ctx = _make_callback(chat_id=ADMIN_ID, data=charts_cb)
    update.callback_query.message.reply_photo = AsyncMock()
    update.callback_query.message.reply_media_group = AsyncMock()

    with (
        patch(
            "app.bot.handlers.alerts.generate_line_chart",
            new=AsyncMock(return_value=b"LINE_PNG"),
        ),
        patch(
            "app.bot.handlers.alerts.generate_pie_chart",
            new=AsyncMock(return_value=b"PIE_PNG"),
        ),
    ):
        await alert_callback(update, ctx)

    msg = update.callback_query.message
    assert msg.reply_media_group.call_count == 1
    media = msg.reply_media_group.call_args[1].get("media")
    captions = [m.caption or "" for m in media]
    assert len(captions) == 3, f"expected reason pie + line + plan pie; got {captions}"
    assert "reason" in captions[0], f"reason pie must lead the album: {captions}"
    assert "last 7 days" in captions[1]
    assert "plan" in captions[2]


async def test_alert_pie_callback_no_data_shows_popup(session_factory, singleton_user):
    """alert_pie with no event data answers with a popup instead of sending charts."""
    from app.bot.handlers.alerts import alert_callback
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="pie-empty.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        alert = await create_alert(
            session,
            project_id=project.id,
            event_name="ghost_event",
            condition=AlertCondition.every,
        )
        await session.commit()
        aid = str(alert.id)

    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"alert_pie:{aid}")
    update.callback_query.message.reply_photo = AsyncMock()
    update.callback_query.message.reply_media_group = AsyncMock()

    await alert_callback(update, ctx)

    msg = update.callback_query.message
    msg.reply_photo.assert_not_called()
    msg.reply_media_group.assert_not_called()
    popup_calls = [
        c for c in update.callback_query.answer.call_args_list if c.kwargs.get("show_alert")
    ]
    assert popup_calls, "expected a show_alert popup when no chart data exists"
    assert "ghost_event" in (popup_calls[0].args[0] if popup_calls[0].args else "")


async def test_no_notification_when_no_alerts_fire(db_session, session_factory, singleton_user):
    """No notification sent if no alerts match or fire."""
    from app.api.ingestion import _run_alert_evaluation
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="no-fire.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = project.id

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    with (
        patch("app.api.ingestion.get_session_factory", return_value=session_factory),
        patch("app.bot.setup.get_bot", return_value=mock_bot),
    ):
        await _run_alert_evaluation(pid, "some_event")

    mock_bot.send_message.assert_not_called()
