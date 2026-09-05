"""FastMCP server build + ASGI app wiring for the cloud MCP surface.

Two entry points:

- :func:`build_fastmcp_server` — returns the configured :class:`FastMCP`
  instance with the v1 tools registered. Phase 4 ships a single ``whoami``
  tool (sanity check that auth + tool dispatch round-trips end-to-end);
  Phase 5 will register the remaining ten.
- :func:`build_mcp_asgi_app` — returns ``(asgi_app, lifespan)``. The OSS
  ``register_http_router`` hook mounts ``asgi_app`` at ``/mcp`` and
  composes ``lifespan`` into its ``AsyncExitStack`` so FastMCP's session
  manager is started/stopped with the parent app.

Anti-pattern guards (verified by tests):

- ``streamable_http_path="/"`` is required because the OSS hook mounts the
  app *at* ``/mcp``. The FastMCP default of ``/mcp`` would surface the
  endpoints at ``/mcp/mcp`` (path collision / 404).
- Tool handlers never raise; the no-token branch of ``whoami`` returns a
  ``TextContent(isError=True)`` so the SDK signals the error correctly
  to the MCP client.
- We pass ``token_verifier`` + ``auth=AuthSettings(...)`` so FastMCP's
  ``streamable_http_app`` installs the bearer-auth middleware stack
  (``BearerAuthBackend``, ``AuthContextMiddleware``,
  ``RequireAuthMiddleware``) automatically. Reaching into the middleware
  list manually is fragile and version-coupled.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent, ToolAnnotations

from app.extensions import get_mcp_whoami_extras
from app.mcp.auth import MCPAccessToken
from app.mcp.tools import register_all_tools

logger = logging.getLogger("app.mcp")


# ─── N2 — Origin header validation middleware ────────────────────────────────

# Type alias for the ASGI message dict; using ``Any`` keeps mypy happy without
# pulling in a starlette dependency just for typing.
_ASGIScope = dict[str, Any]
_ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
_ASGISend = Callable[[dict[str, Any]], Awaitable[None]]


def _build_origin_middleware(
    app: Callable[..., Awaitable[None]],
    allowed_origins: Iterable[str],
) -> Callable[..., Awaitable[None]]:
    """Pure-ASGI middleware that rejects mismatched browser Origins.

    Per the MCP spec (2025-11-25) §"Security best practices", the
    streamable-HTTP endpoint MUST validate the ``Origin`` header on every
    request to prevent DNS rebinding attacks against MCP clients.

    - Missing Origin → pass through (CLI clients legitimately omit it; no
      browser sends a request without Origin, so absence is a strong signal
      the request isn't browser-originated).
    - Origin in allowlist → pass through.
    - Otherwise → 403 with a JSON body so the caller can introspect why.

    Implemented as raw ASGI (rather than Starlette ``BaseHTTPMiddleware``)
    so it wraps the FastMCP-built Starlette app without forcing every
    request through ``BaseHTTPMiddleware``'s extra task — important since
    the underlying app uses ``StreamingResponse`` (server-sent events) and
    ``BaseHTTPMiddleware`` is known to break SSE.
    """
    allowed = frozenset(allowed_origins)

    async def _middleware(scope: _ASGIScope, receive: _ASGIReceive, send: _ASGISend) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        origin = _header_value(scope, b"origin")
        if origin is None or origin in allowed:
            await app(scope, receive, send)
            return

        body = json.dumps(
            {
                "error": "origin_not_allowed",
                "error_description": ("Origin header is not on the MCP server's allowlist"),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return _middleware


def _header_value(scope: _ASGIScope, name: bytes) -> str | None:
    """Return decoded header value or ``None``. Case-insensitive on the name."""
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for header_name, value in headers:
        if header_name.lower() == name:
            try:
                return value.decode("latin-1")
            except UnicodeDecodeError:  # pragma: no cover - defensive
                return None
    return None


def _build_transport_security(settings: Any) -> TransportSecuritySettings:
    """Allow-list our public host for the MCP SDK's DNS-rebinding protection.

    FastMCP auto-enables that protection with a *localhost-only* host list when
    it binds to 127.0.0.1 (our case behind Coolify's proxy), so a request with
    the real ``Host: mcp.tgram-analytics.com`` is rejected with 421 "Invalid
    Host header". We configure the allow-list for the deployment's public host
    while keeping loopback entries so local/dev and the e2e suite still work.
    Origins mirror the N2 Origin middleware's allow-list so the two layers agree.
    """
    public_url = settings.mcp_effective_public_url.rstrip("/")
    public_host = urlparse(public_url).netloc
    configured_origins = getattr(settings, "mcp_allowed_origins", None) or []
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            public_host,
            f"{public_host}:*",
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ],
        allowed_origins=[
            public_url,
            *configured_origins,
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    )


def build_fastmcp_server(
    *,
    token_verifier: Any,
    public_url: str,
    canonical_resource_uri: str | None = None,
    transport_security: TransportSecuritySettings | None = None,
) -> FastMCP:
    """Build the FastMCP instance with the v1 toolset registered.

    Parameters
    ----------
    token_verifier:
        The ``mcp.server.auth.provider.TokenVerifier`` used to
        authenticate bearer tokens. OSS passes
        :class:`app.mcp.auth.StaticTokenVerifier`; the cloud overlay
        supplies its OAuth JWT verifier via
        ``app.extensions.register_mcp_token_verifier``.
    public_url:
        Externally-reachable base URL of the OAuth authorization server
        (e.g. ``https://mcp.tgram-analytics.com``). Becomes ``issuer_url``
        — the JWT ``iss`` claim and the OAuth AS metadata advertise this.
    canonical_resource_uri:
        RFC 8707 canonical resource URI for the protected resource
        (e.g. ``https://mcp.tgram-analytics.com/mcp``). Becomes
        ``resource_server_url`` — the protected-resource metadata
        ``resource`` field and the JWT ``aud`` claim. Defaults to
        ``f"{public_url}/mcp"`` for back-compat with older callers.
    """
    if canonical_resource_uri is None:
        canonical_resource_uri = public_url.rstrip("/") + "/mcp"

    auth_settings = AuthSettings(
        issuer_url=public_url,  # type: ignore[arg-type]
        resource_server_url=canonical_resource_uri,  # type: ignore[arg-type]
        required_scopes=["mcp:tools"],
    )

    mcp = FastMCP(
        name="tgram-analytics-mcp",
        instructions=(
            "tgram-analytics MCP server. Authenticated tools let you query "
            "your projects, events, and integration status, and manage "
            "Telegram alerts (list, create, pause, delete) and read the "
            "history of alerts sent."
        ),
        token_verifier=token_verifier,
        auth=auth_settings,
        # Allow-list our public Host for the SDK's DNS-rebinding protection;
        # without it FastMCP defaults to a localhost-only list and 421s the
        # real ``Host: mcp.tgram-analytics.com``.
        transport_security=transport_security,
        # Mount-aware: the OSS register_http_router hook mounts this ASGI
        # app at /mcp, so the inner streamable-http handler must live at
        # the mount root, not at the SDK default "/mcp".
        streamable_http_path="/",
        stateless_http=True,
    )

    @mcp.tool(
        title="Who am I",
        annotations=ToolAnnotations(
            title="Who am I",
            readOnlyHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def whoami() -> list[TextContent] | dict[str, Any]:
        """Return the authenticated user's identity.

        Used as an end-to-end sanity check that bearer auth + tool
        dispatch are wired correctly. Returns a ``dict`` on success
        (merging any ``register_mcp_whoami_extra`` hook fields); an
        isError ``TextContent`` if the request somehow reaches the
        handler without an authenticated token (defensive —
        ``RequireAuthMiddleware`` should already have 401'd).
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return [
                TextContent(
                    type="text",
                    text="not authenticated",
                    isError=True,  # type: ignore[call-arg]
                )
            ]
        result: dict[str, Any] = {
            "user_id": str(token.extra.get("user_id")),
            "tg_id": token.extra.get("tg_id"),
        }
        extras = get_mcp_whoami_extras()
        if extras:
            from app.mcp.tools._session import open_session

            user_id = uuid.UUID(result["user_id"])
            async with open_session() as session:
                from app.models.user import User

                user = await session.get(User, user_id)
                for fn in extras:
                    try:
                        result.update(await fn(session, user))
                    except Exception:  # pragma: no cover - defensive
                        logger.exception("whoami extra hook failed")
        return result

    # Phase 5b: register the eight real v1 tools + two Phase-6 stubs.
    register_all_tools(mcp)

    return mcp


def build_mcp_asgi_app(settings: Any, *, token_verifier: Any) -> tuple[Any, Any]:
    """Return ``(asgi_app, lifespan)`` for the MCP server.

    The lifespan MUST be composed into the parent app's lifespan or the
    FastMCP session manager never starts (silent hangs on first request).
    """
    mcp = build_fastmcp_server(
        token_verifier=token_verifier,
        public_url=settings.mcp_effective_public_url,
        canonical_resource_uri=settings.mcp_canonical_resource_uri,
        transport_security=_build_transport_security(settings),
    )
    asgi_app = mcp.streamable_http_app()
    lifespan = asgi_app.router.lifespan_context

    configured = getattr(settings, "mcp_allowed_origins", None) or []
    allowed = {*configured, settings.mcp_effective_public_url}
    wrapped = _build_origin_middleware(asgi_app, allowed)
    return wrapped, lifespan
