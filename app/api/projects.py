"""Internal project management endpoints.

All routes are protected by ``X-Internal-Key: <SECRET_KEY>`` header.
These endpoints are only meant to be called by the Telegram bot and
trusted internal tooling — never exposed to end-users directly.
"""

import hmac
import uuid

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import ensure_singleton_user
from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectWithKeyResponse
from app.services.projects import create_project, delete_project, get_project, list_projects

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])

_key_header = APIKeyHeader(name="X-Internal-Key", auto_error=False)


async def _require_internal_key(
    key: str | None = Security(_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    """Dependency: reject callers without the correct X-Internal-Key."""
    if not key or not hmac.compare_digest(key, settings.secret_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Internal-Key")


@router.post(
    "/projects",
    response_model=ProjectWithKeyResponse,
    status_code=201,
    dependencies=[Depends(_require_internal_key)],
    summary="Create a project and receive its API key",
)
async def create_project_endpoint(
    body: ProjectCreate,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ProjectWithKeyResponse:
    """Create a new project.

    The ``api_key`` field in the response is the **only** time the
    plaintext key is shown — it is hashed before storage and cannot be
    retrieved again.
    """
    user = await ensure_singleton_user(session, settings.admin_chat_id)
    project, api_key = await create_project(
        session,
        name=body.name,
        admin_chat_id=settings.admin_chat_id,
        owner_user_id=user.id,
        domain_allowlist=body.domain_allowlist,
    )
    await session.commit()
    return ProjectWithKeyResponse(
        id=project.id,
        name=project.name,
        admin_chat_id=project.admin_chat_id,
        domain_allowlist=list(project.domain_allowlist),
        created_at=project.created_at,
        api_key=api_key,
    )


@router.get(
    "/projects",
    response_model=list[ProjectResponse],
    dependencies=[Depends(_require_internal_key)],
    summary="List all projects",
)
async def list_projects_endpoint(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[ProjectResponse]:
    user = await ensure_singleton_user(session, settings.admin_chat_id)
    await session.commit()
    projects = await list_projects(session, user.id)
    return [ProjectResponse.model_validate(p) for p in projects]


@router.get(
    "/projects/{project_id}",
    response_model=ProjectResponse,
    dependencies=[Depends(_require_internal_key)],
    summary="Get a single project by ID",
)
async def get_project_endpoint(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ProjectResponse:
    user = await ensure_singleton_user(session, settings.admin_chat_id)
    await session.commit()
    project = await get_project(session, project_id, user.id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectResponse.model_validate(project)


@router.delete(
    "/projects/{project_id}",
    status_code=204,
    dependencies=[Depends(_require_internal_key)],
    summary="Delete a project and all its data",
)
async def delete_project_endpoint(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> None:
    user = await ensure_singleton_user(session, settings.admin_chat_id)
    deleted = await delete_project(session, project_id, user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Project not found")
    await session.commit()
