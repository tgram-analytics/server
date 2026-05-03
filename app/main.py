"""FastAPI application factory and lifespan handler."""

import logging
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
from app.core.privacy import RedactingFilter
from app.core.redis_client import close_redis, init_redis
from app.jobs.scheduler import shutdown_scheduler, start_scheduler
from app.plugins import load_plugins


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: initialise resources on startup, clean up on shutdown."""
    settings = get_settings()
    init_db(settings.database_url)
    init_redis(settings.redis_url)
    start_scheduler()
    # Discover and register downstream extensions BEFORE init_bot — plugins
    # may register bot filters that build_application needs to compose, and
    # may register a user resolver that init_bot's singleton bootstrap
    # would otherwise be the sole source of truth for.
    load_plugins()
    await init_bot(
        token=settings.telegram_bot_token,
        admin_chat_id=settings.admin_chat_id,
        webhook_base_url=settings.webhook_base_url,
    )
    yield
    await shutdown_bot()
    await shutdown_scheduler()
    await close_redis()
    await close_db()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Install the redacting filter on the root logger so every logger in the
    # process (uvicorn, sqlalchemy, app.*) inherits it. ``create_app()`` runs
    # at import time and only once; we still guard against duplicate
    # installations in case it is reloaded by tests.
    root_logger = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for f in root_logger.filters):
        root_logger.addFilter(RedactingFilter())

    app = FastAPI(
        title="tgram-analytics",
        description=(
            "Self-hosted, privacy-first analytics platform "
            "controlled entirely through a Telegram bot."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS allows browsers to make cross-origin requests to the ingestion
    # endpoints.  Fine-grained per-project origin validation is handled in
    # ingestion.py via the domain_allowlist; this middleware just lets the
    # browser proceed past the preflight check.  Methods and headers are
    # restricted to exactly what the ingestion endpoints need.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["POST"],
        allow_headers=["Content-Type"],
        max_age=600,
    )

    app.include_router(health_router)
    app.include_router(projects_router)
    app.include_router(ingestion_router)
    app.include_router(webhook_router)

    return app


app = create_app()
