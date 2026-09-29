"""Tap heatmap images: page screenshot from the renderer + heat layer.

The screenshot comes from the separate renderer service (``renderer/``),
reached over HTTP at ``Settings.screenshot_url``. This module never starts a
browser. The heat layer is drawn in-process with Pillow.

Every public function degrades softly: :func:`fetch_screenshot` returns
``None`` on any failure (or when no renderer is configured) so callers can
fall back to a text-only answer.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import random
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageChops, ImageFilter

from app.core.config import Settings, get_settings
from app.models.project import Project
from app.services.events import normalize_origin_entry

logger = logging.getLogger(__name__)

# ── Device presets ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DevicePreset:
    """Screenshot viewport rules for one device bucket (CSS px)."""

    min_width: int
    max_width: int
    default_width: int
    dpr: int


DEVICE_PRESETS: dict[str, DevicePreset] = {
    "mobile": DevicePreset(min_width=320, max_width=767, default_width=390, dpr=2),
    "tablet": DevicePreset(min_width=768, max_width=1023, default_width=820, dpr=2),
    "desktop": DevicePreset(min_width=1024, max_width=1600, default_width=1280, dpr=1),
}

MAX_SCREENSHOT_HEIGHT = 6000  # CSS px
MAX_POINTS = 5000


def screenshot_params(device: str, median_vw: int | None) -> tuple[int, int]:
    """Return ``(width, dpr)`` for a device bucket.

    ``median_vw`` is the median viewport width of the taps being drawn; it is
    clamped to the bucket's range. ``None`` uses the bucket default.
    """
    preset = DEVICE_PRESETS[device]
    if median_vw is None:
        return preset.default_width, preset.dpr
    width = min(max(int(median_vw), preset.min_width), preset.max_width)
    return width, preset.dpr


# ── Site URL ────────────────────────────────────────────────────────────────

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def _is_screenshot_host(host: str) -> bool:
    try:
        bare = urlsplit(f"//{host}").hostname or ""
    except ValueError:
        return False
    if not bare or bare == "localhost" or bare.endswith(".localhost"):
        return False
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        return True
    return False  # IP literals are not a site the renderer will load


def build_site_url(project: Project, path: str) -> str | None:
    """Return the absolute page URL to screenshot, or ``None`` when unknown.

    Uses the first non-wildcard host in the project's domain allowlist, else
    the project name when it looks like a hostname (``example.com``).
    """
    if not path.startswith("/"):
        path = "/" + path
    for entry in project.domain_allowlist or []:
        host = normalize_origin_entry(str(entry))
        if host is None or host.startswith("*.") or not _is_screenshot_host(host):
            continue
        return f"https://{host}{path}"
    name = (project.name or "").strip().lower()
    if _HOSTNAME_RE.match(name):
        return f"https://{name}{path}"
    return None


def slugify_path(path: str) -> str:
    """Filename-safe slug for a page path: ``/browse/a?b=1`` -> ``browse-a-b-1``."""
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
    return slug[:60].rstrip("-") or "home"


# ── Screenshot ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Screenshot:
    """A full-page PNG returned by the renderer."""

    png: bytes
    final_url: str
    login_wall: bool
    doc_height: int  # CSS px of the whole document (before the height cap)
    width: int  # viewport width in CSS px
    dpr: int


async def fetch_screenshot(
    url: str,
    device: str,
    median_vw: int | None = None,
    *,
    settings: Settings | None = None,
) -> Screenshot | None:
    """Ask the renderer for a screenshot of ``url`` for a device bucket.

    Returns ``None`` when no renderer is configured, the device is unknown,
    or anything fails (network, timeout, non-200, not a PNG).
    """
    settings = settings or get_settings()
    base = settings.screenshot_url.strip().rstrip("/")
    if not base:
        return None
    if device not in DEVICE_PRESETS:
        logger.info("heatmap screenshot skipped: unknown device %r", device)
        return None

    width, dpr = screenshot_params(device, median_vw)
    headers = {}
    if settings.screenshot_token:
        headers["Authorization"] = f"Bearer {settings.screenshot_token}"
    params: dict[str, str | int] = {
        "url": url,
        "width": width,
        "dpr": dpr,
        "max_height": MAX_SCREENSHOT_HEIGHT,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.screenshot_timeout_seconds) as client:
            resp = await client.get(f"{base}/shot", params=params, headers=headers)
        if resp.status_code != 200:
            logger.warning("renderer returned HTTP %s for a screenshot", resp.status_code)
            return None
        if not resp.content.startswith(b"\x89PNG"):
            logger.warning("renderer response is not a PNG")
            return None
        return Screenshot(
            png=resp.content,
            final_url=resp.headers.get("X-Final-Url", url),
            login_wall=resp.headers.get("X-Login-Wall", "0") == "1",
            doc_height=int(resp.headers.get("X-Doc-Height", "0") or 0),
            width=width,
            dpr=dpr,
        )
    except Exception:
        logger.warning("screenshot request failed", exc_info=True)
        return None


# ── Heat layer ──────────────────────────────────────────────────────────────

_BLOB_PEAK = 56
_ALPHA_MAX = 200  # keep the page readable under the hottest spots
_NOISE_FLOOR = 2  # blur tails below this stay fully transparent

# Colour stops over the normalised heat value 0..1.
_STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.0, (0, 0, 255)),
    (0.35, (0, 200, 80)),
    (0.65, (255, 230, 0)),
    (1.0, (230, 0, 0)),
)


def _blob(radius: int) -> Image.Image:
    size = 2 * radius + 1
    data = []
    for yy in range(size):
        for xx in range(size):
            d = ((xx - radius) ** 2 + (yy - radius) ** 2) ** 0.5 / radius
            data.append(int(round(_BLOB_PEAK * (1.0 - d) ** 2)) if d < 1.0 else 0)
    img = Image.new("L", (size, size), 0)
    img.putdata(data)
    return img


def _colour_at(t: float) -> tuple[int, int, int]:
    for (t0, c0), (t1, c1) in zip(_STOPS, _STOPS[1:], strict=False):
        if t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return (
                int(c0[0] + (c1[0] - c0[0]) * f),
                int(c0[1] + (c1[1] - c0[1]) * f),
                int(c0[2] + (c1[2] - c0[2]) * f),
            )
    return _STOPS[-1][1]


def _luts(peak: int) -> tuple[list[int], list[int], list[int], list[int]]:
    """256-entry lookup tables mapping a heat value to R, G, B, A."""
    r, g, b, a = [], [], [], []
    for v in range(256):
        t = min(1.0, v / peak) if peak else 0.0
        cr, cg, cb = _colour_at(t)
        r.append(cr)
        g.append(cg)
        b.append(cb)
        # Square root keeps single, isolated taps visible next to hot spots.
        a.append(0 if v < _NOISE_FLOOR else min(_ALPHA_MAX, int(255 * t**0.5)))
    return r, g, b, a


def draw_heat_layer(png: bytes, points: list[tuple[float, int]], dpr: int = 1) -> bytes:
    """Draw tap points over a screenshot and return a PNG of the same size.

    ``points`` are ``(x, y)`` with ``x`` a 0..1 fraction of the document
    width and ``y`` CSS px from the top of the document. ``dpr`` is the
    screenshot's device pixel ratio. Points below the bottom of the image
    (page shorter than when tapped, or height cap) are skipped. CPU-bound:
    call it through ``asyncio.to_thread`` (see :func:`render_heatmap`).
    """
    shot = Image.open(io.BytesIO(png)).convert("RGBA")
    width, height = shot.size
    radius = max(18, width // 26)
    blob = _blob(radius)

    if len(points) > MAX_POINTS:
        points = random.Random(0).sample(points, MAX_POINTS)

    heat = Image.new("L", (width, height), 0)
    for x_frac, y_css in points:
        px = int(min(max(float(x_frac), 0.0), 1.0) * (width - 1))
        py = int(y_css) * dpr
        if py < 0 or py >= height:
            continue
        box = (px - radius, py - radius, px + radius + 1, py + radius + 1)
        heat.paste(ImageChops.add(heat.crop(box), blob), box)

    heat = heat.filter(ImageFilter.GaussianBlur(radius / 2))
    high = heat.getextrema()[1]  # (min, max) for a single-band image
    peak = 0 if isinstance(high, tuple) else int(high)
    lut_r, lut_g, lut_b, lut_a = _luts(peak)
    layer = Image.merge(
        "RGBA",
        (heat.point(lut_r), heat.point(lut_g), heat.point(lut_b), heat.point(lut_a)),
    )
    out = Image.alpha_composite(shot, layer)
    buf = io.BytesIO()
    # Level 1: encoding dominates the run time on tall pages (measured ~4x faster
    # than level 6 for a 780x11138 image) for ~20% larger files.
    out.save(buf, format="PNG", optimize=False, compress_level=1)
    return buf.getvalue()


async def render_heatmap(shot: Screenshot, points: list[tuple[float, int]]) -> bytes:
    """Draw ``points`` on ``shot`` off the event loop."""
    return await asyncio.to_thread(draw_heat_layer, shot.png, points, shot.dpr)
