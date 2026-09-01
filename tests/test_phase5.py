"""Phase 5 — Chart generation tests.

Charts are now rendered in-process via Altair + vl-convert (no external
HTTP service).  Tests assert that the renderer returns valid PNG bytes
and that Vega-Lite specs include the expected titles/series.
"""

import struct
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_SAMPLE_DATA = [
    {"bucket": datetime(2024, 1, 1, tzinfo=UTC), "count": 10},
    {"bucket": datetime(2024, 1, 2, tzinfo=UTC), "count": 20},
    {"bucket": datetime(2024, 1, 3, tzinfo=UTC), "count": 5},
]


# ── generate_line_chart ───────────────────────────────────────────────────


async def test_generate_line_chart_returns_valid_png():
    from app.services.charts import generate_line_chart

    result = await generate_line_chart(
        _SAMPLE_DATA,
        title="signup",
        period_label="30d",
    )
    assert isinstance(result, bytes)
    assert result[:8] == _PNG_MAGIC


async def test_generate_line_chart_spec_includes_title():
    """Title text propagates into the Vega-Lite spec passed to vl-convert."""
    from app.services import charts as charts_mod

    captured: dict = {}

    def _capture(spec, scale):  # noqa: ANN001
        captured["spec"] = spec
        return _PNG_MAGIC + b"\x00" * 32

    with patch.object(charts_mod.vlc, "vegalite_to_png", side_effect=_capture):
        await charts_mod.generate_line_chart(_SAMPLE_DATA, title="my_event", period_label="7d")

    spec_str = str(captured["spec"])
    assert "my_event" in spec_str


async def test_generate_line_chart_empty_data_does_not_raise():
    from app.services.charts import generate_line_chart

    result = await generate_line_chart([], title="no_data", period_label="7d")
    assert isinstance(result, bytes)
    assert result[:8] == _PNG_MAGIC


# ── generate_comparison_chart ─────────────────────────────────────────────


async def test_generate_comparison_chart_returns_valid_png():
    from app.services.charts import generate_comparison_chart

    data_b = [
        {"bucket": datetime(2024, 1, 1, tzinfo=UTC), "count": 3},
        {"bucket": datetime(2024, 1, 2, tzinfo=UTC), "count": 8},
    ]
    result = await generate_comparison_chart(
        _SAMPLE_DATA, data_b, label_a="this week", label_b="last week"
    )
    assert result[:8] == _PNG_MAGIC


async def test_generate_comparison_chart_spec_has_two_series():
    from app.services import charts as charts_mod

    captured: dict = {}

    def _capture(spec, scale):  # noqa: ANN001
        captured["spec"] = spec
        return _PNG_MAGIC + b"\x00" * 32

    data_b = [{"bucket": datetime(2024, 1, 1, tzinfo=UTC), "count": 2}]
    with patch.object(charts_mod.vlc, "vegalite_to_png", side_effect=_capture):
        await charts_mod.generate_comparison_chart(_SAMPLE_DATA, data_b, label_a="A", label_b="B")

    spec_str = str(captured["spec"])
    assert "'A'" in spec_str or '"A"' in spec_str
    assert "'B'" in spec_str or '"B"' in spec_str


# ── generate_bar_chart / pie / funnel ─────────────────────────────────────


async def test_generate_bar_chart_returns_valid_png():
    from app.services.charts import generate_bar_chart

    data = [{"value": "google.com", "count": 50}, {"value": "(direct)", "count": 12}]
    result = await generate_bar_chart(data, title="Top referrers")
    assert result[:8] == _PNG_MAGIC


async def test_generate_pie_chart_returns_valid_png():
    from app.services.charts import generate_pie_chart

    data = [{"source": "Google", "count": 10}, {"source": "Twitter", "count": 5}]
    result = await generate_pie_chart(data, title="Sources")
    assert result[:8] == _PNG_MAGIC


async def test_generate_pie_chart_spec_carries_pct_and_legend_counts():
    """Slice labels carry the % and legend entries carry the raw count."""
    from app.services import charts as charts_mod

    captured: dict = {}

    def _capture(spec, scale):  # noqa: ANN001
        captured["spec"] = spec
        return _PNG_MAGIC + b"\x00" * 32

    data = [{"source": "Google", "count": 10}, {"source": "Twitter", "count": 5}]
    with patch.object(charts_mod.vlc, "vegalite_to_png", side_effect=_capture):
        await charts_mod.generate_pie_chart(data, title="Sources")

    spec_str = str(captured["spec"])
    # Percent labels on slices (10/15 ≈ 66.7%, 5/15 ≈ 33.3%).
    assert "66.7%" in spec_str
    assert "33.3%" in spec_str
    # Legend entries embed the raw count alongside the label.
    assert "Google · 10" in spec_str
    assert "Twitter · 5" in spec_str
    # Legend is sorted by count (descending) so it lines up with slice order.
    assert "'order': 'descending'" in spec_str or '"order": "descending"' in spec_str


