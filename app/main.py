"""FastAPI application factory and lifespan handler."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.ingestion import router as ingestion_router
from app.api.projects import router as projects_router
from app.api.webhook import router as webhook_router
from app.bot.setup import init_bot, shutdown_bot
from app.core.config import get_settings
from app.core.database import close_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: initialise resources on startup, clean up on shutdown."""
    settings = get_settings()
    init_db(settings.database_url)
    await init_bot(
        token=settings.telegram_bot_token,
        admin_chat_id=settings.admin_chat_id,
        webhook_base_url=settings.webhook_base_url,
    )
    yield
    await shutdown_bot()
    await close_db()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="tgram-analytics",
        description=(
            "Self-hosted, privacy-first analytics platform "
            "controlled entirely through a Telegram bot."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # Allow browsers to make cross-origin requests to the ingestion endpoints.
    # Fine-grained per-project origin validation is handled in ingestion.py via
    # the domain_allowlist; CORS middleware just lets the browser proceed past
    # the preflight check.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["POST", "GET"],
        allow_headers=["Content-Type"],
    )

    app.include_router(health_router)
    app.include_router(projects_router)
    app.include_router(ingestion_router)
    app.include_router(webhook_router)

    return app


app = create_app()
