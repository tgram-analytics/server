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


# ── top_property_values ───────────────────────────────────────────────────────


async def test_top_property_values_happy_path(
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
        {"value": "too_expensive", "count": 8},
        {"value": "just_looking", "count": 4},
    ]
    top_mock = AsyncMock(return_value=rows)
    monkeypatch.setattr("app.services.analytics.top_properties", top_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "top_property_values",
            project_id=str(project_a_id),
            event_name="abandon_reason",
            property_key="reason",
            period="30d",
            limit=5,
        )

    assert isinstance(result, dict)
    assert result["event_name"] == "abandon_reason"
    assert result["property_key"] == "reason"
    assert result["values"] == rows
    assert result["period"] == "30d"
    kwargs = top_mock.await_args.kwargs
    assert kwargs["event_name"] == "abandon_reason"
    assert kwargs["property_key"] == "reason"
    assert kwargs["limit"] == 5


async def test_top_property_values_missing_key(
    fresh_mcp, call_tool, set_auth_token, user_a_id, project_a_id
):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "top_property_values",
            project_id=str(project_a_id),
            event_name="abandon_reason",
            property_key="",
        )
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "property_key" in result[0].text


async def test_top_property_values_invalid_limit(
    fresh_mcp, call_tool, set_auth_token, user_a_id, project_a_id
):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "top_property_values",
            project_id=str(project_a_id),
            event_name="abandon_reason",
            property_key="reason",
            limit=0,
        )
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "invalid limit" in result[0].text


async def test_top_property_values_cross_user_403(
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
        result = await call_tool(
            fresh_mcp,
            "top_property_values",
            project_id=str(project_a_id),
            event_name="abandon_reason",
            property_key="reason",
        )
    assert isinstance(result, list)
    assert result[0].isError is True
    top_mock.assert_not_awaited()


# ── list_property_keys ────────────────────────────────────────────────────────


async def test_list_property_keys_happy_path(
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
    keys = ["reason", "plan", "discount_shown"]
    keys_mock = AsyncMock(return_value=keys)
    monkeypatch.setattr("app.services.analytics.list_property_keys", keys_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "list_property_keys",
            project_id=str(project_a_id),
            event_name="abandon_reason",
            period="30d",
        )

    assert isinstance(result, dict)
    assert result["event_name"] == "abandon_reason"
    assert result["keys"] == keys
    assert result["period"] == "30d"
    assert keys_mock.await_args.kwargs["event_name"] == "abandon_reason"


async def test_list_property_keys_cross_user_403(
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
    keys_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("app.services.analytics.list_property_keys", keys_mock)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(
            fresh_mcp,
            "list_property_keys",
            project_id=str(project_a_id),
            event_name="abandon_reason",
        )
    assert isinstance(result, list)
    assert result[0].isError is True
    keys_mock.assert_not_awaited()


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


async def test_recent_events_includes_test_events(
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
        {"event_name": "signup", "timestamp": ts, "is_test": True},
        {"event_name": "signup", "timestamp": ts, "is_test": False},
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
    assert [e["is_test"] for e in result["events"]].count(True) == 1


# ── top_taps ────────────────────────────────────────────────────────────────


def _mock_taps_services(monkeypatch, *, total=100, rows=None, grid=None):
    """Patch every app.services.taps query top_taps calls; return the mocks."""
    mocks = {
        "count_taps": AsyncMock(return_value=total),
        "top_elements": AsyncMock(return_value=rows if rows is not None else []),
        "tap_grid": AsyncMock(return_value=grid if grid is not None else []),
        "scroll_depth_median": AsyncMock(return_value=0.62),
        "median_viewport_width": AsyncMock(return_value=390),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"app.services.taps.{name}", mock)
    return mocks


async def test_top_taps_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(fresh_mcp, "top_taps", project_id=str(project_a_id), path="/")
    assert isinstance(result, list)
    assert result[0].isError is True


async def test_top_taps_invalid_device(
    fresh_mcp, call_tool, set_auth_token, monkeypatch, user_a_id, project_a_id
):
    mocks = _mock_taps_services(monkeypatch)
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp, "top_taps", project_id=str(project_a_id), path="/", device="watch"
        )
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "invalid device" in result[0].text
    mocks["count_taps"].assert_not_awaited()


async def test_top_taps_invalid_period(
    fresh_mcp, call_tool, set_auth_token, monkeypatch, user_a_id, project_a_id
):
    mocks = _mock_taps_services(monkeypatch)
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp, "top_taps", project_id=str(project_a_id), path="/", period="bogus"
        )
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "unsupported period" in result[0].text
    mocks["count_taps"].assert_not_awaited()


async def test_top_taps_empty_path(fresh_mcp, call_tool, set_auth_token, user_a_id, project_a_id):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "top_taps", project_id=str(project_a_id), path="")
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "path" in result[0].text


async def test_top_taps_cross_user_does_not_query(
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
    mocks = _mock_taps_services(monkeypatch)
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(fresh_mcp, "top_taps", project_id=str(project_a_id), path="/")
    assert isinstance(result, list)
    assert result[0].isError is True
    for mock in mocks.values():
        mock.assert_not_awaited()


async def test_top_taps_happy_path_shape(
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
        {"label": 'button "Browse albums"', "count": 50},
        {"label": "Menu", "count": 30},
        {"label": None, "count": 20},
    ]
    grid = [[i + j for j in range(10)] for i in range(20)]
    mocks = _mock_taps_services(monkeypatch, total=100, rows=rows, grid=grid)

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "top_taps",
            project_id=str(project_a_id),
            path="/browse",
            period="30d",
            device="mobile",
        )

    assert isinstance(result, dict)
    assert result["path"] == "/browse"
    assert result["device"] == "mobile"
    assert result["period"] == "30d"
    assert result["total_taps"] == 100
    assert [e["label"] for e in result["elements"]] == ['button "Browse albums"', "Menu", None]
    assert [e["pct"] for e in result["elements"]] == [50.0, 30.0, 20.0]
    assert abs(sum(e["pct"] for e in result["elements"]) - 100) < 0.5
    assert result["grid_cols"] == 10
    assert result["grid_row_px"] == 240
    assert len(result["grid"]) == 20
    assert all(len(r) == 10 for r in result["grid"])
    assert result["scroll_depth_median"] == 0.62
    assert result["median_viewport_width"] == 390

    kwargs = mocks["top_elements"].await_args.kwargs
    assert kwargs["project_id"] == project_a_id
    assert kwargs["path"] == "/browse"
    assert kwargs["device"] == "mobile"
    assert kwargs["limit"] == 10
    grid_kwargs = mocks["tap_grid"].await_args.kwargs
    assert (grid_kwargs["cols"], grid_kwargs["row_px"], grid_kwargs["max_rows"]) == (10, 240, 40)


async def test_top_taps_zero_taps_has_zero_pct(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_a_id,
    project_a_id,
):
    project = _project_obj(project_a_id, owner=user_a_id)
    monkeypatch.setattr("app.services.projects.get_project", AsyncMock(return_value=project))
    _mock_taps_services(monkeypatch, total=0)
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(fresh_mcp, "top_taps", project_id=str(project_a_id), path="/")
    assert isinstance(result, dict)
    assert result["total_taps"] == 0
    assert result["elements"] == []
    assert result["grid"] == []
