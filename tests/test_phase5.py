"""Phase 5 — Chart generation tests.

Charts are now rendered in-process via Altair + vl-convert (no external
HTTP service).  Tests assert that the renderer returns valid PNG bytes
and that Vega-Lite specs include the expected titles/series.
"""

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
