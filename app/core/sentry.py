"""Optional Sentry error-tracking integration.

``init_sentry`` is a no-op unless ``SENTRY_DSN`` is set. ``sentry-sdk`` is
a base dependency, but the import is still guarded so a stripped-down
deployment that drops the package keeps booting.

When enabled this module wires up the standard FastAPI / Starlette /
SQLAlchemy / Redis / HTTPX / asyncio integrations, configures a logging
breadcrumb sink, attaches release + version tags, and installs a
``before_send`` hook that scrubs the obvious secrets (``api_key``,
``token``, ``secret``) out of request URLs and payloads before they leave
the process.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from app.core.config import Settings

if TYPE_CHECKING:
    from sentry_sdk._types import Event, Hint

logger = logging.getLogger(__name__)

_SCRUB_KEYS = {"api_key", "token", "secret_key", "secret", "password", "authorization"}


def _resolve_release(settings: Settings) -> str | None:
    """Return the release identifier — explicit setting wins, else package version."""
    if settings.sentry_release:
        return settings.sentry_release
    try:
        return f"tgram-analytics-server@{version('tgram-analytics-server')}"
    except PackageNotFoundError:
        return None


def _scrub(value: Any) -> Any:
    """Recursively redact known-sensitive keys in dict/list payloads."""
    if isinstance(value, dict):
        return {
            k: ("[Filtered]" if k.lower() in _SCRUB_KEYS else _scrub(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _before_send(event: Event, _hint: Hint) -> Event | None:
    """Scrub api_key / secrets out of request data before the event is sent."""
    request = event.get("request")
    if isinstance(request, dict):
        data = request.get("data")
        if isinstance(data, (dict, list)):
            request["data"] = _scrub(data)
        query_string = request.get("query_string")
        if isinstance(query_string, str):
            # ``api_key=...`` shows up in querystring for SDK GET fallbacks;
            # rewrite the value rather than dropping the whole field so the
            # endpoint URL stays useful for grouping.
            parts = []
            for chunk in query_string.split("&"):
                key, sep, _ = chunk.partition("=")
                if key.lower() in _SCRUB_KEYS and sep:
                    parts.append(f"{key}=[Filtered]")
                else:
                    parts.append(chunk)
            request["query_string"] = "&".join(parts)
    return event


def _build_integrations() -> list[Any]:
    """Build the integration list, importing each lazily so a missing
    optional dep (e.g. ``redis`` not installed) does not break init."""
    integrations: list[Any] = []

    # Logging: WARNING+ → events, INFO+ → breadcrumbs by default.
    try:
        from sentry_sdk.integrations.logging import LoggingIntegration

        integrations.append(LoggingIntegration(level=logging.INFO, event_level=logging.WARNING))
    except ImportError:
        pass

    # FastAPI / Starlette — the SDK ships both; FastAPI extends Starlette.
    for module, cls_name in (
        ("sentry_sdk.integrations.fastapi", "FastApiIntegration"),
        ("sentry_sdk.integrations.starlette", "StarletteIntegration"),
        ("sentry_sdk.integrations.sqlalchemy", "SqlalchemyIntegration"),
        ("sentry_sdk.integrations.redis", "RedisIntegration"),
        ("sentry_sdk.integrations.httpx", "HttpxIntegration"),
        ("sentry_sdk.integrations.asyncio", "AsyncioIntegration"),
        ("sentry_sdk.integrations.asyncpg", "AsyncPGIntegration"),
    ):
        try:
            mod = __import__(module, fromlist=[cls_name])
            integrations.append(getattr(mod, cls_name)())
        except (ImportError, AttributeError):
            # The dep is not installed in this deployment — skip silently.
            continue

    return integrations


def init_sentry(settings: Settings) -> bool:
    """Initialise the Sentry SDK if configured. Returns ``True`` on success.

    Silently returns ``False`` when:
    - ``SENTRY_DSN`` is unset (default for OSS/self-host)
    - ``sentry-sdk`` is not installed (manually stripped from the env)
    - The SDK raises during init (logged at WARNING, never raised)
    """
    dsn = settings.sentry_dsn
    if not dsn:
        return False

    try:
        import sentry_sdk
    except ImportError:
        logger.warning(
            "SENTRY_DSN is set but sentry-sdk is not installed; "
            "install with `pip install sentry-sdk[fastapi]` to enable error tracking."
        )
        return False

    release = _resolve_release(settings)

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=settings.sentry_environment,
            release=release,
            server_name=settings.sentry_server_name,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            profiles_sample_rate=settings.sentry_profiles_sample_rate,
            send_default_pii=settings.sentry_send_default_pii,
            attach_stacktrace=True,
            include_local_variables=False,
            max_breadcrumbs=50,
            integrations=_build_integrations(),
            before_send=_before_send,
        )

        # Static tags applied to every event so dashboards can slice by
        # component without rummaging through breadcrumb context.
        sentry_sdk.set_tag("component", "server")
        if release:
            sentry_sdk.set_tag("release", release)
    except Exception:
        logger.warning("Sentry initialisation failed; continuing without it.", exc_info=True)
        return False

    logger.info(
        "Sentry initialised (env=%s, release=%s, traces=%.2f, profiles=%.2f)",
        settings.sentry_environment or "unset",
        release or "unset",
        settings.sentry_traces_sample_rate,
        settings.sentry_profiles_sample_rate,
    )
    return True
