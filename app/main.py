"""FastAPI application factory and lifespan handler."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.database import close_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: initialise resources on startup, clean up on shutdown."""
    settings = get_settings()
    init_db(settings.database_url)
    yield
    await close_db()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="tg-analytics",
        description=(
            "Self-hosted, privacy-first analytics platform "
            "controlled entirely through a Telegram bot."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(health_router)

    return app


app = create_app()
