"""Tests for rich-message sending (Bot API 10.1 sendRichMessage) and fallback."""

from unittest.mock import AsyncMock, MagicMock

import app.bot.rich as rich
from app.bot.handlers.digest import _project_rich_section, _ProjectDigest
from app.bot.rich import reply_rich_html


def _make_message():
    message = MagicMock()
    message.chat_id = 111
    message.reply_text = AsyncMock()
    return message


async def test_rich_send_success_skips_fallback(monkeypatch):
    """When sendRichMessage succeeds, no plain sendMessage is sent."""
    sent = {}

    async def _record(message, rich_html):
        sent["html"] = rich_html

    monkeypatch.setattr(rich, "_call_send_rich_message", _record)
    message = _make_message()

    await reply_rich_html(message, "<h4>hi</h4>", "hi")

    assert sent["html"] == "<h4>hi</h4>"
    message.reply_text.assert_not_called()


async def test_rich_send_failure_falls_back_to_html():
    """Any sendRichMessage error degrades to reply_text with HTML parse mode.

    The conftest autouse fixture already patches the raw call to raise.
    """
    message = _make_message()

    await reply_rich_html(message, "<h4>hi</h4>", "<b>hi</b>")

    message.reply_text.assert_called_once_with("<b>hi</b>", parse_mode="HTML")


def test_digest_rich_section_renders_table():
    """Project section: sub-heading + one table row per metric."""
    d = _ProjectDigest(
        name="my<site>.com",
        sessions_curr=1234,
        sessions_prev=1000,
        events=[("signup", 12, 0)],
        has_alerts=True,
    )
    section = _project_rich_section(d)

    assert "<h5>📦 my&lt;site&gt;.com</h5>" in section
    assert "<table>" in section and "</table>" in section
    assert "<td>👤 Sessions</td><td><b>1,234</b></td>" in section
    assert "<td>🎯 signup</td><td><b>12</b></td>" in section
    assert "🆕" in section  # delta for a brand-new event
    assert "No alerts" not in section


def test_digest_rich_section_no_alerts_hint():
    """Without alerts the table still shows sessions, plus the /alerts hint."""
    d = _ProjectDigest(
        name="quiet.com",
        sessions_curr=5,
        sessions_prev=5,
        events=[],
        has_alerts=False,
    )
    section = _project_rich_section(d)

    assert "<td>👤 Sessions</td>" in section
    assert "🎯" not in section
    assert "No alerts — set one with /alerts" in section
