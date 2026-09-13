"""Marketing-site redirects for hosted web paths.

The Coolify-hosted domain historically served API-only 404s at ``/``.
Send browsers to the canonical marketing site while leaving ``/mcp``,
webhooks, health, ingestion, and other app routes untouched.
"""

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()

_MARKETING_URL = "https://tgram-analytics.com/"


@router.get("/", include_in_schema=False)
async def redirect_root_to_marketing() -> RedirectResponse:
    """Permanent redirect for the bare host root to the marketing site."""
    return RedirectResponse(url=_MARKETING_URL, status_code=301)
