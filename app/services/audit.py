"""Audit log writer.

Caller is responsible for committing the session — same transactional
contract as ``app.services.projects.create_project``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent


async def write_audit(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    action: str,
    target_type: str,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    row = AuditEvent(
        user_id=user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        metadata_json=metadata or {},
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row
