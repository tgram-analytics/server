"""Tests for app/services/heatmap.py (no database, no browser)."""

import io

import httpx
import pytest
import respx
from PIL import Image

from app.core.config import Settings
from app.models.project import Project
from app.services.heatmap import (
    Screenshot,
    build_site_url,
    draw_heat_layer,
    fetch_screenshot,
    render_heatmap,
    screenshot_params,
    slugify_path,
)

RENDERER = "http://renderer.test:8080"


def _settings(**overrides) -> Settings:
    values = {
        "telegram_bot_token": "1234567890:test-token-for-testing-only",
        "admin_chat_id": 1,
        "database_url": "postgresql+asyncpg://tga:x@localhost/tga_test",
        "secret_key": "test",
        "screenshot_url": RENDERER,
        "screenshot_token": "",
    }
    values.update(overrides)
    return Settings(**values)


def _png(width: int, height: int, colour=(255, 255, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (width, height), colour).save(buf, format="PNG")
    return buf.getvalue()


# ── draw_heat_layer ─────────────────────────────────────────────────────────


def test_draw_heat_layer_marks_points():
    width, height = 400, 800
    radius = max(18, width // 26)
    points = [(0.25, 100), (0.75, 400), (0.5, 700)]
    out = Image.open(io.BytesIO(draw_heat_layer(_png(width, height), points, dpr=1)))
    out = out.convert("RGB")

    # Hottest pixel = the one that moved furthest from the white background.
    def change(xy):
        r, g, b = out.getpixel(xy)
        return (255 - r) + (255 - g) + (255 - b)

    best = max(((x, y) for x in range(0, width, 2) for y in range(0, height, 2)), key=change)
    centres = [(int(x * (width - 1)), y) for x, y in points]
    assert any(
        ((best[0] - cx) ** 2 + (best[1] - cy) ** 2) ** 0.5 <= radius for cx, cy in centres
    ), f"hottest pixel {best} is not near any point {centres}"
    for cx, cy in centres:
        assert change((cx, cy)) > 0

    # Far from every point the page is unchanged.
    assert out.getpixel((width - 1, 0)) == (255, 255, 255)
    assert out.getpixel((0, height // 2 + 50)) == (255, 255, 255)


def test_draw_heat_layer_returns_png_same_size():
    png = _png(390 * 2, 1200)
    data = draw_heat_layer(png, [(0.5, 100), (0.1, 5000)], dpr=2)
    assert data.startswith(b"\x89PNG")
    assert Image.open(io.BytesIO(data)).size == (780, 1200)


def test_draw_heat_layer_scales_y_by_dpr():
    # y=150 CSS px at DPR 2 lands at pixel row 300, not 150.
    out = Image.open(io.BytesIO(draw_heat_layer(_png(200, 600), [(0.5, 150)], dpr=2)))
    out = out.convert("RGB")
    assert out.getpixel((99, 300)) != (255, 255, 255)
    assert out.getpixel((99, 150)) == (255, 255, 255)


def test_draw_heat_layer_without_points_is_unchanged():
    out = Image.open(io.BytesIO(draw_heat_layer(_png(50, 50), [], dpr=1))).convert("RGB")
    assert out.getextrema() == ((255, 255), (255, 255), (255, 255))


async def test_render_heatmap_uses_screenshot_dpr():
    shot = Screenshot(
        png=_png(100, 100), final_url="https://a.example/", login_wall=False,
        doc_height=50, width=50, dpr=2,
    )  # fmt: skip
    data = await render_heatmap(shot, [(0.5, 25)])
    out = Image.open(io.BytesIO(data)).convert("RGB")
    assert out.getpixel((49, 50)) != (255, 255, 255)


# ── build_site_url / slug / params ──────────────────────────────────────────


def test_build_site_url_prefers_allowlist_host():
    project = Project(name="My shop", domain_allowlist=["https://Shop.Example.com/", "b.example"])
    assert build_site_url(project, "/browse?x=1") == "https://shop.example.com/browse?x=1"


def test_build_site_url_skips_wildcards():
    project = Project(name="x", domain_allowlist=["*.example.com", "localhost:5173", "app.example"])
    assert build_site_url(project, "/") == "https://app.example/"


def test_build_site_url_falls_back_to_hostname_name():
    project = Project(name="Example.org", domain_allowlist=["*.example.org"])
    assert build_site_url(project, "pricing") == "https://example.org/pricing"


def test_build_site_url_none_when_unknown():
    assert build_site_url(Project(name="My app", domain_allowlist=[]), "/") is None
    assert build_site_url(Project(name="demo", domain_allowlist=["*.a.example"]), "/") is None


def test_build_site_url_path_cannot_change_host():
    project = Project(name="x", domain_allowlist=["site.example"])
    assert build_site_url(project, "@evil.example/") == "https://site.example/@evil.example/"


def test_slugify_path():
    assert slugify_path("/") == "home"
    assert slugify_path("/browse/albums/42?sort=New") == "browse-albums-42-sort-new"
    assert len(slugify_path("/" + "a" * 200)) <= 60


def test_screenshot_params_clamp_to_bucket():
    assert screenshot_params("mobile", None) == (390, 2)
    assert screenshot_params("mobile", 1200) == (767, 2)
    assert screenshot_params("tablet", 800) == (800, 2)
    assert screenshot_params("desktop", 3000) == (1600, 1)
    assert screenshot_params("desktop", None) == (1280, 1)


# ── fetch_screenshot ────────────────────────────────────────────────────────


async def test_fetch_screenshot_returns_none_when_unconfigured():
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__startswith="http").mock(return_value=httpx.Response(500))
        result = await fetch_screenshot(
            "https://a.example/", "mobile", settings=_settings(screenshot_url="")
        )
    assert result is None
    assert not route.called


async def test_fetch_screenshot_parses_login_wall_header():
    png = _png(10, 10)
    with respx.mock(assert_all_called=True) as router:
        route = router.get(f"{RENDERER}/shot").mock(
            return_value=httpx.Response(
                200,
                content=png,
                headers={
                    "content-type": "image/png",
                    "X-Final-Url": "https://a.example/login",
                    "X-Login-Wall": "1",
                    "X-Doc-Height": "900",
                },
            )
        )
        result = await fetch_screenshot(
            "https://a.example/dashboard",
            "mobile",
            412,
            settings=_settings(screenshot_token="s3cret"),
        )
    assert result is not None
    assert result.login_wall is True
    assert result.final_url == "https://a.example/login"
    assert result.doc_height == 900
    assert (result.width, result.dpr) == (412, 2)
    assert result.png == png
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer s3cret"
    assert request.url.params["url"] == "https://a.example/dashboard"
    assert request.url.params["width"] == "412"
    assert request.url.params["dpr"] == "2"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401),
        httpx.Response(504),
        httpx.Response(200, content=b"<html>not a png</html>"),
    ],
)
async def test_fetch_screenshot_returns_none_on_bad_response(response):
    with respx.mock() as router:
        router.get(f"{RENDERER}/shot").mock(return_value=response)
        assert await fetch_screenshot("https://a.example/", "desktop", settings=_settings()) is None


async def test_fetch_screenshot_returns_none_on_network_error():
    with respx.mock() as router:
        router.get(f"{RENDERER}/shot").mock(side_effect=httpx.ConnectTimeout("down"))
        assert await fetch_screenshot("https://a.example/", "tablet", settings=_settings()) is None


async def test_fetch_screenshot_returns_none_for_unknown_device():
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{RENDERER}/shot").mock(return_value=httpx.Response(200))
        assert await fetch_screenshot("https://a.example/", "all", settings=_settings()) is None
    assert not route.called


def test_draw_heat_layer_single_point_is_red_at_centre():
    """The hottest spot always reaches the top of the colour scale, even alone."""
    width, height = 400, 800
    out = Image.open(io.BytesIO(draw_heat_layer(_png(width, height), [(0.5, 300)], dpr=1)))
    r, g, b = out.convert("RGB").getpixel((int(0.5 * (width - 1)), 300))
    assert r > 200 and g < 100 and b < 100
