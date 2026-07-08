"""Tiny health-check router for the cloud MCP module.

Phase 4 split the previously-combined ``/mcp`` mount into three pieces:

- ``/mcp/oauth`` — the OAuth 2.1 authorization-server (FastAPI router,
  no lifespan), registered directly from the cloud overlay's
  ``register()``.
- ``/mcp`` — the FastMCP streamable-HTTP ASGI app (Starlette app with
  its own lifespan), also registered from ``register()``.
- ``/mcp/_health`` — this router. Kept separate from the FastMCP app
  so liveness probes don't depend on the MCP session manager being
  healthy and don't require a bearer token.

Mount-order rationale: the OSS ``register_http_router`` hook applies
``app.mount(prefix, ...)``; Starlette resolves mounts in the order they
were registered and stops at the first match. ``register()`` therefore
registers ``/mcp/oauth`` and ``/mcp/_health`` BEFORE ``/mcp`` so the
more-specific routes aren't shadowed by the catch-all FastMCP mount.
"""

from __future__ import annotations

from fastapi import APIRouter


def build_health_router() -> APIRouter:
    """Return a router exposing ``/_health`` for liveness probes."""
    router = APIRouter()

    @router.get("/_health")
    async def health() -> dict:
        return {"status": "ok", "module": "mcp"}

    return router
