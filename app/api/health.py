"""Health check endpoint."""

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    status: str


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    """Return ``{"status": "ok"}`` when the server is running."""
    return HealthResponse(status="ok")


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
