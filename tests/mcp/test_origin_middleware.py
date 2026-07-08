"""N2 — Origin header validation on the streamable-HTTP endpoint.

Per the MCP spec (2025-11-25), the server MUST validate ``Origin`` on
every request to prevent DNS rebinding. These tests poke the middleware
directly via a tiny stub ASGI app so we don't depend on a running
FastMCP server (which would require DB + tokens).
"""

from __future__ import annotations

import json

import pytest

from app.mcp.server import _build_origin_middleware


async def _ok_app(scope, receive, send) -> None:
    """Stub downstream app: 200 OK with empty body."""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b'{"ok":true}'})


class _Collector:
    """Capture ASGI send() calls so the test can assert on them."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)


async def _empty_receive() -> dict:  # pragma: no cover - never invoked
    return {"type": "http.request"}


def _scope(*, origin: bytes | None) -> dict:
    headers: list[tuple[bytes, bytes]] = [
        (b"accept", b"application/json"),
    ]
    if origin is not None:
        headers.append((b"origin", origin))
    return {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers,
    }


ALLOWED = {"https://claude.ai", "https://chat.openai.com", "https://mcp.test", "null"}


@pytest.mark.asyncio
async def test_missing_origin_passes_through() -> None:
    mw = _build_origin_middleware(_ok_app, ALLOWED)
    collector = _Collector()
    await mw(_scope(origin=None), _empty_receive, collector)
    starts = [m for m in collector.messages if m["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 200


@pytest.mark.asyncio
async def test_allowed_origin_passes_through() -> None:
    mw = _build_origin_middleware(_ok_app, ALLOWED)
    collector = _Collector()
    await mw(_scope(origin=b"https://claude.ai"), _empty_receive, collector)
    starts = [m for m in collector.messages if m["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 200


@pytest.mark.asyncio
async def test_disallowed_origin_returns_403_with_error_body() -> None:
    mw = _build_origin_middleware(_ok_app, ALLOWED)
    collector = _Collector()
    await mw(_scope(origin=b"https://attacker.example"), _empty_receive, collector)
    starts = [m for m in collector.messages if m["type"] == "http.response.start"]
    bodies = [m for m in collector.messages if m["type"] == "http.response.body"]
    assert starts and starts[0]["status"] == 403
    assert bodies
    payload = json.loads(bodies[0]["body"].decode())
    assert payload["error"] == "origin_not_allowed"


@pytest.mark.asyncio
async def test_null_origin_passes_through() -> None:
    """``Origin: null`` covers ``file://`` clients per fetch spec."""
    mw = _build_origin_middleware(_ok_app, ALLOWED)
    collector = _Collector()
    await mw(_scope(origin=b"null"), _empty_receive, collector)
    starts = [m for m in collector.messages if m["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 200


@pytest.mark.asyncio
async def test_non_http_scope_passes_through() -> None:
    """Lifespan/websocket scopes must not be intercepted."""
    mw = _build_origin_middleware(_ok_app, ALLOWED)
    collector = _Collector()
    # _ok_app would crash on a lifespan scope; use a tiny passthrough.
    called = {"ok": False}

    async def _downstream(scope, receive, send):
        called["ok"] = True

    mw = _build_origin_middleware(_downstream, ALLOWED)
    await mw({"type": "lifespan", "headers": []}, _empty_receive, collector)
    assert called["ok"] is True
