"""Notifications skip chat ids that no Telegram chat can have (>= 2**52)."""

from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from app.core.telegram_ids import TELEGRAM_USER_ID_CEILING, is_unreachable_chat_id
from app.models.alert import AlertCondition

UNREACHABLE = 9_000_000_000_000_000_000


def test_is_unreachable_chat_id() -> None:
    assert TELEGRAM_USER_ID_CEILING == 2**52
    assert is_unreachable_chat_id(2**52)
    assert is_unreachable_chat_id(UNREACHABLE)
    assert not is_unreachable_chat_id(2**52 - 1)
    assert not is_unreachable_chat_id(123456789)
    # Group and channel chat ids are negative and must stay reachable.
    assert not is_unreachable_chat_id(-1001234567890)


async def test_project_request_notify_skips_unreachable_chat() -> None:
    from app.mcp.notify import notify_project_request

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    with patch("app.bot.setup.get_bot", return_value=mock_bot):
        await notify_project_request(
            chat_id=UNREACHABLE, request_id="r1", name="x", domain_allowlist=[]
        )
        mock_bot.send_message.assert_not_called()

        await notify_project_request(chat_id=123, request_id="r2", name="x", domain_allowlist=[])
        mock_bot.send_message.assert_called_once()


async def test_alert_for_unreachable_chat_records_no_chat_without_sending(
    db_session, session_factory, singleton_user
) -> None:
    from app.api.ingestion import _run_alert_evaluation
    from app.models.alert_delivery import AlertDelivery
    from app.services.alerts import create_alert
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="no-chat.example.com",
            admin_chat_id=UNREACHABLE,
            owner_user_id=singleton_user.id,
        )
        alert = await create_alert(
            session,
            project_id=project.id,
            event_name="no_chat_event",
            condition=AlertCondition.every,
        )
        await session.commit()
        pid, alert_id = project.id, alert.id

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    # The singleton_user teardown deletes the user, cascading to the project.
    with (
        patch("app.api.ingestion.get_session_factory", return_value=session_factory),
        patch("app.bot.setup.get_bot", return_value=mock_bot),
    ):
        await _run_alert_evaluation(pid, "no_chat_event")

    mock_bot.send_message.assert_not_called()
    async with session_factory() as session:
        result = await session.execute(select(AlertDelivery).where(AlertDelivery.project_id == pid))
        rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].alert_id == alert_id
    assert rows[0].delivered is False
    assert rows[0].error == "no_chat"
