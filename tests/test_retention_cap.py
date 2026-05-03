"""Tests for the deployment-wide retention cap (``Settings.max_retention_days``).

When ``max_retention_days`` is set (the cloud configuration), the bot's
/settings retention flow rejects values above the cap and rejects
``0`` ("forever") because it would bypass the cap entirely.

When ``max_retention_days`` is ``None`` (the OSS / self-host default),
the handler accepts any non-negative integer including 0.

These tests poke ``handle_set_retention_text`` directly with mocked
session and ``BotStateService``, sidestepping the DB-gated fixture
suite — the cap check runs before any DB I/O so this stays honest.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import config as app_config


@pytest.fixture
def _restore_settings_class():
    """Snapshot/restore ``app.core.config.Settings`` around each test.

    Our tests mutate the class on the module to inject ``max_retention_days``
    overrides; we don't want the mutation to bleed across tests.
    """
    original = app_config.Settings
    yield
    app_config.Settings = original


def _make_settings_with_cap(cap: int | None) -> type[app_config.Settings]:
    """Build a Settings subclass whose ``max_retention_days`` defaults to *cap*.

    Required env-var fields are stubbed via ``ClassVar``-ish defaults so we
    don't need real env vars in unit tests.
    """

    class _StubSettings(app_config.Settings):
        telegram_bot_token: str = "stub-token"
        admin_chat_id: int = 0
        database_url: str = "sqlite+aiosqlite:///:memory:"
        secret_key: str = "stub-secret"
        max_retention_days: int | None = cap

    return _StubSettings


def _make_update(text: str, chat_id: int = 42):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_state(project_id: str | None = None):
    state = MagicMock()
    state.payload = {"project_id": project_id or str(uuid.uuid4())}
    return state


async def test_capped_deployment_rejects_value_above_cap(monkeypatch, _restore_settings_class):
    """Submitting 90 with cap=60 → friendly error, no DB write."""
    from app.bot.handlers import settings as handler

    app_config.Settings = _make_settings_with_cap(60)

    update = _make_update("90")
    session = MagicMock()
    session.execute = AsyncMock()
    svc = MagicMock()
    svc.clear = AsyncMock()
    state = _make_state()

    await handler.handle_set_retention_text(update, session, svc, state)

    update.message.reply_text.assert_awaited_once()
    msg = update.message.reply_text.await_args.args[0]
    assert "60" in msg
    session.execute.assert_not_awaited()


async def test_capped_deployment_rejects_zero(monkeypatch, _restore_settings_class):
    """Submitting 0 with a cap set → rejected (would bypass the cap)."""
    from app.bot.handlers import settings as handler

    app_config.Settings = _make_settings_with_cap(60)

    update = _make_update("0")
    session = MagicMock()
    session.execute = AsyncMock()
    svc = MagicMock()
    svc.clear = AsyncMock()
    state = _make_state()

    await handler.handle_set_retention_text(update, session, svc, state)

    update.message.reply_text.assert_awaited_once()
    msg = update.message.reply_text.await_args.args[0]
    assert "60" in msg
    # 0 must be explicitly disallowed, distinct from other "out of range"
    # messages, so the user understands "forever" is no longer a choice.
    session.execute.assert_not_awaited()


async def test_uncapped_deployment_still_accepts_zero(monkeypatch, _restore_settings_class):
    """OSS (cap=None) keeps the original behavior: 0 means forever."""
    from app.bot.handlers import settings as handler

    app_config.Settings = _make_settings_with_cap(None)

    update = _make_update("0")

    # Stub the DB lookup: a row already exists for this project.
    existing = MagicMock()
    existing.retention_days = 90
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    svc = MagicMock()
    svc.clear = AsyncMock()
    state = _make_state()

    await handler.handle_set_retention_text(update, session, svc, state)

    # The lookup happened (no early-return on the cap).
    session.execute.assert_awaited_once()
    assert existing.retention_days == 0
    msg = update.message.reply_text.await_args.args[0]
    assert "forever" in msg.lower()


async def test_capped_deployment_accepts_value_within_cap(monkeypatch, _restore_settings_class):
    """30 with cap=60 passes through and updates the row."""
    from app.bot.handlers import settings as handler

    app_config.Settings = _make_settings_with_cap(60)

    update = _make_update("30")

    existing = MagicMock()
    existing.retention_days = 90
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    svc = MagicMock()
    svc.clear = AsyncMock()
    state = _make_state()

    await handler.handle_set_retention_text(update, session, svc, state)

    assert existing.retention_days == 30
    msg = update.message.reply_text.await_args.args[0]
    assert "30" in msg
