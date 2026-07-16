"""Tests for ``mcp.docs.fetch.fetch_repo_readme``.

Coverage matrix:

- Happy path: 200 + ``ETag`` → returns dict, ``commit_sha`` populated,
  ``stale=False``.
- Cache hit: second call within TTL doesn't hit the network.
- Stale fallback: a successful first fetch followed by a failed
  second fetch (after TTL eviction) returns the cached body with
  ``stale=True``.
- Network error with no cached copy → re-raises.
- Missing ``ETag`` → ``commit_sha == ""`` (never fabricated).
- Unknown platform → ``KeyError``.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.mcp.docs import fetch as fetch_mod
from app.mcp.docs.fetch import fetch_repo_readme
from app.mcp.docs.sources import PLATFORM_SOURCES


@pytest.fixture(autouse=True)
def _reset_caches():
    """Each test gets a clean ``_CACHE`` and ``_STALE`` to avoid bleed."""
    fetch_mod._CACHE.clear()
    fetch_mod._STALE.clear()
    yield
    fetch_mod._CACHE.clear()
    fetch_mod._STALE.clear()


async def test_happy_path_with_etag():
    url = PLATFORM_SOURCES["js"]
    with respx.mock(assert_all_called=True) as router:
        router.get(url).mock(
            return_value=httpx.Response(
                200,
                text="# JS SDK\n\nHello",
                headers={"ETag": '"abc123"'},
            )
        )
        result = await fetch_repo_readme("js")
    assert result["markdown"].startswith("# JS SDK")
    assert result["source_url"] == url
    assert result["commit_sha"] == "abc123"
    assert result["stale"] is False
    assert "fetched_at" in result


async def test_cache_hit_skips_network():
    url = PLATFORM_SOURCES["python"]
    with respx.mock(assert_all_called=True) as router:
        route = router.get(url).mock(
            return_value=httpx.Response(200, text="hi", headers={"ETag": '"e1"'})
        )
        first = await fetch_repo_readme("python")
        second = await fetch_repo_readme("python")
    assert first["markdown"] == second["markdown"]
    # One network round-trip total, despite two calls.
    assert route.call_count == 1


async def test_stale_fallback_on_subsequent_failure():
    url = PLATFORM_SOURCES["flutter"]
    with respx.mock() as router:
        # First call: success — populates _CACHE and _STALE.
        router.get(url).mock(return_value=httpx.Response(200, text="ok", headers={"ETag": '"v1"'}))
        first = await fetch_repo_readme("flutter")
        assert first["stale"] is False

    # Evict the TTL cache to force a re-fetch on the next call.
    fetch_mod._CACHE.clear()

    with respx.mock() as router:
        router.get(url).mock(return_value=httpx.Response(500, text="boom"))
        second = await fetch_repo_readme("flutter")

    assert second["stale"] is True
    assert second["markdown"] == "ok"
    assert second["commit_sha"] == "v1"


async def test_failure_with_no_cache_raises():
    url = PLATFORM_SOURCES["server"]
    with respx.mock() as router:
        router.get(url).mock(return_value=httpx.Response(404, text="not found"))
        with pytest.raises(httpx.HTTPError):
            await fetch_repo_readme("server")


async def test_missing_etag_returns_empty_commit_sha():
    """We never fabricate ``commit_sha`` — empty string when ETag absent."""
    url = PLATFORM_SOURCES["js"]
    with respx.mock() as router:
        router.get(url).mock(return_value=httpx.Response(200, text="readme body"))
        result = await fetch_repo_readme("js")
    assert result["commit_sha"] == ""
    assert result["markdown"] == "readme body"


async def test_unknown_platform_raises_keyerror():
    with pytest.raises(KeyError):
        await fetch_repo_readme("cobol")


async def test_github_token_added_as_authorization_header():
    url = PLATFORM_SOURCES["js"]
    with respx.mock() as router:
        route = router.get(url).mock(
            return_value=httpx.Response(200, text="x", headers={"ETag": '"q"'})
        )
        await fetch_repo_readme("js", github_token="ghp_secret")
    assert route.calls[0].request.headers.get("Authorization") == "Bearer ghp_secret"
