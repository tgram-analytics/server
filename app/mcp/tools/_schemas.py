"""Pydantic return models for every v1 MCP tool (N4).

Per MCP spec section "Output Schema", a tool that declares
``outputSchema`` lets the client validate ``structuredContent`` against
a known shape. Without it the LLM gets arbitrary dicts and can
hallucinate field names. FastMCP 1.27 auto-derives ``outputSchema``
from a typed Pydantic return, so by switching each handler from
``dict[str, Any]`` to a model we publish a precise schema for free.

Design choices:

- Each model is the *success* shape only. Tools keep the
  ``Result | list[TextContent]`` union return type so error paths stay
  on the existing ``TextContent(isError=True)`` convention; FastMCP
  produces a schema that says "result is either the model or the
  TextContent list", which is the truthful description.
- Datetimes are typed ``datetime`` (Pydantic emits ``format=date-time``
  in the schema). Callers that want ISO strings can serialize with
  ``model_dump(mode="json")``.
- ``RotateAPIKeyResult.api_key`` is a plain ``str`` (not
  ``SecretStr``). ``SecretStr.__repr__`` redacts to ``**********`` which
  would corrupt the JSON shipped to the client; the redaction belongs
  in the *log* statement (already implemented), not in the response
  model.

Cross-cutting note: the cloud DB stores ``api_key_hash`` only — never
plaintext — so ``ProjectInfo`` deliberately exposes neither the hash
nor the original key. Snippets render with a ``<YOUR_API_KEY>``
placeholder until the user pastes their own.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ─── Discovery / metadata ────────────────────────────────────────────────────


class WhoamiResult(BaseModel):
    """Result of :func:`whoami`."""

    user_id: str = Field(..., description="UUID of the authenticated cloud user.")
    tg_id: int | None = Field(
        None, description="Telegram user_id of the authenticated user, if known."
    )


class ProjectInfo(BaseModel):
    """Single project metadata row.

    Mirrors the shape ``_project_to_dict`` returns. The ``api_key_hash``
    field on the OSS ``Project`` row is intentionally omitted — even
    the hash shouldn't leak over MCP.
    """

    id: str
    name: str
    domain_allowlist: list[str] = Field(default_factory=list)
    rate_limit_per_second: int | None = None
    created_at: str | None = None


class ListProjectsResult(BaseModel):
    """Result of :func:`list_projects`."""

    projects: list[ProjectInfo]


class GetProjectResult(BaseModel):
    """Result of :func:`get_project`."""

    project: ProjectInfo


class EventNameRow(BaseModel):
    """One row of :func:`list_event_names`."""

    event_name: str
    count: int
    last_seen: str | None = None


class ListEventNamesResult(BaseModel):
    """Result of :func:`list_event_names`."""

    events: list[EventNameRow]


# ─── Analytics ───────────────────────────────────────────────────────────────


class TimeBucket(BaseModel):
    """One bucket in a time-series response."""

    bucket: str | None
    count: int


class QueryEventsResult(BaseModel):
    """Result of :func:`query_events`."""

    total: int
    series: list[TimeBucket]
    period: str
    granularity: str


class ComparePeriodsResult(BaseModel):
    """Result of :func:`compare_periods`."""

    current: int
    previous: int
    delta_pct: float | None = None
    period: str


class TopPagesRow(BaseModel):
    """One row of :func:`top_pages`."""

    value: str
    count: int


class TopPagesResult(BaseModel):
    """Result of :func:`top_pages`."""

    pages: list[TopPagesRow]
    period: str


class RecentEventRow(BaseModel):
    """One row of :func:`recent_events`."""

    event_name: str
    timestamp: str | None = None


class RecentEventsResult(BaseModel):
    """Result of :func:`recent_events`."""

    events: list[RecentEventRow]


# ─── Setup / docs ────────────────────────────────────────────────────────────


class VerifyIntegrationResult(BaseModel):
    """Result of :func:`verify_integration`."""

    count: int
    since_minutes: int
    is_receiving: bool


class IntegrationGuideResult(BaseModel):
    """Result of :func:`get_integration_guide`.

    Mirrors :func:`fetch_repo_readme`'s payload. ``stale`` is True only
    when the live fetch failed and we fell back to a cached copy.
    """

    markdown: str
    source_url: str
    commit_sha: str
    fetched_at: str
    stale: bool


class SDKSnippetResult(BaseModel):
    """Result of :func:`get_sdk_snippet`."""

    snippet: str
    platform: str


# ─── Project mutation ────────────────────────────────────────────────────────


class RotateAPIKeyResult(BaseModel):
    """Result of :func:`rotate_api_key`.

    ``api_key`` is the freshly minted plaintext key — shown exactly
    once. Callers should treat the response as sensitive (do not log,
    do not echo to other channels). The server never persists this
    value; only ``hash_api_key(api_key)`` is stored.
    """

    api_key: str = Field(
        ...,
        description=(
            "Freshly minted plaintext API key. Shown exactly once — "
            "the server only stores its hash."
        ),
    )
    project_id: str
    message: str


class CreateProjectRequestResult(BaseModel):
    """Result of :func:`create_project_request`.

    Creating a project over MCP never creates it directly — it inserts a
    pending request that the owner must approve in the Telegram bot.
    """

    request_id: str = Field(..., description="UUID of the pending project-create request.")
    status: str = Field(..., description='Always "pending" on creation.')
    message: str = Field(
        ...,
        description=(
            "Human-readable next step, e.g. instructions to approve the "
            "request in Telegram and poll its status."
        ),
    )


class ProjectRequestStatusResult(BaseModel):
    """Result of :func:`get_project_request_status`."""

    request_id: str = Field(..., description="UUID of the project-create request being polled.")
    status: str = Field(
        ...,
        description=("Current lifecycle state: pending | approved | rejected | expired."),
    )
    project_id: str | None = Field(
        None,
        description="UUID of the created project. Set only when approved.",
    )
    message: str = Field(..., description="Human-readable summary of the current state.")


# Re-exports to keep import sites tidy.
__all__ = [
    "WhoamiResult",
    "ProjectInfo",
    "ListProjectsResult",
    "GetProjectResult",
    "EventNameRow",
    "ListEventNamesResult",
    "TimeBucket",
    "QueryEventsResult",
    "ComparePeriodsResult",
    "TopPagesRow",
    "TopPagesResult",
    "RecentEventRow",
    "RecentEventsResult",
    "VerifyIntegrationResult",
    "IntegrationGuideResult",
    "SDKSnippetResult",
    "RotateAPIKeyResult",
    "CreateProjectRequestResult",
    "ProjectRequestStatusResult",
]
