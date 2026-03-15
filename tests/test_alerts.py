"""Alert feature tests — CRUD service, bot handlers, conversation flow, notifications.

Tests follow the same patterns as test_phase6.py: fake Update / Message /
CallbackQuery objects with MagicMock/AsyncMock, handlers called directly.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

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
    update.callback_query.message.chat_id = chat_id
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    ctx = MagicMock()
    return update, ctx


# ── Alert CRUD service tests ───────────────────────────────────────────────────


async def test_create_alert_with_every_condition(db_session, session_factory):
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="test-alerts.com", admin_chat_id=ADMIN_ID)
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


async def test_create_alert_with_every_n_condition(db_session, session_factory):
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="test-every-n.com", admin_chat_id=ADMIN_ID)
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


async def test_create_alert_with_threshold_condition(db_session, session_factory):
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session, name="test-threshold.com", admin_chat_id=ADMIN_ID
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


async def test_list_alerts_returns_project_alerts(db_session, session_factory):
    from app.services.alerts import create_alert, list_alerts
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="list-test.com", admin_chat_id=ADMIN_ID)
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


async def test_delete_alert(db_session, session_factory):
    from app.services.alerts import create_alert, delete_alert, list_alerts
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="delete-test.com", admin_chat_id=ADMIN_ID)
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


async def test_delete_alert_not_found(db_session, session_factory):
    from app.services.alerts import delete_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="delete-nf.com", admin_chat_id=ADMIN_ID)
        await session.commit()

        deleted = await delete_alert(session, uuid.uuid4(), project.id)
        assert deleted is False


async def test_toggle_alert(db_session, session_factory):
    from app.services.alerts import create_alert, toggle_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="toggle-test.com", admin_chat_id=ADMIN_ID)
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


async def test_alerts_menu_shows_alerts_list(db_session, session_factory):
    from app.bot.handlers.alerts import show_alerts_menu
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="alerts-menu.com", admin_chat_id=ADMIN_ID)
        await create_alert(
            session, project_id=project.id, event_name="signup", condition=AlertCondition.every
        )
        await session.commit()
        pid = str(project.id)

    query = MagicMock()
    query.edit_message_text = AsyncMock()

    with patch("app.bot.handlers.alerts.get_session_factory", return_value=session_factory):
        await show_alerts_menu(query, pid, ADMIN_ID)

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "Alerts" in text
    keyboard = query.edit_message_text.call_args[1].get("reply_markup")
    assert keyboard is not None
    flat_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("signup" in label for label in flat_labels)
    assert any("Add alert" in label for label in flat_labels)


async def test_alert_add_starts_conversation(db_session, session_factory):
    from app.bot.handlers.alerts import alert_callback
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="add-conv.com", admin_chat_id=ADMIN_ID)
        await session.commit()
        pid = str(project.id)

    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"alert_add:{pid}")

    with (
        patch("app.bot.handlers.alerts.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.alerts.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
        await alert_callback(update, ctx)

    update.callback_query.edit_message_text.assert_called_once()
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "event name" in text.lower()


async def test_alert_delete_removes_alert(db_session, session_factory):
    from app.bot.handlers.alerts import alert_callback
    from app.services.alerts import create_alert, list_alerts
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="del-alert.com", admin_chat_id=ADMIN_ID)
        alert = await create_alert(
            session, project_id=project.id, event_name="to_del", condition=AlertCondition.every
        )
        await session.commit()
        pid = str(project.id)
        aid = str(alert.id)

    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"alert_d:{aid}")

    with (
        patch("app.bot.handlers.alerts.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.alerts.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
        await alert_callback(update, ctx)

    async with session_factory() as session:
        alerts = await list_alerts(session, uuid.UUID(pid))
        assert len(alerts) == 0


async def test_alert_toggle_changes_active_status(db_session, session_factory):
    from app.bot.handlers.alerts import alert_callback
    from app.services.alerts import create_alert, get_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="toggle-alert.com", admin_chat_id=ADMIN_ID)
        alert = await create_alert(
            session, project_id=project.id, event_name="toggle_ev", condition=AlertCondition.every
        )
        await session.commit()
        pid = str(project.id)
        aid = str(alert.id)
        assert alert.is_active is True

    update, ctx = _make_callback(chat_id=ADMIN_ID, data=f"alert_t:{aid}")

    with (
        patch("app.bot.handlers.alerts.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.alerts.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
        await alert_callback(update, ctx)

    async with session_factory() as session:
        alert_after = await get_alert(session, uuid.UUID(aid), uuid.UUID(pid))
        assert alert_after.is_active is False


async def test_non_admin_alert_callback_ignored():
    from app.bot.handlers.alerts import alert_callback

    update, ctx = _make_callback(chat_id=999_888, data="alert_add:some-uuid")

    with patch("app.bot.handlers.alerts.get_settings") as mock_settings:
        mock_settings.return_value.admin_chat_id = ADMIN_ID
        await alert_callback(update, ctx)

    update.callback_query.answer.assert_called_once()
    update.callback_query.edit_message_text.assert_not_called()


# ── Text message handler (conversation flow) ───────────────────────────────────


async def test_text_handler_captures_event_name(db_session, session_factory):
    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="text-ev.com", admin_chat_id=ADMIN_ID)
        await session.commit()
        pid = str(project.id)

        svc = BotStateService(session)
        await svc.save(ADMIN_ID, flow="add_alert", step="event_name", payload={"project_id": pid})
        await session.commit()

    update, ctx = _make_update(chat_id=ADMIN_ID, text="signup")

    with (
        patch("app.bot.handlers.alerts.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.alerts.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
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


async def test_text_handler_captures_threshold_and_creates_alert(db_session, session_factory):
    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.services.alerts import list_alerts
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="text-thr.com", admin_chat_id=ADMIN_ID)
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

    with (
        patch("app.bot.handlers.alerts.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.alerts.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
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


async def test_text_handler_rejects_invalid_threshold(db_session, session_factory):
    from app.bot.handlers.alerts import handle_text_message
    from app.bot.states import BotStateService
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="text-inv.com", admin_chat_id=ADMIN_ID)
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

    with (
        patch("app.bot.handlers.alerts.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.alerts.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
        await handle_text_message(update, ctx)

    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "positive integer" in text.lower()


async def test_condition_every_creates_alert_immediately(db_session, session_factory):
    from app.bot.handlers.alerts import alert_callback
    from app.bot.states import BotStateService
    from app.services.alerts import list_alerts
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="cond-every.com", admin_chat_id=ADMIN_ID)
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

    with (
        patch("app.bot.handlers.alerts.get_session_factory", return_value=session_factory),
        patch("app.bot.handlers.alerts.get_settings") as mock_settings,
    ):
        mock_settings.return_value.admin_chat_id = ADMIN_ID
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


async def test_alert_notification_sent_on_fire(db_session, session_factory):
    """When an alert fires, a Telegram message should be sent."""
    from app.api.ingestion import _run_alert_evaluation
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="notify-test.com", admin_chat_id=ADMIN_ID)
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


async def test_alert_notification_message_varies_by_condition(db_session, session_factory):
    """Different conditions produce different notification messages."""
    from app.api.ingestion import _run_alert_evaluation
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="msg-vary.com", admin_chat_id=ADMIN_ID)
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


async def test_no_notification_when_no_alerts_fire(db_session, session_factory):
    """No notification sent if no alerts match or fire."""
    from app.api.ingestion import _run_alert_evaluation
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(session, name="no-fire.com", admin_chat_id=ADMIN_ID)
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
