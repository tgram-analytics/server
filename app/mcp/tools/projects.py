"""Project-discovery MCP tools.

Six handlers:

- ``list_projects`` — returns every project owned by the authenticated user.
- ``get_project`` — returns a single project by id (404-equivalent if the
  caller doesn't own it).
- ``list_event_names`` — returns distinct event names + counts +
  last-seen timestamps for a project.
- ``rotate_api_key`` — invalidates the project's current API key and
  returns a freshly generated plaintext key (shown exactly once,
  since the server only stores ``api_key_hash``).
- ``create_project`` — files a *pending* project-create request that the
  owner must approve in the Telegram bot; never creates the project
  directly.
- ``get_project_request_status`` — polls a project-create request until
  it resolves (approved/rejected/expired).

All follow the Phase 4 ``whoami`` shape:

1. Read the authenticated identity via ``get_access_token()``.
2. Open a session via ``open_session()``.
3. For project-scoped tools, call :func:`assert_project_owned_by` first.
4. Call the OSS ``app.services.*`` function — never inline SQL.
5. Return a dict on success, ``[TextContent(isError=True)]`` on error
   boundaries (no token, ownership failure, bad input).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ToolAnnotations

from app.mcp.auth import (
    MCPAccessToken,
    ProjectNotOwnedError,
    assert_project_owned_by,
)
from app.mcp.tools._schemas import (
    CreateProjectRequestResult,
    GetProjectResult,
    ListEventNamesResult,
    ListProjectsResult,
    ProjectInfo,
    ProjectRequestStatusResult,
    RotateAPIKeyResult,
)
from app.mcp.tools._session import open_session

logger = logging.getLogger("app.mcp.tools")

# N3 — Read-only project-discovery tools. ``readOnlyHint`` + ``idempotentHint``
# tell MCP clients these can be safely retried without side-effects;
# ``openWorldHint=False`` because we only read from our own DB.
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    idempotentHint=True,
    openWorldHint=False,
)


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


def _project_to_info(project: Any) -> ProjectInfo:
    """Project row → :class:`ProjectInfo` model.

    We deliberately do NOT expose ``api_key_hash`` (or anything that
    could be brute-forced offline). The plaintext API key is shown only
    once at create time via the bot.
    """
    return ProjectInfo(
        id=str(project.id),
        name=project.name,
        domain_allowlist=list(project.domain_allowlist or []),
        rate_limit_per_second=project.rate_limit_per_second,
        created_at=project.created_at.isoformat() if project.created_at else None,
    )


def register_project_tools(mcp: FastMCP) -> None:
    """Register the project discovery and mutation tools onto *mcp*."""

    @mcp.tool(title="List projects", annotations=_READ_ONLY)
    async def list_projects() -> list[TextContent] | ListProjectsResult:
        """Return every project owned by the authenticated user.

        Response: :class:`ListProjectsResult`. ``projects`` is a list of
        :class:`ProjectInfo` (id, name, domain_allowlist,
        rate_limit_per_second, created_at). Never includes the API key
        hash.
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()

        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.services.projects import list_projects as svc_list_projects

        async with open_session() as session:
            projects = await svc_list_projects(session, owner_user_id)

        return ListProjectsResult(projects=[_project_to_info(p) for p in projects])

    @mcp.tool(title="Get project", annotations=_READ_ONLY)
    async def get_project(project_id: str) -> list[TextContent] | GetProjectResult:
        """Return a single project by id.

        Returns an error content part if the project doesn't exist or
        the authenticated user doesn't own it (the two cases are
        deliberately indistinguishable to avoid project-id enumeration).
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()

        try:
            pid = uuid.UUID(project_id)
        except (ValueError, AttributeError):
            return _bad_input(f"invalid project_id {project_id!r}; must be a UUID")

        owner_user_id = uuid.UUID(token.extra["user_id"])

        async with open_session() as session:
            try:
                project = await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)

        return GetProjectResult(project=_project_to_info(project))

    @mcp.tool(title="List event names", annotations=_READ_ONLY)
    async def list_event_names(
        project_id: str,
    ) -> list[TextContent] | ListEventNamesResult:
        """Return distinct event names for a project, sorted by count desc.

        Each entry is an :class:`EventNameRow` with ``event_name``,
        ``count``, ``last_seen`` (ISO-8601 or ``None``).
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()

        try:
            pid = uuid.UUID(project_id)
        except (ValueError, AttributeError):
            return _bad_input(f"invalid project_id {project_id!r}; must be a UUID")

        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.mcp.tools._schemas import EventNameRow
        from app.services.analytics import list_event_names as svc_list_event_names

        async with open_session() as session:
            try:
                await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)

            rows = await svc_list_event_names(session, project_id=pid)

        events = [
            EventNameRow(
                event_name=r["event_name"],
                count=r["count"],
                last_seen=r["last_seen"].isoformat() if r["last_seen"] else None,
            )
            for r in rows
        ]
        return ListEventNamesResult(events=events)

    @mcp.tool(
        title="Rotate API key (destructive)",
        annotations=ToolAnnotations(
            title="Rotate API key (destructive)",
            # N3 — explicit destructive hint. The tool mints a fresh
            # plaintext key and invalidates the previous one; any SDK or
            # site using the old key starts rejecting events immediately.
            # An MCP client should surface a user confirmation prompt
            # before invoking this. Non-idempotent: calling twice yields
            # two different keys, with the second invalidating the first.
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def rotate_api_key(
        project_id: str,
    ) -> list[TextContent] | RotateAPIKeyResult:
        """Rotate this project's API key, invalidating the previous one.

        WARNING: any deployed SDK or site using the previous key will
        start rejecting events immediately. Update your integration
        before rotating, or be ready to redeploy right after.

        Returns the new key in plaintext exactly once — it cannot be
        retrieved later (the server only stores a hash).

        Response shape:
        ``{"api_key": str, "project_id": str, "message": str}``.
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()

        try:
            pid = uuid.UUID(project_id)
        except (ValueError, AttributeError):
            return _bad_input(f"invalid project_id {project_id!r}; must be a UUID")

        owner_user_id = uuid.UUID(token.extra["user_id"])

        # OSS has no dedicated "rotate" service function — the bot
        # (server/app/bot/handlers/settings.py::confirm_recreate_api_key)
        # inlines this same three-step pattern. We mirror it here so the
        # MCP tool stays single-source-of-truth with the bot's UX.
        from app.core.security import generate_api_key, hash_api_key
        from app.services.audit import write_audit

        async with open_session() as session:
            try:
                project = await assert_project_owned_by(session, pid, owner_user_id)
            except ProjectNotOwnedError:
                return _not_owned_error(project_id)

            new_key = generate_api_key()
            project.api_key_hash = hash_api_key(new_key)
            await write_audit(
                session,
                user_id=owner_user_id,
                action="project.api_key.rotate",
                target_type="project",
                target_id=str(project.id),
                metadata={"name": project.name, "via": "mcp"},
            )
            await session.commit()

        # Log only the project id — never the new key.
        logger.info("rotated api_key for project_id=%s", project.id)

        return RotateAPIKeyResult(
            api_key=new_key,
            project_id=str(project.id),
            message=(
                "API key rotated. The previous key is no longer valid — "
                "any SDK or site still using it will start rejecting "
                "events. Save this new key now; it cannot be retrieved "
                "later (the server only stores a hash)."
            ),
        )

    @mcp.tool(
        title="Create project (requires user confirmation)",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def create_project(
        name: str,
        domain_allowlist: list[str] | None = None,
    ) -> list[TextContent] | CreateProjectRequestResult:
        """File a request to create a new project. Does NOT create it directly.

        This tool inserts a *pending* project-create request and asks the
        owning user to approve or reject it in the Telegram bot. The
        project only comes into existence after the user taps Approve —
        an agent can never create a project without that confirmation.

        Workflow:

        1. Call this tool with the desired ``name`` (and optional
           ``domain_allowlist``). You get back a ``request_id`` with
           status ``pending``.
        2. Poll ``get_project_request_status`` with that ``request_id``
           until the status changes. The user typically responds within
           a few minutes.
        3. On ``approved``, the response includes the new ``project_id``.
           Call ``rotate_api_key`` with it to obtain an API key, then
           ``get_sdk_snippet`` to integrate.

        Requests that the user does not act on expire after 5 minutes
        (status ``expired``); you may file a new one afterwards.

        Response: :class:`CreateProjectRequestResult`
        (``request_id``, ``status``, ``message``).
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()

        clean_name = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(clean_name) <= 120:
            return _bad_input("invalid name; must be 1-120 characters after trimming whitespace")

        clean_allowlist: list[str] = []
        if domain_allowlist is not None:
            if len(domain_allowlist) > 20:
                return _bad_input("domain_allowlist too long; at most 20 entries")
            for entry in domain_allowlist:
                clean = entry.strip() if isinstance(entry, str) else ""
                if not clean or len(clean) > 253:
                    return _bad_input(
                        f"invalid domain_allowlist entry {entry!r}; each entry must "
                        "be a non-empty string of at most 253 characters"
                    )
                clean_allowlist.append(clean)

        # Canonicalize entries the same way the settings handler and
        # app.schemas.project do (bare lowercase host[:port], wildcard
        # preserved, dupes dropped) — migration 0009 normalized stored data.
        from app.services.events import normalize_origin_entries

        normalized_allowlist = normalize_origin_entries(clean_allowlist)
        if clean_allowlist and not normalized_allowlist:
            return _bad_input(
                "no valid domains in domain_allowlist; entries must be "
                "hostnames or URLs like example.com, https://example.com "
                "or *.example.com"
            )
        clean_allowlist = normalized_allowlist

        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.mcp.notify import notify_project_request
        from app.models.user import User
        from app.services.project_requests import (
            PendingCapExceededError,
            create_request,
        )

        async with open_session() as session:
            try:
                row = await create_request(
                    session,
                    owner_user_id=owner_user_id,
                    name=clean_name,
                    domain_allowlist=clean_allowlist,
                    requested_via="mcp",
                )
            except PendingCapExceededError:
                return _bad_input(
                    "too many pending project requests; ask the user to approve "
                    "or reject them in the Telegram bot first"
                )

            from sqlalchemy import select

            result = await session.execute(
                select(User.telegram_user_id).where(User.id == owner_user_id)
            )
            telegram_user_id = result.scalar_one_or_none()
            request_id = str(row.id)

            await session.commit()

        # Best-effort Telegram notification — never fails the tool call
        # (notify_project_request already swallows every exception).
        if telegram_user_id is not None:
            await notify_project_request(
                chat_id=telegram_user_id,
                request_id=request_id,
                name=clean_name,
                domain_allowlist=clean_allowlist,
            )

        return CreateProjectRequestResult(
            request_id=request_id,
            status="pending",
            message=(
                "The user has been asked to approve this project in the "
                "Telegram bot. Poll get_project_request_status with this "
                "request_id until it resolves — the typical wait is a few "
                "minutes. The request expires after 5 minutes, so poll promptly."
            ),
        )

    # Not ``_READ_ONLY``: polling a stale pending request lazily writes
    # the ``expired`` transition. Still idempotent — repeated polls converge.
    @mcp.tool(
        title="Get project request status",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_project_request_status(
        request_id: str,
    ) -> list[TextContent] | ProjectRequestStatusResult:
        """Poll the status of a project-create request filed via ``create_project``.

        Statuses: ``pending`` (awaiting the user's decision in Telegram),
        ``approved`` (project created — ``project_id`` is set), ``rejected``
        (the user declined), ``expired`` (no decision within 5 minutes).
        """
        token = get_access_token()
        if token is None or not isinstance(token, MCPAccessToken):
            return _not_authenticated()

        try:
            rid = uuid.UUID(request_id)
        except (ValueError, AttributeError):
            return _bad_input(f"invalid request_id {request_id!r}; must be a UUID")

        owner_user_id = uuid.UUID(token.extra["user_id"])

        from app.services.project_requests import (
            claim_request,
            get_request,
            is_expired,
        )

        async with open_session() as session:
            row = await get_request(session, rid, owner_user_id)
            if row is None:
                return _bad_input(f"request {request_id} not found or you don't have access")

            if row.status == "pending" and is_expired(row):
                if await claim_request(session, row, status="expired"):
                    await session.commit()
                else:
                    # Lost the race — another transaction resolved the
                    # request concurrently; report its current status.
                    await session.rollback()
                    row = await get_request(session, rid, owner_user_id)
                    if row is None:
                        return _bad_input(
                            f"request {request_id} not found or you don't have access"
                        )

            status = row.status
            project_id = str(row.project_id) if row.project_id else None

        messages = {
            "pending": "waiting for the user to approve in the Telegram bot",
            "approved": (
                f"approved — project {project_id} was created; call "
                "rotate_api_key to obtain an API key, then get_sdk_snippet "
                "to integrate"
            ),
            "rejected": "the user rejected this request",
            "expired": "request expired without a decision; you may file a new one",
        }

        return ProjectRequestStatusResult(
            request_id=str(rid),
            status=status,
            project_id=project_id,
            message=messages.get(status, status),
        )
