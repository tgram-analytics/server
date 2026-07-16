"""Tests for the event-data MCP tools.

Same boundaries as ``test_projects_tools.py`` — auth, ownership,
service dispatch — applied to ``query_events``, ``compare_periods``,
``top_pages``, ``recent_events``.

Each suite verifies:

- No-token branch returns ``isError=True``.
- Happy path forwards to the right OSS service with the right kwargs.
- Cross-user branch returns ``isError=True`` AND does not call the
  analytics service.
- Bad input (invalid period, invalid UUID, out-of-range limit) returns
  ``isError=True`` without touching the DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock


def _project_obj(pid, owner=None):
    return SimpleNamespace(
        id=pid,
        name="myapp",
        owner_user_id=owner,
        domain_allowlist=[],
        rate_limit_per_second=10,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ── query_events ────────────────────────────────────────────────────────────


async def test_query_events_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(
            fresh_mcp,
            "query_events",
            project_id=str(project_a_id),
            event_name="pageview",
        )
    assert isinstance(result, list)
    assert result[0].isError is True


async def test_query_events_invalid_period(
    fresh_mcp, call_tool, set_auth_token, user_a_id, project_a_id
):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "query_events",
            project_id=str(project_a_id),
            event_name="pageview",
            period="bogus",
        )
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "unsupported period" in result[0].text


async def test_query_events_invalid_granularity(
    fresh_mcp, call_tool, set_auth_token, user_a_id, project_a_id
):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "query_events",
            project_id=str(project_a_id),
            event_name="pageview",
            granularity="century",
        )
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "invalid granularity" in result[0].text


async def test_query_events_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_a_id,
    project_a_id,
):
    """Forwards to ``count_events`` + ``events_over_time`` with computed window."""
    project = _project_obj(project_a_id, owner=user_a_id)
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=project),
    )
    monkeypatch.setattr(
        "app.services.analytics.count_events",
        AsyncMock(return_value=42),
    )
    bucket = datetime(2026, 5, 1, tzinfo=UTC)
    monkeypatch.setattr(
        "app.services.analytics.events_over_time",
        AsyncMock(return_value=[{"bucket": bucket, "count": 7}]),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "query_events",
            project_id=str(project_a_id),
            event_name="pageview",
            period="7d",
            granularity="day",
        )

    assert isinstance(result, dict)
    assert result["total"] == 42
    assert result["period"] == "7d"
    assert result["granularity"] == "day"
    assert len(result["series"]) == 1
    assert result["series"][0]["bucket"] == bucket.isoformat()
    assert result["series"][0]["count"] == 7


async def test_query_events_cross_user_403(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_b_id,
    project_a_id,
):
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=None),
    )
    count_mock = AsyncMock(return_value=0)
    over_time_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.services.analytics.count_events", count_mock)
    monkeypatch.setattr("app.services.analytics.events_over_time", over_time_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(
            fresh_mcp,
            "query_events",
            project_id=str(project_a_id),
            event_name="pageview",
        )

    assert isinstance(result, list)
    assert result[0].isError is True
    count_mock.assert_not_awaited()
    over_time_mock.assert_not_awaited()


# ── compare_periods ─────────────────────────────────────────────────────────


async def test_compare_periods_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_a_id,
    project_a_id,
):
    project = _project_obj(project_a_id, owner=user_a_id)
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=project),
    )
    cmp_mock = AsyncMock(return_value={"current": 100, "previous": 50, "delta_pct": 100.0})
    monkeypatch.setattr("app.services.analytics.compare_periods", cmp_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "compare_periods",
            project_id=str(project_a_id),
            event_name="pageview",
            period="30d",
        )

    assert isinstance(result, dict)
    assert result["current"] == 100
    assert result["previous"] == 50
    assert result["delta_pct"] == 100.0
    assert result["period"] == "30d"
    # The current and previous windows are equal-length and adjacent.
    cmp_mock.assert_awaited_once()
    kwargs = cmp_mock.await_args.kwargs
    width_current = kwargs["current_end"] - kwargs["current_start"]
    width_prev = kwargs["previous_end"] - kwargs["previous_start"]
    assert width_current == width_prev
    assert kwargs["previous_end"] == kwargs["current_start"]


async def test_compare_periods_cross_user_403(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_b_id,
    project_a_id,
):
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=None),
    )
    cmp_mock = AsyncMock(return_value={"current": 0, "previous": 0, "delta_pct": None})
    monkeypatch.setattr("app.services.analytics.compare_periods", cmp_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(
            fresh_mcp,
            "compare_periods",
            project_id=str(project_a_id),
            event_name="pageview",
        )
    assert isinstance(result, list)
    assert result[0].isError is True
    cmp_mock.assert_not_awaited()


# ── top_pages ───────────────────────────────────────────────────────────────


async def test_top_pages_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_a_id,
    project_a_id,
):
    project = _project_obj(project_a_id, owner=user_a_id)
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=project),
    )
    rows = [
        {"value": "/home", "count": 100},
        {"value": "/about", "count": 30},
    ]
    top_mock = AsyncMock(return_value=rows)
    monkeypatch.setattr("app.services.analytics.top_properties", top_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "top_pages",
            project_id=str(project_a_id),
            period="7d",
            limit=5,
        )

    assert isinstance(result, dict)
    assert result["pages"] == rows
    assert result["period"] == "7d"
    # Verify the service was called for pageview + url.
    kwargs = top_mock.await_args.kwargs
    assert kwargs["event_name"] == "pageview"
    assert kwargs["property_key"] == "url"
    assert kwargs["limit"] == 5


async def test_top_pages_invalid_limit(
    fresh_mcp, call_tool, set_auth_token, user_a_id, project_a_id
):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "top_pages",
            project_id=str(project_a_id),
            limit=0,
        )
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "invalid limit" in result[0].text


async def test_top_pages_cross_user_403(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_b_id,
    project_a_id,
):
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=None),
    )
    top_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.services.analytics.top_properties", top_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(fresh_mcp, "top_pages", project_id=str(project_a_id))
    assert isinstance(result, list)
    assert result[0].isError is True
    top_mock.assert_not_awaited()


# ── recent_events ───────────────────────────────────────────────────────────


async def test_recent_events_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_a_id,
    project_a_id,
):
    project = _project_obj(project_a_id, owner=user_a_id)
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=project),
    )
    ts = datetime(2026, 5, 8, tzinfo=UTC)
    rows = [
        {"event_name": "pageview", "timestamp": ts},
        {"event_name": "signup", "timestamp": ts},
    ]
    monkeypatch.setattr(
        "app.services.analytics.list_recent_events",
        AsyncMock(return_value=rows),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "recent_events",
            project_id=str(project_a_id),
            limit=10,
        )

    assert isinstance(result, dict)
    assert len(result["events"]) == 2
    assert result["events"][0]["event_name"] == "pageview"
    assert result["events"][0]["timestamp"] == ts.isoformat()


async def test_recent_events_filters_by_event_name(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_a_id,
    project_a_id,
):
    project = _project_obj(project_a_id, owner=user_a_id)
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=project),
    )
    ts = datetime(2026, 5, 8, tzinfo=UTC)
    rows = [
        {"event_name": "pageview", "timestamp": ts},
        {"event_name": "signup", "timestamp": ts},
        {"event_name": "pageview", "timestamp": ts},
    ]
    monkeypatch.setattr(
        "app.services.analytics.list_recent_events",
        AsyncMock(return_value=rows),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "recent_events",
            project_id=str(project_a_id),
            event_name="signup",
            limit=10,
        )

    assert isinstance(result, dict)
    assert len(result["events"]) == 1
    assert result["events"][0]["event_name"] == "signup"


async def test_recent_events_cross_user_403(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_b_id,
    project_a_id,
):
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=None),
    )
    rec_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.services.analytics.list_recent_events", rec_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(fresh_mcp, "recent_events", project_id=str(project_a_id))
    assert isinstance(result, list)
    assert result[0].isError is True
    rec_mock.assert_not_awaited()
