"""Health check endpoint."""

import os
import subprocess
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str
    commit: str


def _server_version() -> str:
    try:
        return version("tgram-analytics-server")
    except PackageNotFoundError:
        return "unknown"


def _git_commit() -> str:
    """Deployed commit SHA.

    Prefers the ``GIT_SHA`` build-time env var (set by the Docker build via
    ``--build-arg GIT_SHA=$(git rev-parse HEAD)``) since production images
    don't ship a ``.git`` dir. Falls back to reading the local repo for dev.
    """
    env_sha = os.environ.get("GIT_SHA")
    if env_sha:
        return env_sha
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    """Return status, installed package version, and deployed commit SHA."""
    return HealthResponse(status="ok", version=_server_version(), commit=_git_commit())


_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#2AABEE"/>'
    '<rect x="6" y="17" width="4" height="9" rx="1" fill="#fff"/>'
    '<rect x="14" y="11" width="4" height="15" rx="1" fill="#fff"/>'
    '<rect x="22" y="6" width="4" height="20" rx="1" fill="#fff"/>'
    "</svg>"
)


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Tiny inline SVG so MCP connectors and browsers show a real icon."""
    return Response(
        content=_FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )
