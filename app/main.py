"""FastAPI application factory and lifespan handler."""

import logging
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import APIRouter, FastAPI
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
from app.core.sentry import init_sentry
from app.extensions import get_registered_http_routers
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
    # Mount any HTTP routers/ASGI apps registered by plugins. APIRouter
    # instances merge cleanly into the main app's OpenAPI; anything else
    # is mounted as an ASGI sub-app. Each plugin may also supply an async
    # context-manager lifespan that we compose with ours via AsyncExitStack
    # so child resources unwind on shutdown.
    async with AsyncExitStack() as stack:
        for prefix, router_or_app, child_lifespan in get_registered_http_routers():
            if isinstance(router_or_app, APIRouter):
                app.include_router(router_or_app, prefix=prefix)
            else:
                app.mount(prefix, router_or_app)
            if child_lifespan is not None:
                await stack.enter_async_context(child_lifespan(app))
        # Mount the MCP surface unless disabled. A plugin may have mounted
        # its own ASGI app at /mcp (pre-hook-era cloud overlays did); in
        # that case skip ours so the two don't stack.
        plugin_owns_mcp = any(
            prefix == "/mcp" and not isinstance(router_or_app, APIRouter)
            for prefix, router_or_app, _ in get_registered_http_routers()
        )
        if settings.mcp_enabled and not plugin_owns_mcp:
            from app.extensions import get_mcp_token_verifier
            from app.mcp.auth import StaticTokenVerifier
            from app.mcp.router import build_health_router
            from app.mcp.server import build_mcp_asgi_app

            verifier = get_mcp_token_verifier() or StaticTokenVerifier()
            app.include_router(build_health_router(), prefix="/mcp")
            # Self-host OAuth for header-less MCP clients (Claude Desktop).
            # Registered BEFORE the /mcp ASGI mount: Starlette matches routes
            # in insertion order, so the /mcp/oauth/* routes must precede the
            # catch-all mount or requests fall through to the FastMCP app and
            # 404 — the same reason the /mcp health router is included first.
            # Only when the DEFAULT verifier is in use: a plugin-registered
            # verifier (cloud overlay) brings its own OAuth and well-known.
            if settings.mcp_oauth_enabled and get_mcp_token_verifier() is None:
                from app.mcp.oauth.router import build_oauth_router
                from app.mcp.well_known import build_well_known_router

                app.include_router(build_oauth_router(), prefix="/mcp/oauth")
                app.include_router(
                    build_well_known_router(public_url=settings.mcp_effective_public_url)
                )
            mcp_asgi_app, mcp_lifespan = build_mcp_asgi_app(settings, token_verifier=verifier)
            app.mount("/mcp", mcp_asgi_app)
            await stack.enter_async_context(mcp_lifespan(app))
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

    # Initialise Sentry before FastAPI() so its ASGI/HTTPX/SQLAlchemy
    # auto-integrations attach to the app and outbound clients. No-op when
    # SENTRY_DSN is unset or sentry-sdk is not installed.
    init_sentry(get_settings())

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
