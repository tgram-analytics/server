"""Tests for the ``pcr:yes/no:<uuid>`` project-create request callback.

Uses the same fake-``Update``/``CallbackQuery`` idiom as
``tests/test_mcp_token_handler.py`` / ``tests/test_settings_handler.py``,
but with the ``@requires_user`` plumbing fully mocked (session factory +
user resolution) so the suite runs without Postgres: the
``project_create_requests`` model uses a Postgres ``ARRAY`` column that
doesn't compile on SQLite, so the service functions are patched in the
handler module's namespace (where they were imported) and the session is
a MagicMock whose ``commit`` we can assert on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.bot.handlers.project_requests as handler_mod
from app.bot.handlers.project_requests import project_request_callback
from app.extensions import ExtensionError

ADMIN_ID = 111
REQUEST_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
PROJECT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings needs these to build (the approve path calls get_settings)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234567890:test-token-for-testing-only")
    monkeypatch.setenv("ADMIN_CHAT_ID", str(ADMIN_ID))
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://tga:password@localhost/tganalytics_test"
    )
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-not-for-production")
    monkeypatch.setenv("WEBHOOK_BASE_URL", "https://example.com")


@pytest.fixture
def stub_user():
    """User-like object injected by the (mocked) ``@requires_user`` resolver."""
    return SimpleNamespace(id=uuid.uuid4(), telegram_user_id=ADMIN_ID)


@pytest.fixture
def mock_session():
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.fixture(autouse=True)
def wire_auth(monkeypatch, stub_user, mock_session):
    """Mock the ``@requires_user`` plumbing: session factory + user resolution.

    The decorator opens a session via ``app.core.database.get_session_factory``
    and resolves the caller via ``app.bot.auth.get_current_user`` — both are
    looked up at call time, so patching the module attributes is enough.
    """

    class _SessionCM:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.core.database.get_session_factory", lambda: lambda: _SessionCM())
    monkeypatch.setattr("app.bot.auth.get_current_user", AsyncMock(return_value=stub_user))


def _make_callback(data: str):
    """Fake a ``pcr:...`` callback-query update + context."""
    update = MagicMock()
    update.effective_chat.id = ADMIN_ID
    update.effective_user.id = ADMIN_ID
    update.message = None
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    ctx = MagicMock()
    return update, ctx, query


def _pending_row(name: str = "agent-app"):
    return SimpleNamespace(
        id=REQUEST_ID,
        name=name,
        status="pending",
        domain_allowlist=["example.com"],
        project_id=None,
        created_at=datetime(2026, 7, 18, tzinfo=UTC),
        resolved_at=None,
    )


@pytest.fixture
def claim_mock(monkeypatch):
    """Patched ``claim_request`` that mimics winning the CAS transition."""

    async def _claim(_session, request, *, status, project_id=None):
        request.status = status
        request.project_id = project_id
        return True

    mock = AsyncMock(side_effect=_claim)
    monkeypatch.setattr(handler_mod, "claim_request", mock)
    return mock


async def test_approve_creates_project_and_shows_key(
    monkeypatch, mock_session, stub_user, claim_mock
):
    row = _pending_row(name="agent-app")
    monkeypatch.setattr(handler_mod, "get_request", AsyncMock(return_value=row))
    monkeypatch.setattr(handler_mod, "is_expired", MagicMock(return_value=False))
    project = SimpleNamespace(id=PROJECT_ID, name="agent-app")
    create_mock = AsyncMock(return_value=(project, "plainkey123"))
    monkeypatch.setattr(handler_mod, "create_project", create_mock)

    update, ctx, query = _make_callback(f"pcr:yes:{REQUEST_ID}")
    await project_request_callback(update, ctx)

    # The real project was created for the request's owner with its allowlist.
    create_mock.assert_awaited_once()
    kwargs = create_mock.await_args.kwargs
    assert kwargs["name"] == "agent-app"
    assert kwargs["owner_user_id"] == stub_user.id
    assert kwargs["admin_chat_id"] == ADMIN_ID
    assert kwargs["domain_allowlist"] == ["example.com"]

    # The request was claimed as approved and linked to the project.
    claim_mock.assert_awaited_once()
    assert claim_mock.await_args.kwargs["status"] == "approved"
    assert claim_mock.await_args.kwargs["project_id"] == PROJECT_ID
    assert row.status == "approved"
    mock_session.commit.assert_awaited()

    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "created" in text
    assert "plainkey123" in text


async def test_reject_resolves_without_creating(monkeypatch, mock_session, claim_mock):
    row = _pending_row()
    monkeypatch.setattr(handler_mod, "get_request", AsyncMock(return_value=row))
    monkeypatch.setattr(handler_mod, "is_expired", MagicMock(return_value=False))
    create_mock = AsyncMock()
    monkeypatch.setattr(handler_mod, "create_project", create_mock)

    update, ctx, query = _make_callback(f"pcr:no:{REQUEST_ID}")
    await project_request_callback(update, ctx)

    claim_mock.assert_awaited_once()
    assert claim_mock.await_args.kwargs["status"] == "rejected"
    assert row.status == "rejected"
    create_mock.assert_not_awaited()
    mock_session.commit.assert_awaited()

    text = query.edit_message_text.call_args[0][0]
    assert "Rejected" in text


async def test_malformed_callback_data(monkeypatch, claim_mock):
    get_mock = AsyncMock()
    monkeypatch.setattr(handler_mod, "get_request", get_mock)

    update, ctx, query = _make_callback("pcr:bogus")
    await project_request_callback(update, ctx)

    query.edit_message_text.assert_called_once_with("❌ Invalid request.")
    get_mock.assert_not_awaited()
    claim_mock.assert_not_awaited()


async def test_request_not_found(monkeypatch, claim_mock):
    monkeypatch.setattr(handler_mod, "get_request", AsyncMock(return_value=None))

    update, ctx, query = _make_callback(f"pcr:yes:{REQUEST_ID}")
    await project_request_callback(update, ctx)

    query.edit_message_text.assert_called_once_with("❌ Request not found.")
    claim_mock.assert_not_awaited()


async def test_already_resolved_request(monkeypatch, claim_mock):
    row = _pending_row()
    row.status = "approved"
    monkeypatch.setattr(handler_mod, "get_request", AsyncMock(return_value=row))
    create_mock = AsyncMock()
    monkeypatch.setattr(handler_mod, "create_project", create_mock)

    update, ctx, query = _make_callback(f"pcr:yes:{REQUEST_ID}")
    await project_request_callback(update, ctx)

    text = query.edit_message_text.call_args[0][0]
    assert "already approved" in text
    create_mock.assert_not_awaited()
    claim_mock.assert_not_awaited()


async def test_expired_pending_row(monkeypatch, mock_session, claim_mock):
    row = _pending_row()
    monkeypatch.setattr(handler_mod, "get_request", AsyncMock(return_value=row))
    monkeypatch.setattr(handler_mod, "is_expired", MagicMock(return_value=True))
    create_mock = AsyncMock()
    monkeypatch.setattr(handler_mod, "create_project", create_mock)

    update, ctx, query = _make_callback(f"pcr:yes:{REQUEST_ID}")
    await project_request_callback(update, ctx)

    claim_mock.assert_awaited_once()
    assert claim_mock.await_args.kwargs["status"] == "expired"
    assert row.status == "expired"
    create_mock.assert_not_awaited()
    mock_session.commit.assert_awaited()

    text = query.edit_message_text.call_args[0][0]
    assert "expired" in text


async def test_extension_error_leaves_request_pending(monkeypatch, claim_mock):
    row = _pending_row()
    monkeypatch.setattr(handler_mod, "get_request", AsyncMock(return_value=row))
    monkeypatch.setattr(handler_mod, "is_expired", MagicMock(return_value=False))
    monkeypatch.setattr(
        handler_mod,
        "create_project",
        AsyncMock(side_effect=ExtensionError("🚫 Plan limit reached — upgrade to add projects.")),
    )

    update, ctx, query = _make_callback(f"pcr:yes:{REQUEST_ID}")
    await project_request_callback(update, ctx)

    # The plugin error was rendered verbatim, the request stayed pending,
    # and the Approve/Reject keyboard was re-attached for a retry.
    query.edit_message_text.assert_called_once()
    args, kwargs = query.edit_message_text.call_args
    assert args[0] == "🚫 Plan limit reached — upgrade to add projects."
    markup = kwargs["reply_markup"]
    buttons = [button for line in markup.inline_keyboard for button in line]
    assert [b.callback_data for b in buttons] == [
        f"pcr:yes:{REQUEST_ID}",
        f"pcr:no:{REQUEST_ID}",
    ]
    claim_mock.assert_not_awaited()
    assert row.status == "pending"


async def test_double_tap_approve_already_claimed(monkeypatch, mock_session, claim_mock):
    """Losing the CAS race rolls back the just-created project."""
    row = _pending_row()
    monkeypatch.setattr(handler_mod, "get_request", AsyncMock(return_value=row))
    monkeypatch.setattr(handler_mod, "is_expired", MagicMock(return_value=False))
    project = SimpleNamespace(id=PROJECT_ID, name="agent-app")
    monkeypatch.setattr(
        handler_mod, "create_project", AsyncMock(return_value=(project, "plainkey123"))
    )
    claim_mock.side_effect = None
    claim_mock.return_value = False

    update, ctx, query = _make_callback(f"pcr:yes:{REQUEST_ID}")
    await project_request_callback(update, ctx)

    # create_project may have run — the rollback discards it. (The
    # ``@requires_user`` decorator still commits on return, but that
    # commit only closes the now-empty transaction.)
    mock_session.rollback.assert_awaited_once()
    query.edit_message_text.assert_called_once_with("ℹ️ This request was already handled.")


async def test_sender_mismatch_is_silently_ignored(monkeypatch, claim_mock):
    """A callback whose sender isn't the resolved owner does nothing."""
    get_mock = AsyncMock()
    monkeypatch.setattr(handler_mod, "get_request", get_mock)
    create_mock = AsyncMock()
    monkeypatch.setattr(handler_mod, "create_project", create_mock)

    update, ctx, query = _make_callback(f"pcr:yes:{REQUEST_ID}")
    update.effective_user.id = ADMIN_ID + 1

    await project_request_callback(update, ctx)

    get_mock.assert_not_awaited()
    create_mock.assert_not_awaited()
    claim_mock.assert_not_awaited()
    query.edit_message_text.assert_not_called()
