"""Tests for the Full Pie Charts button and `_send_full_pie_charts` handler."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Message

ADMIN_ID = 111


# ── Test 1: button is present in event-detail keyboard ─────────────────────────


async def test_full_pie_charts_button_present_in_event_detail(session_factory, singleton_user):
    """Event detail keyboard exposes both pie buttons with correct callbacks.

    Asserts that:
      - "🥧 Specific Pie Chart" → callback_data "evta:pie"
      - "🥧📊 Full Pie Charts"  → callback_data "evta:pie_all"
      - The two buttons sit on consecutive rows, in that order.
    """
    from app.bot.handlers.events import _show_event_detail
    from app.bot.states import BotStateService
    from app.services.events import insert_event
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="pie-detail.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = project.id

        # Seed at least one event so the count queries return data.
        await insert_event(
            session,
            project_id=pid,
            event_name="purchase",
            session_id="s1",
            properties={"plan": "pro"},
            timestamp=datetime.now(UTC) - timedelta(hours=1),
        )

        # Required: _show_event_detail expects state with flow="events".
        svc = BotStateService(session)
        await svc.save(
            ADMIN_ID,
            flow="events",
            step="list",
            payload={"project_id": str(pid)},
        )
        await session.commit()

    query = MagicMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = ADMIN_ID
    query.edit_message_text = AsyncMock()

    await _show_event_detail(query, "purchase", singleton_user.id)

    query.edit_message_text.assert_called_once()
    keyboard = query.edit_message_text.call_args[1].get("reply_markup")
    assert keyboard is not None, "event detail must attach an InlineKeyboardMarkup"

    rows = keyboard.inline_keyboard
    # Find the row containing the Specific Pie Chart button.
    specific_row_idx = None
    full_row_idx = None
    for idx, row in enumerate(rows):
        for btn in row:
            if btn.callback_data == "evta:pie":
                assert btn.text == "🥧 Specific Pie Chart"
                specific_row_idx = idx
            elif btn.callback_data == "evta:pie_all":
                assert btn.text == "🥧📊 Full Pie Charts"
                full_row_idx = idx

    assert specific_row_idx is not None, "Specific Pie Chart button missing"
    assert full_row_idx is not None, "Full Pie Charts button missing"
    # Full Pie Charts must sit on its own row, directly after Specific Pie Chart.
    assert full_row_idx == specific_row_idx + 1, (
        "Full Pie Charts must be on the row immediately after Specific Pie Chart"
    )
    assert len(rows[full_row_idx]) == 1, "Full Pie Charts must be alone on its row"


# ── Test 2: fans out one photo per non-empty property ──────────────────────────


async def test_full_pie_charts_sends_one_photo_per_property(session_factory, singleton_user):
    """Each property with data produces exactly one reply_photo call."""
    from app.bot.handlers.events import _send_full_pie_charts
    from app.bot.states import BotStateService
    from app.services.events import insert_event
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="pie-fanout.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = project.id

        # Two distinct properties: plan + currency.
        seed = [
            {"plan": "pro", "currency": "USD"},
            {"plan": "free", "currency": "EUR"},
            {"plan": "pro", "currency": "USD"},
        ]
        for i, props in enumerate(seed):
            await insert_event(
                session,
                project_id=pid,
                event_name="purchase",
                session_id=f"s{i}",
                properties=props,
                timestamp=datetime.now(UTC) - timedelta(hours=i + 1),
            )

        svc = BotStateService(session)
        await svc.save(
            ADMIN_ID,
            flow="events",
            step="detail",
            payload={"project_id": str(pid), "event_name": "purchase"},
        )
        await session.commit()

    query = MagicMock()
    query.data = "evta:pie_all"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = ADMIN_ID
    query.message.reply_photo = AsyncMock()
    query.message.reply_text = AsyncMock()

    fake_png = b"FAKE_PNG_BYTES"
    with patch(
        "app.bot.handlers.events.generate_pie_chart",
        new=AsyncMock(return_value=fake_png),
    ) as mock_chart:
        await _send_full_pie_charts(query, singleton_user.id)

    assert query.message.reply_photo.call_count == 2, (
        f"expected 2 reply_photo calls, got {query.message.reply_photo.call_count}"
    )
    assert mock_chart.call_count == 2

    # Gather captions and verify both property keys are mentioned.
    captions = []
    for call in query.message.reply_photo.call_args_list:
        cap = call[1].get("caption") or ""
        captions.append(cap)
        # No reply_markup attached to the photos — the trailing text carries
        # the Back keyboard.
        assert "reply_markup" not in call[1], (
            f"reply_photo should not carry a reply_markup; got {call[1]}"
        )

    joined = " | ".join(captions)
    assert "plan" in joined, f"plan caption missing: {captions}"
    assert "currency" in joined, f"currency caption missing: {captions}"

    # Trailing back-nav text message fires exactly once.
    query.message.reply_text.assert_called_once()


# ── Test 3: silently skips empty + failing keys ────────────────────────────────


async def test_full_pie_charts_skips_keys_with_no_data_and_chart_failures(
    session_factory, singleton_user
):
    """Empty `top_properties` results and ChartGenerationError raisers are skipped.

    Three property keys are seeded:
      - good_key  → valid values, chart succeeds → 1 reply_photo
      - fail_key  → valid values, chart raises    → skipped
      - bad_key   → all None values, top_properties returns []  → skipped
    Final state: exactly one reply_photo, one trailing reply_text, no exception.
    """
    from app.bot.handlers.events import _send_full_pie_charts
    from app.bot.states import BotStateService
    from app.services.charts import ChartGenerationError
    from app.services.events import insert_event
    from app.services.projects import create_project

    async with session_factory() as session:
        project, _ = await create_project(
            session,
            name="pie-skip.com",
            admin_chat_id=ADMIN_ID,
            owner_user_id=singleton_user.id,
        )
        await session.commit()
        pid = project.id

        # good_key + fail_key: real values. bad_key: None values
        # (top_properties filters .isnot(None) → empty list).
        seed = [
            {"good_key": "alpha", "fail_key": "x", "bad_key": None},
            {"good_key": "beta", "fail_key": "y", "bad_key": None},
        ]
        for i, props in enumerate(seed):
            await insert_event(
                session,
                project_id=pid,
                event_name="purchase",
                session_id=f"s{i}",
                properties=props,
                timestamp=datetime.now(UTC) - timedelta(hours=i + 1),
            )

        svc = BotStateService(session)
        await svc.save(
            ADMIN_ID,
            flow="events",
            step="detail",
            payload={"project_id": str(pid), "event_name": "purchase"},
        )
        await session.commit()

    query = MagicMock()
    query.data = "evta:pie_all"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock(spec=Message)
    query.message.chat_id = ADMIN_ID
    query.message.reply_photo = AsyncMock()
    query.message.reply_text = AsyncMock()

    async def _chart_side_effect(pie_data, *, title):
        if "fail_key" in title:
            raise ChartGenerationError("simulated quickchart failure")
        return b"OK_PNG"

    with patch(
        "app.bot.handlers.events.generate_pie_chart",
        new=AsyncMock(side_effect=_chart_side_effect),
    ):
        # Must not raise — silent skip on both empty data and chart errors.
        await _send_full_pie_charts(query, singleton_user.id)

    assert query.message.reply_photo.call_count == 1, (
        f"only good_key should produce a photo; got {query.message.reply_photo.call_count}"
    )
    caption = query.message.reply_photo.call_args[1].get("caption") or ""
    assert "good_key" in caption, f"surviving caption should mention good_key: {caption!r}"
    assert "fail_key" not in caption
    assert "bad_key" not in caption

    # Trailing back-nav text was sent (sent_count > 0).
    query.message.reply_text.assert_called_once()
