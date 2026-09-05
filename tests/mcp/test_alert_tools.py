"""Tests for the alert MCP tools.

Same boundaries as ``test_projects_tools.py``: no token → error part;
cross-user → error part (ownership check runs before any service);
happy path forwards to ``app.services.alerts`` with the right kwargs.
Services are mocked; ownership is mocked via ``assert_project_owned_by``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent

from app.mcp.auth import ProjectNotOwnedError
from app.models.alert import AlertCondition
from tests.mcp.conftest import _make_token


def _alert_obj(
    aid,
    pid,
    *,
    event_name="purchase",
    condition=AlertCondition.every,
    threshold_n=None,
    is_active=True,
):
    return SimpleNamespace(
        id=aid,
        project_id=pid,
        event_name=event_name,
        condition=condition,
        threshold_n=threshold_n,
        counter=0,
        is_active=is_active,
        muted_until=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _delivery_obj(did, aid, pid, *, delivered=True, error=None):
    return SimpleNamespace(
        id=did,
        alert_id=aid,
        project_id=pid,
        event_name="purchase",
        condition=AlertCondition.every,
        threshold_n=None,
        fired_at=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        delivered=delivered,
        error=error,
    )


@pytest.fixture
def owned(monkeypatch, project_a_id, user_a_id):
    """Make ``assert_project_owned_by`` succeed and return a project stub."""
    project = SimpleNamespace(id=project_a_id, name="myapp", owner_user_id=user_a_id)
    mock = AsyncMock(return_value=project)
    monkeypatch.setattr("app.mcp.tools.alerts.assert_project_owned_by", mock)
    return mock


@pytest.fixture
def not_owned(monkeypatch):
    mock = AsyncMock(side_effect=ProjectNotOwnedError())
    monkeypatch.setattr("app.mcp.tools.alerts.assert_project_owned_by", mock)
    return mock


def _is_error(result) -> bool:
    return (
        isinstance(result, list)
        and isinstance(result[0], TextContent)
        and result[0].isError is True
    )


# ── list_alerts ─────────────────────────────────────────────────────────────


async def test_list_alerts_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(fresh_mcp, "list_alerts", project_id=str(project_a_id))
    assert _is_error(result)
    assert "not authenticated" in result[0].text


async def test_list_alerts_invalid_uuid(fresh_mcp, call_tool, set_auth_token, user_a_id):
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "list_alerts", project_id="nope")
    assert _is_error(result)
    assert "must be a UUID" in result[0].text


async def test_list_alerts_cross_user(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    not_owned,
    monkeypatch,
    project_a_id,
    user_b_id,
):
    svc = AsyncMock(return_value=[])
    monkeypatch.setattr("app.services.alerts.list_alerts", svc)
    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(fresh_mcp, "list_alerts", project_id=str(project_a_id))
    assert _is_error(result)
    svc.assert_not_awaited()


async def test_list_alerts_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
):
    aid = uuid.uuid4()
    svc = AsyncMock(
        return_value=[
            _alert_obj(aid, project_a_id, condition=AlertCondition.every_n, threshold_n=5)
        ]
    )
    monkeypatch.setattr("app.services.alerts.list_alerts", svc)
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "list_alerts", project_id=str(project_a_id))
    assert isinstance(result, dict)
    assert result["alerts"] == [
        {
            "id": str(aid),
            "project_id": str(project_a_id),
            "event_name": "purchase",
            "condition": "every_n",
            "threshold_n": 5,
            "counter": 0,
            "is_active": True,
            "muted_until": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    svc.assert_awaited_once()
    assert svc.await_args.args[1] == project_a_id


# ── alert_history ───────────────────────────────────────────────────────────


async def test_alert_history_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(fresh_mcp, "alert_history", project_id=str(project_a_id))
    assert _is_error(result)


async def test_alert_history_bad_period(
    fresh_mcp, call_tool, set_auth_token, patch_open_session, owned, user_a_id, project_a_id
):
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp, "alert_history", project_id=str(project_a_id), period="2w"
        )
    assert _is_error(result)
    assert "unsupported period" in result[0].text


async def test_alert_history_bad_limit(
    fresh_mcp, call_tool, set_auth_token, patch_open_session, owned, user_a_id, project_a_id
):
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "alert_history", project_id=str(project_a_id), limit=0)
    assert _is_error(result)
    assert "limit" in result[0].text


async def test_alert_history_cross_user(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    not_owned,
    monkeypatch,
    project_a_id,
    user_b_id,
):
    svc = AsyncMock(return_value=[])
    monkeypatch.setattr("app.services.alerts.list_deliveries", svc)
    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(fresh_mcp, "alert_history", project_id=str(project_a_id))
    assert _is_error(result)
    svc.assert_not_awaited()


async def test_alert_history_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
):
    did, aid = uuid.uuid4(), uuid.uuid4()
    svc = AsyncMock(
        return_value=[_delivery_obj(did, aid, project_a_id, delivered=False, error="TimedOut")]
    )
    monkeypatch.setattr("app.services.alerts.list_deliveries", svc)
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "alert_history",
            project_id=str(project_a_id),
            period="30d",
            limit=5,
            event_name="purchase",
        )
    assert result["project_id"] == str(project_a_id)
    assert result["period"] == "30d"
    assert result["rows"] == [
        {
            "id": str(did),
            "alert_id": str(aid),
            "event_name": "purchase",
            "condition": "every",
            "threshold_n": None,
            "fired_at": "2026-09-05T12:00:00+00:00",
            "delivered": False,
            "error": "TimedOut",
        }
    ]
    svc.assert_awaited_once()
    kwargs = svc.await_args.kwargs
    assert svc.await_args.args[1] == project_a_id
    assert kwargs["limit"] == 5
    assert kwargs["event_name"] == "purchase"
    assert (datetime.now(UTC) - kwargs["since"]).days in (29, 30)
