"""Integration / setup MCP tools.

Three handlers:

- ``verify_integration`` — counts events received in the last
  ``since_minutes`` window so MCP clients can confirm that an SDK
  install is reaching the server.
- ``get_integration_guide`` — fetches the platform's README from the
  public GitHub repo (TTL-cached, stale-on-error fallback). Phase 6.
- ``get_sdk_snippet`` — renders a copy-paste init snippet from a
  Jinja template after enforcing project ownership. Phase 6.

The two docs tools accept the platform names ``js``, ``python``,
``flutter`` (plus ``server`` for the integration guide); see
:mod:`app.mcp.docs.sources` for the URL map.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ToolAnnotations

from app.mcp.auth import (
    MCPAccessToken,
    ProjectNotOwnedError,
    assert_project_owned_by,
)
from app.mcp.docs.fetch import fetch_repo_readme
from app.mcp.docs.render import render_snippet
from app.mcp.tools._schemas import (
    IntegrationGuideResult,
    SDKSnippetResult,
    VerifyIntegrationResult,
)
from app.mcp.tools._session import open_session

logger = logging.getLogger("app.mcp.tools")

# N3 — read-only annotations for setup helpers that only read from our DB.
_READ_ONLY_LOCAL = ToolAnnotations(
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
)

# ``get_integration_guide`` fetches a README from GitHub — by definition an
# open-world fact source whose contents we don't control. The ``openWorldHint``
# flips True so clients know responses may shift with upstream pushes.
_READ_ONLY_OPEN_WORLD = ToolAnnotations(
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=True,
)

# Supported docs platforms. ``server`` is valid for the integration
# guide (people self-hosting want the server README) but not for the
# snippet renderer (no init pattern for the server itself).
_SUPPORTED_PLATFORMS: tuple[str, ...] = ("js", "python", "flutter")
_SUPPORTED_GUIDE_PLATFORMS: tuple[str, ...] = ("js", "python", "flutter", "server")


def _not_authenticated() -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text="not authenticated",
            isError=True,  # type: ignore[call-arg]
        )
    ]


def _not_owned_error(project_id: str) -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text=f"project {project_id} not found or you don't have access",
            isError=True,  # type: ignore[call-arg]
        )
    ]


def _bad_input(msg: str) -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text=msg,
            isError=True,  # type: ignore[call-arg]
        )
    ]


def _stub(text: str) -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text=text,
            isError=True,  # type: ignore[call-arg]
        )
    ]


def register_setup_tools(mcp: FastMCP) -> None:
    """Register ``verify_integration`` (real) + the two Phase-6 stubs."""

    @mcp.tool(title="Verify integration", annotations=_READ_ONLY_LOCAL)
    async def verify_integration(
        project_id: str,
        since_minutes: int = 30,
    ) -> list[TextContent] | VerifyIntegrationResult:
        """Return the count of events received in the last *since_minutes*.

        Useful right after an SDK install: clients can poll this to
        confirm that events are flowing. Counts ALL event names — we
        don't filter by name here because new integrations don't
        necessarily fire the same name as their first ping.

        Response: ``{"count": int, "since_minutes": int, "is_receiving": bool}``.
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()

        try:
            pid = uuid.UUID(project_id)
        except (ValueError, AttributeError):
            return _bad_input(f"invalid project_id {project_id!r}; must be a UUID")

        if not isinstance(since_minutes, int) or since_minutes <= 0 or since_minutes > 1440:
            return _bad_input(f"invalid since_minutes {since_minutes!r}; must be 1..1440 (24h max)")

        owner_user_id = uuid.UUID(token.extra["user_id"])
        end = datetime.now(UTC)
        start = end - timedelta(minutes=since_minutes)

        # ``count_events`` requires an event name. For verify_integration
        # we want a project-wide count, so we use ``list_recent_events``
        # with a generous limit and filter to the time window. For the
        # v1 surface this is acceptable — projects with thousands of
        # events per minute are not in the free tier — and avoids
        # adding a dedicated service signature solely for this tool.
        from app.services.analytics import list_recent_events

        async with open_session() as session:
            try:
                await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)

            rows = await list_recent_events(session, project_id=pid, limit=1000)

        in_window = [r for r in rows if r["timestamp"] is not None and r["timestamp"] >= start]
        count = len(in_window)
        return VerifyIntegrationResult(
            count=count,
            since_minutes=since_minutes,
            is_receiving=count > 0,
        )

    @mcp.tool(title="Get integration guide", annotations=_READ_ONLY_OPEN_WORLD)
    async def get_integration_guide(
        platform: str,
    ) -> list[TextContent] | IntegrationGuideResult:
        """Return the platform's README fetched from the public GitHub repo.

        Response shape:

        - ``markdown`` — full README body
        - ``source_url`` — the URL we fetched from
        - ``commit_sha`` — ``ETag`` (blob OID) or ``""`` if the upstream
          didn't send one (we never fabricate)
        - ``fetched_at`` — ISO-8601 fetch timestamp
        - ``stale`` — ``True`` if served from a degraded fallback after
          a live fetch failed

        The ``mcp_github_token`` setting, when set, raises the GitHub
        rate limit on the underlying request.
        """
        if platform not in _SUPPORTED_GUIDE_PLATFORMS:
            return _bad_input(
                f"invalid platform {platform!r}; expected one of {list(_SUPPORTED_GUIDE_PLATFORMS)}"
            )

        # Optional GitHub token raises the raw.githubusercontent.com rate limit.
        github_token: str | None = None
        try:
            from app.core.config import get_settings

            github_token = getattr(get_settings(), "mcp_github_token", None)
        except Exception:
            github_token = None

        try:
            payload = await fetch_repo_readme(platform, github_token=github_token)
        except KeyError:
            # Defensive — _SUPPORTED_GUIDE_PLATFORMS should match
            # PLATFORM_SOURCES, but if a future PR splits them by
            # accident we still want a clean error rather than a 500.
            return _bad_input(f"unknown platform {platform!r}")
        except httpx.HTTPError as exc:
            logger.warning("get_integration_guide(%s) fetch failed: %s", platform, exc)
            return _stub(f"could not fetch integration guide for {platform!r}: {exc}")
        return IntegrationGuideResult(
            markdown=payload["markdown"],
            source_url=payload["source_url"],
            commit_sha=payload["commit_sha"],
            fetched_at=payload["fetched_at"],
            stale=payload["stale"],
        )

    @mcp.tool(title="Get SDK snippet", annotations=_READ_ONLY_LOCAL)
    async def get_sdk_snippet(
        platform: str,
        project_id: str,
        api_key: str | None = None,
    ) -> list[TextContent] | SDKSnippetResult:
        """Return a copy-paste init snippet for *platform*.

        Enforces project ownership before rendering — without this
        check a token holder could request a snippet against any
        ``project_id``.

        Key handling:

        - When *api_key* is omitted, the snippet ships with the
          ``<YOUR_API_KEY>`` placeholder. The OSS schema only persists
          ``api_key_hash``, so the real key cannot be recovered from
          storage — callers fill the placeholder by hand.
        - When *api_key* is provided, it is spliced verbatim into the
          template. The typical flow: call ``rotate_api_key`` first to
          mint a fresh plaintext key, then pass that string here so the
          rendered snippet is immediately runnable. We do **not**
          validate the key against the project — splicing is pure
          template substitution.

        Response shape: ``{"snippet": str, "platform": str}``.
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()

        if platform not in _SUPPORTED_PLATFORMS:
            return _bad_input(
                f"invalid platform {platform!r}; expected one of {list(_SUPPORTED_PLATFORMS)}"
            )
        try:
            pid = uuid.UUID(project_id)
        except (ValueError, AttributeError):
            return _bad_input(f"invalid project_id {project_id!r}; must be a UUID")

        owner_user_id = uuid.UUID(token.extra["user_id"])
        async with open_session() as session:
            try:
                rendered = await render_snippet(
                    session, platform, pid, owner_user_id, api_key=api_key
                )
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)
            except KeyError:
                return _bad_input(f"unknown platform {platform!r}")
        return SDKSnippetResult(snippet=rendered, platform=platform)
