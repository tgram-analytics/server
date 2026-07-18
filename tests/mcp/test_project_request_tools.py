"""Tests for the project-create request MCP tools.

Covers:

- ``create_project``: auth boundary, input validation (name length,
  allowlist size/entries/normalization), the pending-cap error path,
  and the happy path (service dispatch + Telegram notification +
  result shape).
- ``get_project_request_status``: auth boundary, UUID validation,
  not-found/ownership error, approved result shape, and the lazy
  expiry path (``is_expired`` → ``claim_request(status="expired")``).

The ``app.services.project_requests`` service functions are mocked
(the tools import them lazily inside the handler body, so patching the
service module attribute is picked up at call time). The session is a
minimal stand-in because the ``project_create_requests`` table uses a
Postgres ``ARRAY`` column that doesn't compile on SQLite.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import TextContent

REQUEST_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
TELEGRAM_USER_ID = 424242


def _request_row(
    rid: uuid.UUID = REQUEST_ID,
    *,
    status: str = "pending",
    name: str = "myapp",
    project_id: uuid.UUID | None = None,
):
    """Build a ProjectCreateRequest-like object with the attrs the tools read."""
    return SimpleNamespace(
        id=rid,
        name=name,
        status=status,
        domain_allowlist=["example.com"],
        project_id=project_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        resolved_at=None,
    )


async def _invoke(mcp, tool_name, **kwargs):
    """Invoke a tool's underlying fn, downcasting Pydantic results to dicts.

    Mirrors ``tests.mcp.conftest._call`` — needed locally because that
    helper's second positional parameter is called ``name``, which
    collides with ``create_project``'s ``name`` keyword argument.
    """
    from pydantic import BaseModel

    result = await mcp._tool_manager.get_tool(tool_name).fn(**kwargs)
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    return result


class _FakeResult:
    """Result stand-in for the User.telegram_user_id lookup."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Minimal AsyncSession stand-in for the request tools.

    ``create_project`` runs one SELECT (the owner's telegram_user_id)
    and commits; ``get_project_request_status`` only commits (on the
    lazy-expiry path). Everything else goes through mocked services.
    """

    def __init__(self, telegram_user_id: int | None = TELEGRAM_USER_ID) -> None:
        self.telegram_user_id = telegram_user_id
        self.commit_count = 0

    async def execute(self, _stmt):
        return _FakeResult(self.telegram_user_id)

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        pass


@pytest.fixture
def request_session(monkeypatch):
    """Install a ``_FakeSession`` as the projects-module open_session yield."""
    session = _FakeSession()

    @asynccontextmanager
    async def _fake():
        yield session

    import app.mcp.tools.projects as projects_mod

    monkeypatch.setattr(projects_mod, "open_session", _fake)
    return session


# ── create_project ──────────────────────────────────────────────────────────


async def test_create_project_no_token_returns_error(fresh_mcp, set_auth_token):
    with set_auth_token(None):
        result = await _invoke(fresh_mcp, "create_project", name="myapp")
    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], TextContent)
    assert result[0].isError is True
    assert "not authenticated" in result[0].text


@pytest.mark.parametrize("bad_name", ["", "   "])
async def test_create_project_blank_name_returns_error(
    fresh_mcp, set_auth_token, user_a_id, bad_name
):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await _invoke(fresh_mcp, "create_project", name=bad_name)
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "invalid name" in result[0].text


async def test_create_project_name_too_long_returns_error(fresh_mcp, set_auth_token, user_a_id):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await _invoke(fresh_mcp, "create_project", name="x" * 121)
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "invalid name" in result[0].text


async def test_create_project_allowlist_too_many_entries(fresh_mcp, set_auth_token, user_a_id):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await _invoke(
            fresh_mcp,
            "create_project",
            name="myapp",
            domain_allowlist=[f"d{i}.example.com" for i in range(21)],
        )
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "at most 20 entries" in result[0].text


async def test_create_project_allowlist_blank_entry(fresh_mcp, set_auth_token, user_a_id):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await _invoke(
            fresh_mcp,
            "create_project",
            name="myapp",
            domain_allowlist=["good.example.com", "   "],
        )
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "invalid domain_allowlist entry" in result[0].text


async def test_create_project_happy_path(
    fresh_mcp, set_auth_token, monkeypatch, request_session, user_a_id
):
    """Files a pending request, notifies the owner, returns request_id."""
    row = _request_row(name="My App")
    create_mock = AsyncMock(return_value=row)
    monkeypatch.setattr("app.services.project_requests.create_request", create_mock)
    notify_mock = AsyncMock()
    monkeypatch.setattr("app.mcp.notify.notify_project_request", notify_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await _invoke(
            fresh_mcp,
            "create_project",
            name="  My App  ",
            domain_allowlist=[" example.com "],
        )

    assert isinstance(result, dict)
    assert result["status"] == "pending"
    assert result["request_id"] == str(REQUEST_ID)
    assert "get_project_request_status" in result["message"]

    # The service got the trimmed inputs and the authenticated owner.
    create_mock.assert_awaited_once()
    kwargs = create_mock.await_args.kwargs
    assert kwargs["owner_user_id"] == user_a_id
    assert kwargs["name"] == "My App"
    assert kwargs["domain_allowlist"] == ["example.com"]
    assert kwargs["requested_via"] == "mcp"

    # The Telegram notification was awaited with the right request/name.
    notify_mock.assert_awaited_once_with(
        chat_id=TELEGRAM_USER_ID,
        request_id=str(REQUEST_ID),
        name="My App",
        domain_allowlist=["example.com"],
    )

    # The request row was committed before notifying.
    assert request_session.commit_count == 1


async def test_create_project_allowlist_is_normalized(
    fresh_mcp, set_auth_token, monkeypatch, request_session, user_a_id
):
    """Entries are canonicalized to bare lowercase hosts before storage."""
    row = _request_row(name="myapp")
    create_mock = AsyncMock(return_value=row)
    monkeypatch.setattr("app.services.project_requests.create_request", create_mock)
    notify_mock = AsyncMock()
    monkeypatch.setattr("app.mcp.notify.notify_project_request", notify_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await _invoke(
            fresh_mcp,
            "create_project",
            name="myapp",
            domain_allowlist=["HTTPS://Example.com/path", "sub.Example.org"],
        )

    assert isinstance(result, dict)
    assert result["status"] == "pending"

    create_mock.assert_awaited_once()
    assert create_mock.await_args.kwargs["domain_allowlist"] == [
        "example.com",
        "sub.example.org",
    ]
    # The notification shows the same normalized entries.
    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["domain_allowlist"] == [
        "example.com",
        "sub.example.org",
    ]


async def test_create_project_allowlist_all_junk_returns_error(
    fresh_mcp, set_auth_token, monkeypatch, request_session, user_a_id
):
    """Non-blank entries that all normalize away are rejected up front."""
    create_mock = AsyncMock()
    monkeypatch.setattr("app.services.project_requests.create_request", create_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await _invoke(
            fresh_mcp,
            "create_project",
            name="myapp",
            domain_allowlist=["*.", "https://"],
        )

    assert isinstance(result, list)
    assert result[0].isError is True
    assert "no valid domains" in result[0].text
    create_mock.assert_not_awaited()
    assert request_session.commit_count == 0


async def test_create_project_pending_cap_returns_error(
    fresh_mcp, set_auth_token, monkeypatch, request_session, user_a_id
):
    from app.services.project_requests import PendingCapExceededError

    monkeypatch.setattr(
        "app.services.project_requests.create_request",
        AsyncMock(side_effect=PendingCapExceededError("cap hit")),
    )
    notify_mock = AsyncMock()
    monkeypatch.setattr("app.mcp.notify.notify_project_request", notify_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await _invoke(fresh_mcp, "create_project", name="myapp")

    assert isinstance(result, list)
    assert result[0].isError is True
    assert "too many pending" in result[0].text
    notify_mock.assert_not_awaited()
    assert request_session.commit_count == 0


# ── get_project_request_status ──────────────────────────────────────────────


async def test_get_request_status_no_token_returns_error(fresh_mcp, set_auth_token):
    with set_auth_token(None):
        result = await _invoke(fresh_mcp, "get_project_request_status", request_id=str(REQUEST_ID))
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "not authenticated" in result[0].text


async def test_get_request_status_invalid_uuid_returns_error(fresh_mcp, set_auth_token, user_a_id):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await _invoke(fresh_mcp, "get_project_request_status", request_id="not-a-uuid")
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "must be a UUID" in result[0].text


async def test_get_request_status_not_found_returns_error(
    fresh_mcp, set_auth_token, monkeypatch, request_session, user_a_id
):
    monkeypatch.setattr(
        "app.services.project_requests.get_request",
        AsyncMock(return_value=None),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await _invoke(fresh_mcp, "get_project_request_status", request_id=str(REQUEST_ID))

    assert isinstance(result, list)
    assert result[0].isError is True
    assert "not found or you don't have access" in result[0].text


async def test_get_request_status_approved(
    fresh_mcp, set_auth_token, monkeypatch, request_session, user_a_id, project_a_id
):
    row = _request_row(status="approved", project_id=project_a_id)
    get_mock = AsyncMock(return_value=row)
    monkeypatch.setattr("app.services.project_requests.get_request", get_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await _invoke(fresh_mcp, "get_project_request_status", request_id=str(REQUEST_ID))

    assert isinstance(result, dict)
    assert result["status"] == "approved"
    assert result["project_id"] == str(project_a_id)
    assert result["request_id"] == str(REQUEST_ID)
    assert "rotate_api_key" in result["message"]
    # Ownership scoping: the lookup used the authenticated user's id.
    get_mock.assert_awaited_once_with(request_session, REQUEST_ID, user_a_id)


async def test_get_request_status_pending_expired_lazily_resolves(
    fresh_mcp, set_auth_token, monkeypatch, request_session, user_a_id
):
    """A pending-but-stale row is resolved to ``expired`` on read."""
    row = _request_row(status="pending")
    monkeypatch.setattr(
        "app.services.project_requests.get_request",
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(
        "app.services.project_requests.is_expired",
        MagicMock(return_value=True),
    )

    async def _claim(_session, request, *, status, project_id=None):
        request.status = status
        request.project_id = project_id
        return True

    claim_mock = AsyncMock(side_effect=_claim)
    monkeypatch.setattr("app.services.project_requests.claim_request", claim_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await _invoke(fresh_mcp, "get_project_request_status", request_id=str(REQUEST_ID))

    assert isinstance(result, dict)
    assert result["status"] == "expired"
    assert result["project_id"] is None
    assert "expired" in result["message"]

    claim_mock.assert_awaited_once()
    assert claim_mock.await_args.kwargs["status"] == "expired"
    # The lazy expiry was committed.
    assert request_session.commit_count == 1
