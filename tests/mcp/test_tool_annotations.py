"""MCP tool annotations (N3).

Per MCP spec section "Tools", each tool may carry ``annotations`` —
``readOnlyHint``, ``destructiveHint``, ``idempotentHint``,
``openWorldHint``. Clients use these to decide whether to require user
confirmation (esp. before destructive tools) and whether to retry on
failure. Locks the values down so a refactor can't quietly drop the
``destructiveHint`` from ``rotate_api_key``.

Runs offline against ``build_fastmcp_server`` — no DB, no HTTP.
"""

from __future__ import annotations

import asyncio

import pytest

from app.mcp.auth import StaticTokenVerifier
from app.mcp.server import build_fastmcp_server

PUBLIC_URL = "https://mcp.test"


# Expected (readOnlyHint, destructiveHint, idempotentHint, openWorldHint)
# tuples for each tool. ``None`` means "unset" (FastMCP default).
EXPECTED: dict[str, dict[str, bool]] = {
    "whoami": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    "list_projects": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "get_project": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "list_event_names": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "query_events": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "compare_periods": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "top_pages": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "recent_events": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "verify_integration": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    # get_integration_guide fetches docs from GitHub → openWorldHint=True.
    "get_integration_guide": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "get_sdk_snippet": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    # rotate_api_key is the only destructive tool. Clients should prompt
    # the user before invoking it.
    "rotate_api_key": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
}


@pytest.fixture(scope="module")
def listed_tools():
    mcp = build_fastmcp_server(token_verifier=StaticTokenVerifier(), public_url=PUBLIC_URL)
    tools = asyncio.run(mcp.list_tools())
    return {t.name: t for t in tools}


def test_every_tool_has_annotations(listed_tools) -> None:
    """No tool may ship without annotations — MCP clients rely on them."""
    for name in EXPECTED:
        tool = listed_tools.get(name)
        assert tool is not None, f"missing tool {name!r}"
        assert tool.annotations is not None, f"tool {name!r} has no annotations"


@pytest.mark.parametrize("name,hints", list(EXPECTED.items()))
def test_tool_annotations_match_expected(listed_tools, name: str, hints: dict) -> None:
    """Every documented hint is present and equal to the expected value."""
    tool = listed_tools[name]
    ann = tool.annotations
    for key, expected in hints.items():
        actual = getattr(ann, key, None)
        assert actual == expected, (
            f"tool {name!r}: annotations.{key} expected {expected!r}, got {actual!r}"
        )


def test_rotate_api_key_is_marked_destructive(listed_tools) -> None:
    """Lock in the single most important guarantee.

    A client that ignores ``destructiveHint=True`` on this tool would
    silently re-key projects on every "summarize my analytics" follow-up
    prompt. The hint MUST stay True; this test prevents an accidental
    revert during refactors.
    """
    tool = listed_tools["rotate_api_key"]
    assert tool.annotations.destructiveHint is True
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.idempotentHint is False
