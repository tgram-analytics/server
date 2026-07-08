"""Tests for the project-discovery MCP tools.

Covers:

- ``list_projects``: happy path returns the user's projects; no-token
  path returns ``isError=True``.
- ``get_project``: happy path returns the project; cross-user returns
  ``isError=True``; bad UUID input returns ``isError=True``.
- ``list_event_names``: happy path forwards to the analytics service;
  cross-user returns ``isError=True``.

The OSS service functions are mocked so the suite doesn't depend on
Postgres-specific column types. We use the real
``assert_project_owned_by`` helper (which queries
``services.projects.get_project``) so the cross-user branch is
exercised end-to-end through the helper.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent

from app.core.security import hash_api_key


def _project_obj(pid, name="myapp", owner=None):
    """Build a project-like object with the attributes the tools read."""
    return SimpleNamespace(
        id=pid,
        name=name,
        owner_user_id=owner,
        domain_allowlist=["example.com"],
        rate_limit_per_second=10,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ── list_projects ───────────────────────────────────────────────────────────


async def test_list_projects_no_token_returns_error(fresh_mcp, call_tool, set_auth_token):
    """Missing auth token → ``isError=True`` content part."""
    with set_auth_token(None):
        result = await call_tool(fresh_mcp, "list_projects")
    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], TextContent)
    assert result[0].isError is True
    assert "not authenticated" in result[0].text


async def test_list_projects_happy_path(
    fresh_mcp, call_tool, set_auth_token, monkeypatch, patch_open_session, user_a_id, project_a_id
):
    """Authenticated → forwards to ``services.projects.list_projects``."""
    expected = [_project_obj(project_a_id, owner=user_a_id)]
    mock_svc = AsyncMock(return_value=expected)
    monkeypatch.setattr("app.services.projects.list_projects", mock_svc)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "list_projects")

    assert isinstance(result, dict)
    assert "projects" in result
    assert len(result["projects"]) == 1
    assert result["projects"][0]["id"] == str(project_a_id)
    assert result["projects"][0]["name"] == "myapp"
    assert "api_key_hash" not in result["projects"][0]  # MUST NOT leak
    # The service was called with our user_id.
    mock_svc.assert_awaited_once()
    _, kwargs = mock_svc.await_args
    args = mock_svc.await_args.args
    assert args[1] == user_a_id  # owner_user_id positional


# ── get_project ─────────────────────────────────────────────────────────────


async def test_get_project_no_token_returns_error(
    fresh_mcp, call_tool, set_auth_token, project_a_id
):
    with set_auth_token(None):
        result = await call_tool(fresh_mcp, "get_project", project_id=str(project_a_id))
    assert isinstance(result, list)
    assert result[0].isError is True


async def test_get_project_invalid_uuid_returns_error(
    fresh_mcp, call_tool, set_auth_token, user_a_id
):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "get_project", project_id="not-a-uuid")
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "must be a UUID" in result[0].text


async def test_get_project_happy_path(
    fresh_mcp, call_tool, set_auth_token, monkeypatch, patch_open_session, user_a_id, project_a_id
):
    """Owner calls get_project → returns project dict."""
    project = _project_obj(project_a_id, owner=user_a_id)
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=project),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "get_project", project_id=str(project_a_id))

    assert isinstance(result, dict)
    assert result["project"]["id"] == str(project_a_id)
    assert result["project"]["name"] == "myapp"


async def test_get_project_cross_user_returns_403(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_b_id,
    project_a_id,
):
    """User B asks for User A's project → service returns None → tool errors."""
    # ``services.projects.get_project`` filters by owner_user_id at the
    # SQL level; if user_b_id doesn't match owner, it returns None.
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=None),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(fresh_mcp, "get_project", project_id=str(project_a_id))

    assert isinstance(result, list)
    assert result[0].isError is True
    assert "not found or you don't have access" in result[0].text


# ── list_event_names ────────────────────────────────────────────────────────


async def test_list_event_names_no_token_returns_error(
    fresh_mcp, call_tool, set_auth_token, project_a_id
):
    with set_auth_token(None):
        result = await call_tool(fresh_mcp, "list_event_names", project_id=str(project_a_id))
    assert isinstance(result, list)
    assert result[0].isError is True


async def test_list_event_names_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_a_id,
    project_a_id,
):
    """Owner authenticated → returns the event-name rows from the analytics service."""
    project = _project_obj(project_a_id, owner=user_a_id)
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=project),
    )
    rows = [
        {"event_name": "page_view", "count": 100, "last_seen": datetime(2026, 1, 1, tzinfo=UTC)},
        {"event_name": "signup", "count": 5, "last_seen": None},
    ]
    monkeypatch.setattr(
        "app.services.analytics.list_event_names",
        AsyncMock(return_value=rows),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "list_event_names", project_id=str(project_a_id))

    assert isinstance(result, dict)
    assert len(result["events"]) == 2
    assert result["events"][0]["event_name"] == "page_view"
    assert result["events"][0]["count"] == 100
    assert result["events"][0]["last_seen"] == "2026-01-01T00:00:00+00:00"
    assert result["events"][1]["last_seen"] is None


