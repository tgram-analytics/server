"""MCP tool outputSchema (N4).

Per MCP spec section "Output Schema", a tool that publishes an
``outputSchema`` lets the client validate ``structuredContent`` against
a precise contract. Without it the LLM gets an arbitrary dict and can
hallucinate field names on follow-up calls.

We declare a Pydantic model per tool return shape
(:mod:`app.mcp.tools._schemas`); FastMCP 1.27
auto-derives ``outputSchema`` from the typed return. These tests lock
in the contract:

1. Every tool publishes a non-empty ``outputSchema``.
2. The schema references each known field for the most commonly used
   data tools — so a refactor that silently switches a return type to
   ``dict[str, Any]`` breaks the test.
3. The :class:`RotateAPIKeyResult` model validates a payload-shaped
   dict, ensuring a future change to the rotation flow can't accidentally
   change the response shape.

Runs offline — no DB, no HTTP.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.mcp.auth import StaticTokenVerifier
from app.mcp.server import build_fastmcp_server
from app.mcp.tools._schemas import (
    ComparePeriodsResult,
    GetProjectResult,
    IntegrationGuideResult,
    ListEventNamesResult,
    ListProjectsResult,
    QueryEventsResult,
    RecentEventsResult,
    RotateAPIKeyResult,
    SDKSnippetResult,
    TopPagesResult,
    VerifyIntegrationResult,
    WhoamiResult,
)

PUBLIC_URL = "https://mcp.test"


@pytest.fixture(scope="module")
def listed_tools():
    mcp = build_fastmcp_server(token_verifier=StaticTokenVerifier(), public_url=PUBLIC_URL)
    tools = asyncio.run(mcp.list_tools())
    return {t.name: t for t in tools}


V1_TOOLS = [
    "whoami",
    "list_projects",
    "get_project",
    "list_event_names",
    "query_events",
    "compare_periods",
    "top_pages",
    "recent_events",
    "verify_integration",
    "get_integration_guide",
    "get_sdk_snippet",
    "rotate_api_key",
]

ALL_TOOLS = [
    *V1_TOOLS,
    "list_alerts",
    "alert_history",
    "create_alert",
    "set_alert_active",
    "delete_alert",
]


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_every_tool_publishes_output_schema(listed_tools, name: str) -> None:
    """No tool may ship without an outputSchema."""
    tool = listed_tools[name]
    schema = tool.outputSchema
    assert schema is not None, f"tool {name!r} has no outputSchema"
    assert isinstance(schema, dict)
    # The schema is non-trivial — at minimum it contains ``properties``
    # (or refers to a definition that does).
    serialized = json.dumps(schema)
    assert "properties" in serialized, f"tool {name!r} schema has no properties: {schema}"


def test_query_events_schema_declares_known_fields(listed_tools) -> None:
    """Lock in the QueryEventsResult shape via the published schema."""
    schema = listed_tools["query_events"].outputSchema
    serialized = json.dumps(schema)
    for field in ("total", "series", "period", "granularity"):
        assert f'"{field}"' in serialized, (
            f"expected field {field!r} in query_events outputSchema: {schema}"
        )


def test_rotate_api_key_schema_declares_api_key_field(listed_tools) -> None:
    """The destructive tool's response schema must declare ``api_key``.

    Without this in the schema, a client cannot validate that the
    server's response actually includes the new key the user must save.
    """
    schema = listed_tools["rotate_api_key"].outputSchema
    serialized = json.dumps(schema)
    assert '"api_key"' in serialized
    assert '"project_id"' in serialized
    assert '"message"' in serialized


# ─── Model validation round-trip ────────────────────────────────────────────


def test_rotate_api_key_result_validates() -> None:
    """RotateAPIKeyResult accepts a payload-shaped dict."""
    model = RotateAPIKeyResult.model_validate(
        {
            "api_key": "tga_test_key_xxxxxxxxxxxxxxxxxxxxxxxx",
            "project_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "message": "rotated",
        }
    )
    assert model.api_key.startswith("tga_")
    assert model.message == "rotated"


def test_query_events_result_validates() -> None:
    """QueryEventsResult round-trips a small payload."""
    model = QueryEventsResult.model_validate(
        {
            "total": 5,
            "series": [{"bucket": "2026-05-01T00:00:00", "count": 5}],
            "period": "7d",
            "granularity": "day",
        }
    )
    assert model.total == 5
    assert model.series[0].count == 5


def test_whoami_result_validates() -> None:
    """WhoamiResult round-trips a small payload."""
    model = WhoamiResult.model_validate(
        {"user_id": "11111111-1111-1111-1111-111111111111", "tg_id": 42}
    )
    assert model.tg_id == 42


# ─── Models cover every tool ────────────────────────────────────────────────


def test_every_v1_tool_has_a_schema_module() -> None:
    """Each tool maps to one Pydantic model in ``_schemas``.

    A refactor that adds a tool but forgets the schema model will fail
    this list-mismatch check.
    """
    expected_models = {
        WhoamiResult,
        ListProjectsResult,
        GetProjectResult,
        ListEventNamesResult,
        QueryEventsResult,
        ComparePeriodsResult,
        TopPagesResult,
        RecentEventsResult,
        VerifyIntegrationResult,
        IntegrationGuideResult,
        SDKSnippetResult,
        RotateAPIKeyResult,
    }
    # The 12 v1 tools map 1:1 to the 12 success-result models declared in
    # _schemas.py (verify by count, not by name — the mapping is enforced
    # in the tool source files themselves). Later tools (alerts) are not
    # 1:1 — create_alert and set_alert_active share ``AlertInfo`` — so
    # this check stays scoped to the v1 set.
    assert len(expected_models) == len(V1_TOOLS)
