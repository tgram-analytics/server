"""Project CRUD service.

All functions accept an ``AsyncSession`` and flush but do NOT commit —
the caller (API layer) is responsible for committing or rolling back.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_api_key, hash_api_key
from app.models.project import Project
from app.models.settings import ProjectSettings
from app.services.audit import write_audit


async def create_project(
    session: AsyncSession,
    *,
    name: str,
    admin_chat_id: int,
    owner_user_id: uuid.UUID | None = None,
    domain_allowlist: list[str] | None = None,
) -> tuple[Project, str]:
    """Create a project and its default settings row.

    Returns ``(project, plaintext_api_key)``.  The plaintext key is the
    ONLY time it is exposed — it is NOT stored in the database.

    Both ``admin_chat_id`` and ``owner_user_id`` are written to the row.
    ``admin_chat_id`` remains required while the column is still NOT NULL
    in the schema; ``owner_user_id`` is the authoritative ownership link
    going forward and should be supplied by all new call sites. It is
    typed as optional only so legacy/transitional callers (e.g. the
    internal HTTP API in ``app/api/projects.py``) can land without
    breaking until Phase 3.4 wires them through ``ensure_singleton_user``.

    Hooks: any callable registered via
    :func:`app.extensions.register_project_pre_create` runs in
    registration order *after* the API key has been generated but
    *before* any DB write. A hook may raise to abort creation; the
    exception propagates and no project row is inserted. A hook may
    also return a dict of column overrides (whitelisted via
    :data:`app.extensions.PROJECT_OVERRIDABLE_FIELDS` and
    :data:`app.extensions.PROJECT_SETTINGS_OVERRIDABLE_FIELDS`) to
    apply to the new ``Project`` row and the auto-created
    ``ProjectSettings`` row respectively; the merged dict is split by
    target. Multiple hooks' dicts merge last-write-wins. OSS ships with
    zero hooks by default.
    """
    import logging

    from app.extensions import (
        PROJECT_OVERRIDABLE_FIELDS,
        PROJECT_SETTINGS_OVERRIDABLE_FIELDS,
        get_project_pre_create_hooks,
    )

    log = logging.getLogger(__name__)

    api_key = generate_api_key()

    safe_allowlist = list(domain_allowlist or [])
    project_overrides: dict[str, object] = {}
    settings_overrides: dict[str, object] = {}
    for hook in get_project_pre_create_hooks():
        result = await hook(
            session,
            name=name,
            owner_user_id=owner_user_id,
            domain_allowlist=list(safe_allowlist),
        )
        if result is None:
            continue
        if not isinstance(result, dict):
            raise TypeError(
                f"pre_create hook {hook!r} returned {type(result).__name__}; "
                "expected dict or None"
            )
        for key, value in result.items():
            if key in PROJECT_OVERRIDABLE_FIELDS:
                project_overrides[key] = value
            elif key in PROJECT_SETTINGS_OVERRIDABLE_FIELDS:
                settings_overrides[key] = value
            else:
                log.warning(
                    "pre_create hook %r returned non-whitelisted key %r — ignored",
                    hook,
                    key,
                )

    project = Project(
        name=name,
        api_key_hash=hash_api_key(api_key),
        admin_chat_id=admin_chat_id,
        owner_user_id=owner_user_id,
        domain_allowlist=safe_allowlist,
        **project_overrides,
    )
    session.add(project)
    await session.flush()

    # Auto-create settings with defaults so every project always has a row.
    settings = ProjectSettings(project_id=project.id, **settings_overrides)
    session.add(settings)
    await session.flush()

    await session.refresh(project)

    await write_audit(
        session,
        user_id=owner_user_id,
        action="project.create",
        target_type="project",
        target_id=str(project.id),
        metadata={"name": project.name},
    )

    return project, api_key


async def list_projects(
    session: AsyncSession,
    owner_user_id: uuid.UUID,
) -> list[Project]:
    """Return all projects belonging to *owner_user_id*, ordered by creation."""
    result = await session.execute(
        select(Project).where(Project.owner_user_id == owner_user_id).order_by(Project.created_at)
    )
    return list(result.scalars().all())


async def get_project(
    session: AsyncSession,
    project_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> Project | None:
    """Return a project by ID, or None if not found / not owned by user."""
    result = await session.execute(
        select(Project).where(
            Project.id == project_id,
            Project.owner_user_id == owner_user_id,
        )
    )
    return result.scalar_one_or_none()


async def delete_project(
    session: AsyncSession,
    project_id: uuid.UUID,
    owner_user_id: uuid.UUID,
) -> bool:
    """Delete a project (cascades to all child rows). Returns False if not found."""
    project = await get_project(session, project_id, owner_user_id)
    if project is None:
        return False
    await session.delete(project)
    await session.flush()
    return True