async def test_list_event_names_cross_user_returns_403(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_b_id,
    project_a_id,
):
    """Cross-user → ownership check fails BEFORE the analytics service is called."""
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=None),
    )
    analytics_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.services.analytics.list_event_names", analytics_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(fresh_mcp, "list_event_names", project_id=str(project_a_id))

    assert isinstance(result, list)
    assert result[0].isError is True
    # Critical security guarantee: the analytics service was NEVER called.
    analytics_mock.assert_not_awaited()


# ── rotate_api_key ──────────────────────────────────────────────────────────


class _MutableSession:
    """Minimal AsyncSession stand-in for the rotation tool.

    The MCP rotate handler calls ``session.add`` (via write_audit),
    ``session.flush``, ``session.refresh`` and ``session.commit`` —
    we satisfy each as a no-op so the tool body can run without a
    real DB. ``commit_count`` lets a test assert the rotation
    transaction actually ran.
    """

    def __init__(self) -> None:
        self.commit_count = 0

    def add(self, _row):
        return None

    async def flush(self):
        return None

    async def refresh(self, _row):
        return None

    async def commit(self):
        self.commit_count += 1


@pytest.fixture
def rotate_session(monkeypatch):
    """Install a ``_MutableSession`` as the open_session yield value."""
    session = _MutableSession()

    @asynccontextmanager
    async def _fake():
        yield session

    import app.mcp.tools.projects as projects_mod

    monkeypatch.setattr(projects_mod, "open_session", _fake)
    return session


async def test_rotate_api_key_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(fresh_mcp, "rotate_api_key", project_id=str(project_a_id))
    assert isinstance(result, list)
    assert result[0].isError is True


async def test_rotate_api_key_invalid_uuid(fresh_mcp, call_tool, set_auth_token, user_a_id):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "rotate_api_key", project_id="not-a-uuid")
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "must be a UUID" in result[0].text


async def test_rotate_api_key_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    rotate_session,
    user_a_id,
    project_a_id,
):
    """Owner rotates → new key returned, hash changed, audit + commit ran."""
    # Seed a project with a known starting hash.
    project = _project_obj(project_a_id, owner=user_a_id)
    project.api_key_hash = hash_api_key("proj_old_key_value")
    original_hash = project.api_key_hash
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=project),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "rotate_api_key", project_id=str(project_a_id))

    assert isinstance(result, dict)
    new_key = result["api_key"]
    assert isinstance(new_key, str) and new_key
    assert new_key.startswith("proj_")
    assert result["project_id"] == str(project_a_id)
    assert "no longer valid" in result["message"]
    # Hash on the project row was updated to match the new key.
    assert project.api_key_hash != original_hash
    assert project.api_key_hash == hash_api_key(new_key)
    # The transaction committed exactly once.
    assert rotate_session.commit_count == 1


async def test_rotate_api_key_cross_user_403(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    rotate_session,
    user_b_id,
    project_a_id,
):
    """User B rotating user A's project → isError, hash untouched, no commit."""
    project = _project_obj(project_a_id, owner=uuid.UUID("11111111-1111-1111-1111-111111111111"))
    project.api_key_hash = hash_api_key("proj_old_key_value")
    untouched_hash = project.api_key_hash
    # Cross-user lookup goes through services.projects.get_project
    # (which filters by owner_user_id) — returning None simulates the
    # SQL filter rejecting the wrong-owner read.
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=None),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(fresh_mcp, "rotate_api_key", project_id=str(project_a_id))

    assert isinstance(result, list)
    assert result[0].isError is True
    assert "not found or you don't have access" in result[0].text
    # The hash was never modified and no commit was issued.
    assert project.api_key_hash == untouched_hash
    assert rotate_session.commit_count == 0


async def test_rotate_api_key_invalidates_previous(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    rotate_session,
    user_a_id,
    project_a_id,
):
    """After rotation, the previous key's hash no longer matches the project.

    (OSS event-ingestion stack isn't trivially available in this
    test suite — we assert the hash invariant the way ``validate_api_key``
    would: the old key's SHA-256 must no longer equal
    ``project.api_key_hash`` and the new key's SHA-256 must.)
    """
    old_key = "proj_old_key_value_xyz"
    project = _project_obj(project_a_id, owner=user_a_id)
    project.api_key_hash = hash_api_key(old_key)
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=project),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "rotate_api_key", project_id=str(project_a_id))

    assert isinstance(result, dict)
    new_key = result["api_key"]
    # The old key no longer validates against the stored hash.
    assert hash_api_key(old_key) != project.api_key_hash
    # The new key does.
    assert hash_api_key(new_key) == project.api_key_hash


# ── tool list assertion ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_twelve_tools_registered(fresh_mcp):
    """The full v1 tool list must be present after register_all_tools()."""
    tools = await fresh_mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "list_projects",
        "get_project",
        "list_event_names",
        "rotate_api_key",
        "query_events",
        "compare_periods",
        "top_pages",
        "recent_events",
        "verify_integration",
        "get_integration_guide",
        "get_sdk_snippet",
    }
    # The fresh_mcp fixture omits ``whoami`` (only registered in
    # build_fastmcp_server); register_all_tools brings the other eleven.
    assert expected.issubset(names), f"missing: {expected - names}"
