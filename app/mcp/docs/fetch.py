"""TTL-cached GitHub README fetcher with stale-on-error fallback.

Phase 6 implementation backing ``get_integration_guide``. The README
files for each SDK live in their own public GitHub repos
(:mod:`sources`); we fetch them on demand, cache for 5 minutes, and
fall back to the previous cached body if a subsequent fetch fails so
a transient GitHub outage doesn't blow up the MCP tool call.

Design notes:

- We use the ``ETag`` response header as ``commit_sha`` for cheap
  drift detection. GitHub raw content sets it to the blob OID of the
  served file. If the header is missing — older endpoints, proxies
  that strip headers, etc. — we report ``commit_sha = ""`` rather
  than fabricating a value. The empty string lets clients detect
  "version unknown" without breaking equality checks.
- The cache is process-local (``cachetools.TTLCache``). For a
  multi-worker deploy each gunicorn worker carries its own copy;
  this is acceptable because the readme files are small (~few KB)
  and the upstream is GitHub (which has its own CDN). A shared
  Redis cache would only help once we run >10 workers behind a
  single domain.
- ``maxsize=100`` is wildly more than the four supported platforms,
  but leaves room for per-version or per-locale keying later
  without re-tuning.
- On HTTP error WITH a stale cached copy, we return that copy with
  ``stale=True`` so a degraded experience beats a blocked tool call.
  On HTTP error WITHOUT a cached copy, we re-raise so the tool
  layer can surface ``isError=True`` to the client.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from cachetools import TTLCache

from app.mcp.docs.sources import PLATFORM_SOURCES

logger = logging.getLogger("app.mcp.docs")

# Module-level cache so repeated calls within the TTL window hit
# memory instead of GitHub. ``maxsize=100`` is generous for the
# current ~4 platforms; ``ttl=300`` (5 min) balances freshness vs
# rate-limit pressure on unauthenticated GitHub raw requests.
_CACHE: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=100, ttl=300)

# Stale fallback. ``_CACHE`` evicts on TTL; ``_STALE`` keeps the last
# successful payload around so we can serve it after a GitHub
# failure. Bounded by the same logical key set so a flaky platform
# can't grow ``_STALE`` unbounded.
_STALE: dict[str, dict[str, Any]] = {}


async def fetch_repo_readme(
    platform: str,
    *,
    github_token: str | None = None,
) -> dict[str, Any]:
    """Fetch the README for *platform* from its public GitHub raw URL.

    Returns a dict with keys:

    - ``markdown`` — full file body
    - ``source_url`` — the URL we fetched
    - ``commit_sha`` — value of the response ``ETag`` header, or
      ``""`` if the server didn't send one (we never fabricate)
    - ``fetched_at`` — ISO-8601 UTC timestamp of the fetch
    - ``stale`` — ``True`` if we're serving a cached copy because
      the live fetch failed; ``False`` on a fresh hit

    *github_token*, when supplied, is sent as
    ``Authorization: Bearer <token>``. The unauthenticated rate
    limit (60 req/h per IP) is normally fine because of the cache,
    but cloud deploys with bursty traffic should set
    ``CloudSettings.mcp_github_token``.

    Raises:
        KeyError: if *platform* is not in :data:`PLATFORM_SOURCES`.
        httpx.HTTPError: on a network/HTTP error WITHOUT a cached
            stale copy to fall back on.
    """
    if platform not in PLATFORM_SOURCES:
        raise KeyError(platform)

    cached = _CACHE.get(platform)
    if cached is not None:
        return cached

    source_url = PLATFORM_SOURCES[platform]
    headers: dict[str, str] = {"Accept": "text/plain, */*"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(source_url, headers=headers)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        stale_copy = _STALE.get(platform)
        if stale_copy is not None:
            logger.warning(
                "fetch_repo_readme(%s) failed (%s); serving stale copy",
                platform,
                exc,
            )
            # Return a copy so callers can't mutate the stale store.
            return {**stale_copy, "stale": True}
        logger.warning(
            "fetch_repo_readme(%s) failed and no stale copy available",
            platform,
        )
        raise

    # ``ETag`` is the GitHub raw blob OID — perfect for drift detection.
    # We never invent a value if the header is missing.
    etag = response.headers.get("ETag", "")
    commit_sha = etag.strip('"') if etag else ""

    payload: dict[str, Any] = {
        "markdown": response.text,
        "source_url": source_url,
        "commit_sha": commit_sha,
        "fetched_at": datetime.now(UTC).isoformat(),
        "stale": False,
    }
    _CACHE[platform] = payload
    _STALE[platform] = payload
    return payload