async def test_generate_funnel_chart_returns_valid_png():
    from app.services.charts import generate_funnel_chart

    data = [
        {"step": "view", "count": 100},
        {"step": "click", "count": 40},
        {"step": "buy", "count": 8},
    ]
    result = await generate_funnel_chart(data, title="Funnel")
    assert result[:8] == _PNG_MAGIC


# ── funnel readability regressions ────────────────────────────────────────

_LONG_FUNNEL = [
    {"step": "signup", "count": 202},
    {"step": "onboarding_completed", "count": 131},
    {"step": "app_opened", "count": 6},
    {"step": "app_upload_started", "count": 5},
    {"step": "app_mode_chosen", "count": 1},
    {"step": "app_model_chosen", "count": 1},
    {"step": "paywall_shown", "count": 1},
    {"step": "checkout_redirected", "count": 0},
    {"step": "app_generate_clicked", "count": 0},
    {"step": "app_job_done", "count": 0},
]


def _png_size(png: bytes) -> tuple[int, int]:
    """Width/height in pixels, read from the PNG IHDR chunk."""
    return struct.unpack(">II", png[16:24])


async def test_funnel_chart_canvas_is_not_inflated_by_a_long_title():
    """A long title must wrap, not stretch the canvas sideways.

    Regression: titles were passed to Vega-Lite as one unbroken line, so a
    funnel named after all ten of its steps blew the canvas out to 5024px
    while the plot stayed 720px — the chart became an unreadable sliver in
    the corner.
    """
    from app.services.charts import generate_funnel_chart

    long_title = " → ".join(r["step"] for r in _LONG_FUNNEL) + " — last 30 days (window: 1 hour)"

    wide = await generate_funnel_chart(_LONG_FUNNEL, title=long_title)
    short = await generate_funnel_chart(_LONG_FUNNEL, title="Signup → paid")

    wide_w, wide_h = _png_size(wide)
    short_w, _ = _png_size(short)

    # The long title costs at most a few title lines, never extra width.
    assert wide_w == short_w
    # And the image stays portrait-ish, so the chart dominates the frame.
    assert wide_w < 2 * wide_h


async def test_funnel_chart_title_is_wrapped_and_capped():
    from app.services import charts as charts_mod

    captured = {}

    def _capture(spec, scale):  # noqa: ANN001
        captured["spec"] = spec
        return _PNG_MAGIC + b"\x00" * 32

    long_title = " → ".join(r["step"] for r in _LONG_FUNNEL)
    with patch.object(charts_mod.vlc, "vegalite_to_png", side_effect=_capture):
        await charts_mod.generate_funnel_chart(
            _LONG_FUNNEL, title=long_title, subtitle="LAST 30 DAYS · WINDOW 1 HOUR"
        )

    title = captured["spec"]["title"]
    assert isinstance(title["text"], list)
    assert len(title["text"]) <= charts_mod._TITLE_MAX_LINES
    assert all(len(line) <= charts_mod._TITLE_LINE_CHARS for line in title["text"])
    assert title["subtitle"] == ["LAST 30 DAYS · WINDOW 1 HOUR"]


async def test_funnel_chart_height_grows_with_step_count():
    """Rows stack downwards, so more steps means a taller — not wider — image."""
    from app.services.charts import generate_funnel_chart

    three = await generate_funnel_chart(_LONG_FUNNEL[:3], title="Short")
    ten = await generate_funnel_chart(_LONG_FUNNEL, title="Short")

    three_w, three_h = _png_size(three)
    ten_w, ten_h = _png_size(ten)

    assert ten_w == three_w
    assert ten_h > three_h


def test_wrap_cuts_a_single_oversized_token():
    """A word longer than the line budget is truncated, not left to overflow."""
    from app.services.charts import _wrap

    lines = _wrap("a" * 200, 34, 2)
    assert len(lines) == 1
    assert len(lines[0]) <= 34
    assert lines[0].endswith("…")


async def test_generate_multi_line_chart_returns_valid_png():
    from app.services.charts import generate_multi_line_chart

    series = [
        {"label": "events", "data": _SAMPLE_DATA},
        {
            "label": "pageviews",
            "data": [{"bucket": _SAMPLE_DATA[0]["bucket"], "count": 7}],
        },
    ]
    result = await generate_multi_line_chart(series, title="Overview", period_label="7d")
    assert result[:8] == _PNG_MAGIC


# ── error handling ────────────────────────────────────────────────────────


async def test_render_failure_raises_chart_generation_error():
    from app.services import charts as charts_mod

    def _boom(spec, scale):  # noqa: ANN001
        raise RuntimeError("vl-convert exploded")

    with (
        patch.object(charts_mod.vlc, "vegalite_to_png", side_effect=_boom),
        pytest.raises(charts_mod.ChartGenerationError, match="vl-convert"),
    ):
        await charts_mod.generate_line_chart(_SAMPLE_DATA, title="t", period_label="7d")
