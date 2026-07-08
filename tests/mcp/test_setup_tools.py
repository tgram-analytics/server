"""Tests for the setup MCP tools.

- ``verify_integration``: counts events received in the last
  ``since_minutes``; returns ``is_receiving=False`` for an empty
  project, ``is_receiving=True`` once events arrive.
- ``get_integration_guide``: fetches the platform README from
  GitHub (Phase 6); we mock the HTTP layer with respx.
- ``get_sdk_snippet``: renders a Jinja init snippet after an
  ownership check (Phase 6).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import respx

from app.mcp.docs import fetch as fetch_mod
from app.mcp.docs.sources import PLATFORM_SOURCES


def _project_obj(pid, owner=None):
    return SimpleNamespace(
        id=pid,
        name="myapp",
        owner_user_id=owner,
        domain_allowlist=[],
        rate_limit_per_second=10,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ── verify_integration ──────────────────────────────────────────────────────


async def test_verify_integration_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(fresh_mcp, "verify_integration", project_id=str(project_a_id))
    assert isinstance(result, list)
    assert result[0].isError is True


async def test_verify_integration_zero_events(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_a_id,
    project_a_id,
):
    """Fresh project → ``count = 0`` → ``is_receiving = False``."""
    project = _project_obj(project_a_id, owner=user_a_id)
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=project),
    )
    monkeypatch.setattr(
        "app.services.analytics.list_recent_events",
        AsyncMock(return_value=[]),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "verify_integration",
            project_id=str(project_a_id),
            since_minutes=30,
        )

    assert isinstance(result, dict)
    assert result["count"] == 0
    assert result["is_receiving"] is False
    assert result["since_minutes"] == 30


async def test_verify_integration_with_events(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_a_id,
    project_a_id,
):
    """Recent events in window → ``count > 0`` → ``is_receiving = True``.

    We seed two rows: one inside the window (counted) and one outside
    (must be filtered out).
    """
    project = _project_obj(project_a_id, owner=user_a_id)
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=project),
    )
    now = datetime.now(UTC)
    rows = [
        {"event_name": "page_view", "timestamp": now - timedelta(minutes=5)},
        {"event_name": "page_view", "timestamp": now - timedelta(minutes=2)},
        # Outside the 30-min window:
        {"event_name": "page_view", "timestamp": now - timedelta(hours=2)},
    ]
    monkeypatch.setattr(
        "app.services.analytics.list_recent_events",
        AsyncMock(return_value=rows),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "verify_integration",
            project_id=str(project_a_id),
            since_minutes=30,
        )

    assert isinstance(result, dict)
    assert result["count"] == 2
    assert result["is_receiving"] is True


async def test_verify_integration_invalid_since_minutes(
    fresh_mcp, call_tool, set_auth_token, user_a_id, project_a_id
):
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "verify_integration",
            project_id=str(project_a_id),
            since_minutes=99999,
        )
    assert isinstance(result, list)
    assert result[0].isError is True


async def test_verify_integration_cross_user_403(
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
        result = await call_tool(fresh_mcp, "verify_integration", project_id=str(project_a_id))
    assert isinstance(result, list)
    assert result[0].isError is True
    rec_mock.assert_not_awaited()


# ── get_integration_guide (Phase 6) ─────────────────────────────────────────


async def test_get_integration_guide_happy_path(fresh_mcp, call_tool):
    """Valid platform + 200 GitHub response → dict with markdown body."""
    fetch_mod._CACHE.clear()
    fetch_mod._STALE.clear()
    url = PLATFORM_SOURCES["js"]
    with respx.mock() as router:
        router.get(url).mock(
            return_value=httpx.Response(200, text="# JS SDK\nhi", headers={"ETag": '"sha-1"'})
        )
        result = await call_tool(fresh_mcp, "get_integration_guide", platform="js")
    assert isinstance(result, dict)
    assert result["markdown"].startswith("# JS SDK")
    assert result["commit_sha"] == "sha-1"
    assert result["stale"] is False


async def test_get_integration_guide_validates_platform(fresh_mcp, call_tool):
    result = await call_tool(fresh_mcp, "get_integration_guide", platform="cobol")
    assert isinstance(result, list)
    assert result[0].isError is True
    assert "invalid platform" in result[0].text


async def test_get_integration_guide_http_error_is_translated(fresh_mcp, call_tool):
    """A live fetch failure with no cached copy → ``isError=True``."""
    fetch_mod._CACHE.clear()
    fetch_mod._STALE.clear()
    url = PLATFORM_SOURCES["python"]
    with respx.mock() as router:
        router.get(url).mock(return_value=httpx.Response(500, text="boom"))
        result = await call_tool(fresh_mcp, "get_integration_guide", platform="python")
    assert isinstance(result, list)
    assert result[0].isError is True


# ── get_sdk_snippet (Phase 6) ───────────────────────────────────────────────


async def test_get_sdk_snippet_happy_path(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_a_id,
    project_a_id,
):
    """Owner gets a rendered snippet for the requested platform."""
    project = _project_obj(project_a_id, owner=user_a_id)
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=project),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "get_sdk_snippet",
            platform="python",
            project_id=str(project_a_id),
        )
    assert isinstance(result, dict)
    assert result["platform"] == "python"
    # Real init pattern — not a stub message.
    assert "TGA(" in result["snippet"]


async def test_get_sdk_snippet_no_token(fresh_mcp, call_tool, set_auth_token, project_a_id):
    with set_auth_token(None):
        result = await call_tool(
            fresh_mcp,
            "get_sdk_snippet",
            platform="js",
            project_id=str(project_a_id),
        )
    assert isinstance(result, list)
    assert result[0].isError is True


async def test_get_sdk_snippet_validates_inputs(
    fresh_mcp, call_tool, set_auth_token, user_a_id, project_a_id
):
    """Bad platform AND bad project_id both produce isError=True."""
    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_a_id)):
        bad_platform = await call_tool(
            fresh_mcp,
            "get_sdk_snippet",
            platform="cobol",
            project_id=str(project_a_id),
        )
        assert isinstance(bad_platform, list) and bad_platform[0].isError is True

        bad_uuid = await call_tool(
            fresh_mcp,
            "get_sdk_snippet",
            platform="python",
            project_id="not-a-uuid",
        )
        assert isinstance(bad_uuid, list) and bad_uuid[0].isError is True


async def test_get_sdk_snippet_with_explicit_key(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_a_id,
    project_a_id,
):
    """Passing ``api_key=`` splices that value into the rendered snippet.

    The placeholder ``<YOUR_API_KEY>`` must be absent — clients pass
    the freshly rotated key in via this argument and expect the
    snippet to be immediately runnable.
    """
    project = _project_obj(project_a_id, owner=user_a_id)
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=project),
    )

    from tests.mcp.conftest import _make_token

    explicit = "tga_explicit_xyz"
    with set_auth_token(_make_token(user_a_id)):
        result = await call_tool(
            fresh_mcp,
            "get_sdk_snippet",
            platform="python",
            project_id=str(project_a_id),
            api_key=explicit,
        )

    assert isinstance(result, dict)
    assert result["platform"] == "python"
    assert explicit in result["snippet"]
    assert "<YOUR_API_KEY>" not in result["snippet"]


async def test_get_sdk_snippet_cross_user_403(
    fresh_mcp,
    call_tool,
    set_auth_token,
    monkeypatch,
    patch_open_session,
    user_b_id,
    project_a_id,
):
    """User B asking for project A's snippet must get an error, not a snippet.

    This is the security-critical assertion: without the ownership
    check inside ``render_snippet``, a token holder could pull
    project-scoped snippets for any project_id.
    """
    monkeypatch.setattr(
        "app.services.projects.get_project",
        AsyncMock(return_value=None),
    )

    from tests.mcp.conftest import _make_token

    with set_auth_token(_make_token(user_b_id)):
        result = await call_tool(
            fresh_mcp,
            "get_sdk_snippet",
            platform="js",
            project_id=str(project_a_id),
        )
    assert isinstance(result, list)
    assert result[0].isError is True
