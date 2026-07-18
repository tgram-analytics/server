"""Render copy-paste SDK init snippets for ``get_sdk_snippet``.

Phase 6 backend for the snippet tool. Loads Jinja templates from
``snippets/`` (one per platform), enforces project ownership before
exposing anything tied to the user's account, then renders the
template with three variables: ``api_key``, ``server_url``,
``package_name``.

Security note — the key we render is the OSS *placeholder*
``<YOUR_API_KEY>``, not the real key. The OSS schema only stores
``api_key_hash`` (see ``server/app/models/project.py``); the raw key
is generated once at project create and never persisted. We still
require the ownership check before rendering so the snippet can
safely include project-scoped metadata in future revisions (e.g.
project name) without re-auditing this surface. Returning a key
shaped like ``<YOUR_API_KEY>`` matches the bot's onboarding flow
(``server/app/bot/handlers/onboarding.py``) so docs are consistent.

Autoescape is **off**. These templates are source code (JS / Python /
Dart), never HTML — the rendered string is returned verbatim in a JSON
MCP tool result for the caller to copy-paste. With autoescape on, the
``<YOUR_API_KEY>`` placeholder rendered as ``&lt;YOUR_API_KEY&gt;``,
which broke the snippet the tool exists to produce. The only templated
value a caller can influence is ``api_key`` — their own key, and only
in their own output — so escaping buys no security here.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.auth import assert_project_owned_by

# Per-platform template names. Keep keys aligned with
# ``mcp.tools.setup._SUPPORTED_PLATFORMS`` and ``docs.sources``.
_TEMPLATES: dict[str, str] = {
    "js": "js.jinja2",
    "python": "python.jinja2",
    "flutter": "flutter.jinja2",
}

# Per-platform pip / npm / pub.dev package name. The Python and
# Dart packages substitute ``-`` → ``_`` inside the template since
# import paths use the underscored name.
_PACKAGE_NAMES: dict[str, str] = {
    "js": "tgram-analytics",
    "python": "tgram-analytics",
    "flutter": "tgram_analytics",
}

_SNIPPETS_DIR = Path(__file__).parent / "snippets"

# Last-resort ingestion URL, used only when settings cannot be loaded
# at all. Never a substitute for the instance's own configured base URL
# — see ``_resolve_server_url``.
_FALLBACK_SERVER_URL = "https://api.tgram-analytics.com"

# Single shared environment — Jinja templates are reloaded only when
# the file mtime changes, and we never write to them at runtime.
_env = Environment(
    loader=FileSystemLoader(str(_SNIPPETS_DIR)),
    autoescape=False,  # templates are code, not HTML — see module docstring
    keep_trailing_newline=True,
)


def _resolve_server_url() -> str:
    """Return the public ingestion URL the SDK should point at.

    This must be **this instance's own** public base URL, not a
    canonical hosted one. Projects and API keys are per-instance: a
    key minted by the MCP running on ``https://tga.example.com``
    is only valid against ``https://tga.example.com/api/v1/...``.
    Handing out a snippet pointing anywhere else produces a silently
    broken install — the SDK posts events and every one is rejected
    with ``400 {"detail":"Invalid API key"}``.

    We reuse :attr:`Settings.mcp_effective_public_url` — the same
    resolved base URL the MCP already advertises in its OAuth
    issuer/resource metadata (``MCP_PUBLIC_URL`` → ``WEBHOOK_BASE_URL``
    → ``http://localhost:8000``). The MCP surface and the ingestion API
    are served by the same app off the same origin, so whatever host
    the caller reached ``/mcp`` on is exactly the host that owns their
    project.

    If settings cannot be loaded at all (CI without env vars), we fall
    back to the canonical hosted URL — the snippet is a copy-paste
    onboarding aid and a stable default beats a 500.
    """
    try:
        from app.core.config import get_settings

        settings: Any = get_settings()
    except Exception:
        return _FALLBACK_SERVER_URL
    url = getattr(settings, "mcp_effective_public_url", "") or ""
    return url.rstrip("/") or _FALLBACK_SERVER_URL


async def render_snippet(
    session: AsyncSession,
    platform: str,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    api_key: str | None = None,
) -> str:
    """Render and return the SDK init snippet string for *platform*.

    Calls :func:`assert_project_owned_by` first — this enforces the
    same ownership boundary every other project-scoped MCP tool
    relies on; without it a token holder could ask for a snippet
    against any project_id and we'd happily render one. The check
    raises :class:`ProjectNotOwnedError` for the caller to translate
    at the tool boundary.

    When *api_key* is ``None`` (the default), the snippet is rendered
    with the ``<YOUR_API_KEY>`` placeholder so it can be safely shown
    in docs / shared transcripts. When supplied (typically the value
    returned by ``rotate_api_key``), that value is spliced verbatim
    into the template — we do **not** validate it against the project
    here, the caller is responsible for the rotate→render handshake.

    Raises:
        KeyError: if *platform* has no registered template.
        ProjectNotOwnedError: if *user_id* does not own *project_id*.
    """
    if platform not in _TEMPLATES:
        raise KeyError(platform)

    # Ownership gate. We don't actually consume any project field
    # today (api_key isn't stored as plaintext — see module docstring),
    # but we still want the boundary in place so future revisions of
    # the template can safely surface project metadata.
    await assert_project_owned_by(session, project_id, user_id)

    template = _env.get_template(_TEMPLATES[platform])
    return template.render(
        api_key=api_key if api_key is not None else "<YOUR_API_KEY>",
        server_url=_resolve_server_url(),
        package_name=_PACKAGE_NAMES[platform],
    )
