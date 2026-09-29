"""Screenshot renderer for tap heatmaps.

A small HTTP service that loads one public web page in headless Chromium and
returns a full-page PNG. It runs as its own process (and container), never
inside the API server, so a browser crash or memory spike cannot take the
API down.

Endpoints:
  GET /health -> {"status": "ok"}
  GET /shot?url=<http(s) url>&width=390&dpr=2&max_height=6000 -> image/png
      Response headers: X-Final-Url, X-Login-Wall (0|1), X-Doc-Height,
      X-Doc-Width (CSS px of the whole document, before the height cap).

Limits: one screenshot at a time, Chromium launched per request and closed
after it, navigation timeout, pixel budget. The page host is resolved once,
checked, and pinned in Chromium with --host-resolver-rules. Sub-requests to
hosts that resolve to non-public addresses are blocked. This is not a full
SSRF boundary: see README.md, "Network guard".

Environment:
  RENDERER_TOKEN  optional; when set, /shot requires "Authorization: Bearer <token>"
  PORT            listen port (default 8080; read by the container CMD)
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Query, Response
from guard import (
    MAX_DOC_WIDTH,
    CheckedTarget,
    GuardError,
    clip_height,
    host_is_public,
    is_login_wall,
    launch_args,
    validate_target_url,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Request, Route, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger("renderer")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

NAV_TIMEOUT_MS = 20_000
NETWORK_IDLE_TIMEOUT_MS = 8_000
SETTLE_SECONDS = 1.0
QUEUE_TIMEOUT_SECONDS = 30.0
SHOT_TIMEOUT_SECONDS = 40.0
VIEWPORT_HEIGHT = 844

# Ingestion paths of the analytics server. Blocked so that a screenshot of a
# tracked site is not counted as a visit.
_BLOCKED_PATH_SUFFIXES = ("/api/v1/track", "/api/v1/pageview", "/api/v1/taps")

_one_at_a_time = asyncio.Semaphore(1)

app = FastAPI(title="tgram-analytics renderer", docs_url=None, redoc_url=None, openapi_url=None)


@dataclass
class _HostCheck:
    """Per-screenshot cache of host -> is-public, plus every URL requested."""

    cache: dict[tuple[str, int | None], bool] = field(default_factory=dict)
    seen_urls: set[str] = field(default_factory=set)

    async def allowed(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return False
        if parts.scheme in ("data", "blob", "about"):
            return True
        if parts.scheme not in ("http", "https", "ws", "wss"):
            return False
        host = parts.hostname or ""
        key = (host, port)
        if key not in self.cache:
            self.cache[key] = await asyncio.to_thread(host_is_public, host, port)
        return self.cache[key]


@dataclass
class _Shot:
    png: bytes
    final_url: str
    login_wall: bool
    doc_height: int
    doc_width: int


def _check_token(authorization: str | None) -> None:
    expected = os.environ.get("RENDERER_TOKEN", "")
    if not expected:
        return
    given = ""
    if authorization and authorization.lower().startswith("bearer "):
        given = authorization[7:].strip()
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing token")


async def _render(target: CheckedTarget, width: int, dpr: int, max_height: int) -> _Shot:
    url = target.url
    checks = _HostCheck()

    async def on_route(route: Route) -> None:
        req_url = route.request.url
        path = urlsplit(req_url).path
        if path.rstrip("/").endswith(_BLOCKED_PATH_SUFFIXES):
            await route.abort("blockedbyclient")
            return
        if await checks.allowed(req_url):
            await route.continue_()
        else:
            logger.warning("blocked non-public request host: %s", urlsplit(req_url).hostname)
            await route.abort("blockedbyclient")

    def on_request(request: Request) -> None:
        # Fires for redirect hops too, which route() does not see.
        checks.seen_urls.add(request.url)

    async with async_playwright() as p:
        # Launched per request with the page host pinned to the checked address.
        browser = await p.chromium.launch(headless=True, args=launch_args(target))
        try:
            context = await browser.new_context(
                viewport={"width": width, "height": VIEWPORT_HEIGHT},
                device_scale_factor=dpr,
                is_mobile=width < 768,
                has_touch=width < 1024,
                service_workers="block",
                accept_downloads=False,
            )
            # Context-wide, so any page the site opens goes through the guard.
            context.on("request", on_request)
            await context.route("**/*", on_route)
            page = await context.new_page()
            page.on("websocket", lambda ws: checks.seen_urls.add(ws.url))

            def on_new_page(extra: Page) -> None:
                # Popups are also blocked by --block-new-web-contents.
                if extra is not page:
                    asyncio.ensure_future(extra.close())

            context.on("page", on_new_page)

            try:
                await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            except PlaywrightTimeoutError as exc:
                raise HTTPException(status_code=504, detail="navigation timeout") from exc
            # Pages with long-polling never go idle; the load event is enough.
            with contextlib.suppress(PlaywrightTimeoutError):
                await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
            await asyncio.sleep(SETTLE_SECONDS)

            dims = await page.evaluate(
                "() => [document.documentElement.scrollWidth,"
                " Math.max(document.documentElement.scrollHeight,"
                " document.body ? document.body.scrollHeight : 0)]"
            )
            doc_width = max(width, min(int(dims[0]), MAX_DOC_WIDTH))
            doc_height = max(1, int(dims[1]))
            has_password = await page.locator("input[type=password]").count() > 0
            final_url = page.url

            shot_height = clip_height(doc_width, doc_height, dpr, max_height)
            png = await page.screenshot(
                full_page=True,
                type="png",
                clip={"x": 0, "y": 0, "width": doc_width, "height": shot_height},
            )

            # Refuse the result when anything the page loaded (redirect hops
            # included) is on a host that now resolves to a non-public address.
            # This uses the renderer's own DNS lookup, not the address the
            # browser connected to; the page host itself is pinned at launch.
            for seen in sorted(checks.seen_urls):
                if not await checks.allowed(seen):
                    raise HTTPException(
                        status_code=403, detail="page requested a non-public address"
                    )
        finally:
            await browser.close()

    return _Shot(
        png=png,
        final_url=final_url,
        login_wall=is_login_wall(url, final_url, has_password),
        doc_height=doc_height,
        doc_width=doc_width,
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/shot")
async def shot(
    url: str = Query(..., max_length=4096),
    width: int = Query(390, ge=240, le=2000),
    dpr: int = Query(2, ge=1, le=3),
    max_height: int = Query(6000, ge=200, le=20000),
    authorization: str | None = Header(default=None),
) -> Response:
    _check_token(authorization)
    try:
        target = await asyncio.to_thread(validate_target_url, url)
    except GuardError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    try:
        await asyncio.wait_for(_one_at_a_time.acquire(), timeout=QUEUE_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="renderer busy") from exc
    try:
        result = await asyncio.wait_for(
            _render(target, width, dpr, max_height), timeout=SHOT_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="screenshot timeout") from exc
    except PlaywrightError as exc:
        logger.warning("screenshot failed for %s: %s", urlsplit(url).hostname, exc)
        raise HTTPException(status_code=502, detail="page could not be rendered") from exc
    finally:
        _one_at_a_time.release()

    return Response(
        content=result.png,
        media_type="image/png",
        headers={
            "X-Final-Url": result.final_url,
            "X-Login-Wall": "1" if result.login_wall else "0",
            "X-Doc-Height": str(result.doc_height),
            "X-Doc-Width": str(result.doc_width),
            "Cache-Control": "no-store",
        },
    )
