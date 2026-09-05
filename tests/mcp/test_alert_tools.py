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


# ── create_alert ────────────────────────────────────────────────────────────


async def test_create_alert_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(
            fresh_mcp,
            "create_alert",
            project_id=str(project_a_id),
            event_name="x",
            condition="every",
        )
    assert _is_error(result)


async def test_create_alert_cross_user(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    not_owned,
    monkeypatch,
    project_a_id,
    user_b_id,
    mock_session,
):
    svc = AsyncMock()
    monkeypatch.setattr("app.services.alerts.create_alert", svc)
    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(
            fresh_mcp,
            "create_alert",
            project_id=str(project_a_id),
            event_name="x",
            condition="every",
        )
    assert _is_error(result)
    svc.assert_not_awaited()
    mock_session.commit.assert_not_awaited()


async def test_create_alert_every_n_requires_threshold(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
):
    svc = AsyncMock()
    monkeypatch.setattr("app.services.alerts.create_alert", svc)
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "create_alert",
            project_id=str(project_a_id),
            event_name="signup",
            condition="every_n",
        )
    assert _is_error(result)
    assert "threshold_n is required" in result[0].text
    svc.assert_not_awaited()


async def test_create_alert_bad_condition(
    fresh_mcp, call_tool, set_auth_token, patch_open_session, owned, user_a_id, project_a_id
):
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "create_alert",
            project_id=str(project_a_id),
            event_name="signup",
            condition="sometimes",
        )
    assert _is_error(result)
    assert "condition" in result[0].text


async def test_create_alert_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
    mock_session,
):
    aid = uuid.uuid4()
    svc = AsyncMock(
        return_value=_alert_obj(
            aid,
            project_a_id,
            event_name="signup",
            condition=AlertCondition.every_n,
            threshold_n=10,
        )
    )
    monkeypatch.setattr("app.services.alerts.create_alert", svc)
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "create_alert",
            project_id=str(project_a_id),
            event_name="signup",
            condition="every_n",
            threshold_n=10,
        )
    assert result["id"] == str(aid)
    assert result["condition"] == "every_n"
    assert result["threshold_n"] == 10
    svc.assert_awaited_once()
    kwargs = svc.await_args.kwargs
    assert kwargs["project_id"] == project_a_id
    assert kwargs["event_name"] == "signup"
    assert kwargs["condition"] == AlertCondition.every_n
    assert kwargs["threshold_n"] == 10
    mock_session.commit.assert_awaited_once()


# ── set_alert_active ────────────────────────────────────────────────────────


async def test_set_alert_active_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(
            fresh_mcp,
            "set_alert_active",
            project_id=str(project_a_id),
            alert_id=str(uuid.uuid4()),
            is_active=False,
        )
    assert _is_error(result)


async def test_set_alert_active_bad_alert_uuid(
    fresh_mcp, call_tool, set_auth_token, patch_open_session, owned, user_a_id, project_a_id
):
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "set_alert_active",
            project_id=str(project_a_id),
            alert_id="nope",
            is_active=False,
        )
    assert _is_error(result)
    assert "alert_id" in result[0].text


async def test_set_alert_active_cross_user(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    not_owned,
    monkeypatch,
    project_a_id,
    user_b_id,
    mock_session,
):
    svc = AsyncMock()
    monkeypatch.setattr("app.services.alerts.set_alert_active", svc)
    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(
            fresh_mcp,
            "set_alert_active",
            project_id=str(project_a_id),
            alert_id=str(uuid.uuid4()),
            is_active=False,
        )
    assert _is_error(result)
    svc.assert_not_awaited()
    mock_session.commit.assert_not_awaited()


async def test_set_alert_active_not_found(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
    mock_session,
):
    monkeypatch.setattr("app.services.alerts.set_alert_active", AsyncMock(return_value=None))
    aid = uuid.uuid4()
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "set_alert_active",
            project_id=str(project_a_id),
            alert_id=str(aid),
            is_active=False,
        )
    assert _is_error(result)
    assert f"alert {aid} not found" in result[0].text
    mock_session.commit.assert_not_awaited()


async def test_set_alert_active_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
    mock_session,
):
    aid = uuid.uuid4()
    svc = AsyncMock(return_value=_alert_obj(aid, project_a_id, is_active=False))
    monkeypatch.setattr("app.services.alerts.set_alert_active", svc)
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "set_alert_active",
            project_id=str(project_a_id),
            alert_id=str(aid),
            is_active=False,
        )
    assert result["id"] == str(aid)
    assert result["is_active"] is False
    svc.assert_awaited_once()
    assert svc.await_args.args[1:] == (aid, project_a_id)
    assert svc.await_args.kwargs == {"is_active": False}
    mock_session.commit.assert_awaited_once()


