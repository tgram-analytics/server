"""Project-create request service (AI-agent confirmation flow).

All functions accept an ``AsyncSession`` and flush but do NOT commit —
the caller (MCP tool / bot handler) is responsible for committing or
rolling back. Same transactional contract as
``app.services.projects.create_project``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project_create_request import ProjectCreateRequest
from app.services.audit import write_audit

# Maximum number of unresolved (``pending``) requests a single user may
# have at once — keeps an agent from spamming the owner's Telegram chat.
PENDING_CAP = 3

# A pending request that the owner hasn't decided on within this window
# is considered expired (enforced lazily by :func:`is_expired`).
REQUEST_TTL = timedelta(minutes=5)


class PendingCapExceededError(Exception):
    """Raised when a user already has ``PENDING_CAP`` pending requests."""


async def create_request(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    name: str,
    domain_allowlist: list[str] | None = None,
    requested_via: str = "mcp",
) -> ProjectCreateRequest:
    """Insert a ``pending`` project-create request for *owner_user_id*.

    Raises :class:`PendingCapExceededError` if the user already has
    ``PENDING_CAP`` unresolved requests.
    """
    result = await session.execute(
        select(func.count())
        .select_from(ProjectCreateRequest)
        .where(
            ProjectCreateRequest.owner_user_id == owner_user_id,
            ProjectCreateRequest.status == "pending",
        )
    )
    pending_count = result.scalar_one()
    if pending_count >= PENDING_CAP:
        raise PendingCapExceededError(
            f"user {owner_user_id} already has {pending_count} pending project requests"
        )

    row = ProjectCreateRequest(
        owner_user_id=owner_user_id,
        name=name,
        domain_allowlist=list(domain_allowlist or []),
        status="pending",
        requested_via=requested_via,
    )
    session.add(row)
    await session.flush()

    await write_audit(
        session,
        user_id=owner_user_id,
        action="project.create.request",
        target_type="project_create_request",
        target_id=str(row.id),
        metadata={"name": name, "via": requested_via},
    )

    return row


async def get_request(
    session: AsyncSession,
    request_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> ProjectCreateRequest | None:
    """Return a request by ID, or None if not found / not owned by user."""
    result = await session.execute(
        select(ProjectCreateRequest).where(
            ProjectCreateRequest.id == request_id,
            ProjectCreateRequest.owner_user_id == owner_user_id,
        )
    )
    return result.scalar_one_or_none()


def is_expired(
    request: ProjectCreateRequest,
    *,
    now: datetime | None = None,
) -> bool:
    """True if *request* is still ``pending`` but older than ``REQUEST_TTL``.

    ``created_at`` may come back naive from SQLite in tests; treat a
    naive timestamp as UTC.
    """
    if request.status != "pending":
        return False
    created_at = request.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    if now is None:
        now = datetime.now(UTC)
    return now - created_at > REQUEST_TTL


async def claim_request(
    session: AsyncSession,
    request: ProjectCreateRequest,
    *,
    status: str,
    project_id: uuid.UUID | None = None,
) -> bool:
    """Atomically move *request* from ``pending`` to a terminal *status*.

    Compare-and-set: the UPDATE only matches while the row's stored
    status is still ``pending``, so exactly one concurrent caller wins.
    In webhook mode ``application.process_update`` runs per HTTP request,
    so two rapid Approve taps race each other — without this guard both
    would read ``pending`` and both create a project.

    On Postgres a concurrent UPDATE of the same row blocks on the row
    lock until the first transaction commits, then re-evaluates its WHERE
    clause against the committed row and matches 0 rows. ``rowcount``
    therefore tells us whether *we* performed the transition.

    Returns True if this call claimed the request (attributes on the ORM
    object are updated in place and the ``project.create.request.<status>``
    audit entry is written); False if another transaction already
    resolved it — the caller should roll back and treat the request as
    already handled.
    """
    resolved_at = datetime.now(UTC)
    result = await session.execute(
        update(ProjectCreateRequest)
        .where(
            ProjectCreateRequest.id == request.id,
            ProjectCreateRequest.status == "pending",
        )
        .values(status=status, project_id=project_id, resolved_at=resolved_at)
    )
    await session.flush()
    if result.rowcount != 1:
        return False

    # Refresh the ORM object in place so callers can read the terminal
    # state without a refetch.
    request.status = status
    request.project_id = project_id
    request.resolved_at = resolved_at

    await write_audit(
        session,
        user_id=request.owner_user_id,
        action=f"project.create.request.{status}",
        target_type="project_create_request",
        target_id=str(request.id),
        metadata={"name": request.name},
    )
    return True