# ── delete_alert ────────────────────────────────────────────────────────────


async def test_delete_alert_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(
            fresh_mcp, "delete_alert", project_id=str(project_a_id), alert_id=str(uuid.uuid4())
        )
    assert _is_error(result)


async def test_delete_alert_cross_user(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    not_owned,
    monkeypatch,
    project_a_id,
    user_b_id,
    mock_session,
):
    svc = AsyncMock()
    monkeypatch.setattr("app.services.alerts.delete_alert", svc)
    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(
            fresh_mcp, "delete_alert", project_id=str(project_a_id), alert_id=str(uuid.uuid4())
        )
    assert _is_error(result)
    svc.assert_not_awaited()
    mock_session.commit.assert_not_awaited()


async def test_delete_alert_not_found(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
    mock_session,
):
    monkeypatch.setattr("app.services.alerts.get_alert", AsyncMock(return_value=None))
    audit = AsyncMock()
    monkeypatch.setattr("app.services.audit.write_audit", audit)
    aid = uuid.uuid4()
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp, "delete_alert", project_id=str(project_a_id), alert_id=str(aid)
        )
    assert _is_error(result)
    assert f"alert {aid} not found" in result[0].text
    audit.assert_not_awaited()
    mock_session.commit.assert_not_awaited()


async def test_delete_alert_happy_path_writes_audit(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
    mock_session,
):
    aid = uuid.uuid4()
    monkeypatch.setattr(
        "app.services.alerts.get_alert",
        AsyncMock(return_value=_alert_obj(aid, project_a_id, event_name="signup")),
    )
    delete_svc = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.alerts.delete_alert", delete_svc)
    audit = AsyncMock()
    monkeypatch.setattr("app.services.audit.write_audit", audit)

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp, "delete_alert", project_id=str(project_a_id), alert_id=str(aid)
        )

    assert result == {"deleted": True, "alert_id": str(aid)}
    delete_svc.assert_awaited_once()
    assert delete_svc.await_args.args[1:] == (aid, project_a_id)
    audit.assert_awaited_once()
    akw = audit.await_args.kwargs
    assert akw["user_id"] == user_a_id
    assert akw["action"] == "alert.delete"
    assert akw["target_type"] == "alert"
    assert akw["target_id"] == str(aid)
    assert akw["metadata"] == {
        "project_id": str(project_a_id),
        "event_name": "signup",
        "condition": "every",
        "via": "mcp",
    }
    mock_session.commit.assert_awaited_once()


async def test_create_alert_invalid_project_uuid(fresh_mcp, call_tool, set_auth_token, user_a_id):
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp, "create_alert", project_id="nope", event_name="x", condition="every"
        )
    assert _is_error(result)
    assert "must be a UUID" in result[0].text


async def test_set_alert_active_invalid_project_uuid(
    fresh_mcp, call_tool, set_auth_token, user_a_id
):
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "set_alert_active",
            project_id="nope",
            alert_id=str(uuid.uuid4()),
            is_active=False,
        )
    assert _is_error(result)
    assert "must be a UUID" in result[0].text


async def test_delete_alert_invalid_project_uuid(fresh_mcp, call_tool, set_auth_token, user_a_id):
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp, "delete_alert", project_id="nope", alert_id=str(uuid.uuid4())
        )
    assert _is_error(result)
    assert "must be a UUID" in result[0].text


async def test_create_alert_every_rejects_threshold(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
):
    svc = AsyncMock()
    monkeypatch.setattr("app.services.alerts.create_alert", svc)
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "create_alert",
            project_id=str(project_a_id),
            event_name="signup",
            condition="every",
            threshold_n=5,
        )
    assert _is_error(result)
    assert "must be omitted" in result[0].text
    svc.assert_not_awaited()


async def test_delete_alert_service_false_skips_audit(
    fresh_mcp,
    call_tool,
    set_auth_token,
    patch_open_session,
    owned,
    monkeypatch,
    project_a_id,
    user_a_id,
    mock_session,
):
    aid = uuid.uuid4()
    monkeypatch.setattr(
        "app.services.alerts.get_alert",
        AsyncMock(return_value=_alert_obj(aid, project_a_id, event_name="signup")),
    )
    monkeypatch.setattr("app.services.alerts.delete_alert", AsyncMock(return_value=False))
    audit = AsyncMock()
    monkeypatch.setattr("app.services.audit.write_audit", audit)

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp, "delete_alert", project_id=str(project_a_id), alert_id=str(aid)
        )

    assert _is_error(result)
    audit.assert_not_awaited()
    mock_session.commit.assert_not_awaited()
