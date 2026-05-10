"""Chart generation service backed by Altair + vl-convert.

Renders Vega-Lite specs to PNG bytes in-process using vl-convert (a Rust
binary), themed with the Telegram brand palette.  No external HTTP service
is required.

All public functions are async and raise :class:`ChartGenerationError` on
any failure so callers can fall back to a text-only message gracefully.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

import altair as alt
import vl_convert as vlc

logger = logging.getLogger(__name__)


class ChartGenerationError(Exception):
    """Raised when vl-convert fails to render a chart."""


# ── Telegram palette ─────────────────────────────────────────────────────
_TG_BLUE = "#2AABEE"
_TG_BLUE_DEEP = "#229ED9"
_TG_BLUE_LIGHT = "#54B9F0"

# Multi-series accents drawn from Telegram UI cues (notification orange,
# online green, premium purple, pin yellow, alert red…).
_TG_PALETTE = [
    "#2AABEE",
    "#FF7A45",
    "#2ECC71",
    "#9B59B6",
    "#F1C40F",
    "#E74C3C",
    "#1ABC9C",
    "#34495E",
]
_TG_FUNNEL_STOPS = ["#229ED9", "#2AABEE", "#54B9F0", "#8FCDF3", "#C7E6F8"]

_INK = "#0F172A"
_MUTED = "#6C7883"
_GRID = "#E1E8ED"
_BG = "#FFFFFF"

_FONT = "SF Pro Display, SF Pro Text, -apple-system, Roboto, Helvetica Neue, Arial, sans-serif"

_PNG_SCALE = 2  # 2x density so charts stay crisp on retina Telegram clients
_DEFAULT_WIDTH = 720
_DEFAULT_HEIGHT = 320


@alt.theme.register("telegram", enable=True)
def _telegram_theme() -> alt.theme.ThemeConfig:
    return alt.theme.ThemeConfig(
        {
            "config": {
                "background": _BG,
                "view": {"stroke": None},
                "padding": {"left": 24, "right": 24, "top": 16, "bottom": 16},
                "title": {
                    "font": _FONT,
                    "fontSize": 17,
                    "fontWeight": 700,
                    "color": _INK,
                    "anchor": "start",
                    "offset": 16,
                    "subtitleFont": _FONT,
                    "subtitleColor": _MUTED,
                    "subtitleFontSize": 11,
                    "subtitleFontWeight": 600,
                    "subtitlePadding": 6,
                },
                "axis": {
                    "labelFont": _FONT,
                    "titleFont": _FONT,
                    "labelColor": _MUTED,
                    "labelFontSize": 11,
                    "titleColor": _MUTED,
                    "domain": False,
                    "ticks": False,
                    "labelPadding": 8,
                    "gridColor": _GRID,
                    "gridDash": [],
                    "gridWidth": 1,
                },
                "axisX": {"grid": False},
                "axisY": {"tickCount": 4},
                "legend": {
                    "labelFont": _FONT,
                    "titleFont": _FONT,
                    "labelColor": _INK,
                    "labelFontSize": 11,
                    "symbolSize": 80,
                    "padding": 8,
                },
                "range": {"category": _TG_PALETTE},
                "bar": {"color": _TG_BLUE, "cornerRadiusEnd": 4},
                "line": {"color": _TG_BLUE, "strokeWidth": 3},
                "point": {"color": _TG_BLUE, "filled": True, "size": 70},
                "area": {"color": _TG_BLUE, "opacity": 0.18},
            }
        }
    )


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%-d %b")


async def _render(chart: alt.Chart) -> bytes:
    """Render a chart to PNG bytes via vl-convert, off the event loop."""
    spec = chart.to_dict()
    try:
        return await asyncio.to_thread(vlc.vegalite_to_png, spec, scale=_PNG_SCALE)
    except Exception as exc:
        logger.error("Chart rendering failed: %s", exc)
        raise ChartGenerationError(f"vl-convert failed: {exc}") from exc


def _empty_data_message(title: str, period_label: str | None = None) -> alt.Chart:
    """Render a tiny placeholder chart when there is nothing to plot."""
    subtitle = period_label.upper() if period_label else "NO DATA"
    chart = (
        alt.Chart(alt.InlineData(values=[{"x": 0, "y": 0}]))
        .mark_text(text="No data", font=_FONT, fontSize=14, color=_MUTED)
        .encode(x=alt.value(_DEFAULT_WIDTH / 2), y=alt.value(_DEFAULT_HEIGHT / 2))
        .properties(
            width=_DEFAULT_WIDTH,
            height=_DEFAULT_HEIGHT,
            title=alt.TitleParams(text=title, subtitle=subtitle),
        )
    )
    return chart


# ── line ────────────────────────────────────────────────────────────────
async def generate_line_chart(
    data: list[dict[str, Any]],
    *,
    title: str,
    period_label: str,
) -> bytes:
    """Single-series line chart with Telegram-blue gradient fill.

    *data* is ``[{"bucket": datetime, "count": int}, ...]``.
    """
    if not data:
        return await _render(_empty_data_message(title, period_label))

    values = [{"date": _fmt_date(r["bucket"]), "count": r["count"]} for r in data]
    peak_value = max(r["count"] for r in data)
    peak_label = next(v for v in values if v["count"] == peak_value)

    base = alt.Chart(alt.InlineData(values=values)).encode(
        x=alt.X("date:O", sort=None, axis=alt.Axis(title=None)),
        y=alt.Y("count:Q", axis=alt.Axis(title=None)),
    )
    area = base.mark_area(
        line={"color": _TG_BLUE, "strokeWidth": 3},
        interpolate="monotone",
        color=alt.Gradient(
            gradient="linear",
            stops=[
                alt.GradientStop(color=_TG_BLUE, offset=0),
                alt.GradientStop(color="#FFFFFF", offset=1),
            ],
            x1=1,
            x2=1,
            y1=1,
            y2=0,
        ),
        opacity=0.45,
    )
    points = base.mark_point(color=_TG_BLUE, filled=True, size=70, stroke="white", strokeWidth=2)
    peak_text = (
        alt.Chart(alt.InlineData(values=[peak_label]))
        .mark_text(dy=-14, font=_FONT, fontSize=12, fontWeight=700, color=_INK)
        .encode(x=alt.X("date:O", sort=None), y="count:Q", text=alt.Text("count:Q", format=","))
    )

    chart = (area + points + peak_text).properties(
        width=_DEFAULT_WIDTH,
        height=_DEFAULT_HEIGHT,
        title=alt.TitleParams(text=title, subtitle=period_label.upper()),
    )
    return await _render(chart)


# ── multi-series comparison ─────────────────────────────────────────────
async def generate_comparison_chart(
    data_a: list[dict[str, Any]],
    data_b: list[dict[str, Any]],
    *,
    label_a: str,
    label_b: str,
) -> bytes:
    """Two-series line chart for period-over-period comparison."""
    if not data_a and not data_b:
        return await _render(_empty_data_message("Comparison"))

    rows = []
    for r in data_a:
        rows.append({"date": _fmt_date(r["bucket"]), "count": r["count"], "series": label_a})
    for r in data_b:
        rows.append({"date": _fmt_date(r["bucket"]), "count": r["count"], "series": label_b})

    primary = data_a if len(data_a) >= len(data_b) else data_b
    label_order = [_fmt_date(r["bucket"]) for r in primary]

    chart = (
        alt.Chart(alt.InlineData(values=rows))
        .mark_line(
            point={"filled": True, "size": 70, "stroke": "white", "strokeWidth": 2},
            strokeWidth=3,
            interpolate="monotone",
        )
        .encode(
            x=alt.X("date:O", sort=label_order, axis=alt.Axis(title=None)),
            y=alt.Y("count:Q", axis=alt.Axis(title=None)),
            color=alt.Color(
                "series:N",
                scale=alt.Scale(domain=[label_a, label_b], range=[_TG_BLUE, _TG_PALETTE[1]]),
                legend=alt.Legend(title=None, orient="top-right"),
            ),
        )
        .properties(width=_DEFAULT_WIDTH, height=_DEFAULT_HEIGHT)
    )
    return await _render(chart)


# ── multi-series line ───────────────────────────────────────────────────
async def generate_multi_line_chart(
    series: list[dict[str, Any]],
    *,
    title: str,
    period_label: str,
) -> bytes:
    """Multi-series line chart.

    *series* is ``[{"label": str, "data": [{"bucket": datetime, "count": int}]}]``.
    """
    if not series or all(not s["data"] for s in series):
        return await _render(_empty_data_message(title, period_label))

    rows = []
    for s in series:
        for r in s["data"]:
            rows.append({"date": _fmt_date(r["bucket"]), "count": r["count"], "series": s["label"]})

    primary = max(series, key=lambda s: len(s["data"]))
    label_order = [_fmt_date(r["bucket"]) for r in primary["data"]]

    chart = (
        alt.Chart(alt.InlineData(values=rows))
        .mark_line(
            point={"filled": True, "size": 60, "stroke": "white", "strokeWidth": 2},
            strokeWidth=2.5,
            interpolate="monotone",
        )
        .encode(
            x=alt.X("date:O", sort=label_order, axis=alt.Axis(title=None)),
            y=alt.Y("count:Q", axis=alt.Axis(title=None)),
            color=alt.Color("series:N", legend=alt.Legend(title=None, orient="top-right")),
        )
        .properties(
            width=_DEFAULT_WIDTH,
            height=_DEFAULT_HEIGHT,
            title=alt.TitleParams(text=title, subtitle=period_label.upper()),
        )
    )
    return await _render(chart)


# ── horizontal bar ──────────────────────────────────────────────────────
async def generate_bar_chart(
    data: list[dict[str, Any]],
    *,
    title: str,
) -> bytes:
    """Horizontal bar chart with Telegram-blue gradient fill.

    *data* is ``[{"value": str, "count": int}, ...]``.
    """
    if not data:
        return await _render(_empty_data_message(title))

    values = [{"value": r["value"], "count": r["count"]} for r in data]
    bars = (
        alt.Chart(alt.InlineData(values=values))
        .mark_bar(
            cornerRadiusEnd=4,
            height=22,
            color=alt.Gradient(
                gradient="linear",
                stops=[
                    alt.GradientStop(color=_TG_BLUE_LIGHT, offset=0),
                    alt.GradientStop(color=_TG_BLUE, offset=0.6),
                    alt.GradientStop(color=_TG_BLUE_DEEP, offset=1),
                ],
                x1=0,
                x2=1,
                y1=0,
                y2=0,
            ),
        )
        .encode(
            x=alt.X("count:Q", axis=alt.Axis(title=None)),
            y=alt.Y("value:N", sort="-x", axis=alt.Axis(title=None)),
        )
    )
    labels = (
        alt.Chart(alt.InlineData(values=values))
        .mark_text(
            align="left",
            baseline="middle",
            dx=6,
            font=_FONT,
            fontSize=11,
            color=_INK,
            fontWeight=600,
        )
        .encode(
            x="count:Q",
            y=alt.Y("value:N", sort="-x"),
            text=alt.Text("count:Q", format=","),
        )
    )
    chart = (bars + labels).properties(
        width=_DEFAULT_WIDTH - 100,
        height=_DEFAULT_HEIGHT,
        title=alt.TitleParams(text=title),
    )
    return await _render(chart)


# ── pie ─────────────────────────────────────────────────────────────────
async def generate_pie_chart(
    data: list[dict[str, Any]],
    *,
    title: str,
    label_key: str = "source",
) -> bytes:
    """Donut chart with Telegram-themed multi-color slices and % labels."""
    if not data:
        return await _render(_empty_data_message(title))

    total = sum(r["count"] for r in data) or 1
    values = [
        {
            "label": r[label_key],
            "count": r["count"],
            "pct": f"{r['count'] / total * 100:.1f}%",
        }
        for r in data
    ]

    base = alt.Chart(alt.InlineData(values=values)).encode(
        theta=alt.Theta("count:Q", stack=True),
        color=alt.Color(
            "label:N",
            scale=alt.Scale(range=_TG_PALETTE),
            legend=alt.Legend(title=None, orient="right"),
        ),
        order=alt.Order("count:Q", sort="descending"),
    )
    arcs = base.mark_arc(innerRadius=70, outerRadius=130, stroke="white", strokeWidth=2)
    pct_labels = base.mark_text(
        radius=100, font=_FONT, fontSize=11, fontWeight=700, color="white"
    ).encode(text="pct:N")

    chart = (arcs + pct_labels).properties(
        width=_DEFAULT_WIDTH,
        height=_DEFAULT_HEIGHT,
        title=alt.TitleParams(text=title),
    )
    return await _render(chart)


# ── funnel ──────────────────────────────────────────────────────────────
async def generate_funnel_chart(
    data: list[dict[str, Any]],
    *,
    title: str,
) -> bytes:
    """Vertical funnel bars in the Telegram-blue ramp."""
    if not data:
        return await _render(_empty_data_message(title))

    base_count = data[0]["count"] or 1
    rows = []
    for i, r in enumerate(data):
        pct = round(r["count"] / base_count * 100)
        rows.append(
            {
                "step": r["step"],
                "count": r["count"],
                "label": f"{r['count']:,}",
                "pct": f"{pct}%",
                "shade": _TG_FUNNEL_STOPS[i % len(_TG_FUNNEL_STOPS)],
                "order": i,
            }
        )

    bars = (
        alt.Chart(alt.InlineData(values=rows))
        .mark_bar(cornerRadiusTopLeft=8, cornerRadiusTopRight=8, size=78)
        .encode(
            x=alt.X("step:N", sort=alt.SortField("order"), axis=alt.Axis(title=None, labelAngle=0)),
            y=alt.Y("count:Q", axis=alt.Axis(title=None)),
            color=alt.Color("shade:N", scale=None, legend=None),
        )
    )
    text_count = (
        alt.Chart(alt.InlineData(values=rows))
        .mark_text(dy=-22, font=_FONT, fontSize=12, fontWeight=700, color=_INK)
        .encode(x=alt.X("step:N", sort=alt.SortField("order")), y="count:Q", text="label:N")
    )
    text_pct = (
        alt.Chart(alt.InlineData(values=rows))
        .mark_text(dy=-8, font=_FONT, fontSize=10, fontWeight=600, color=_MUTED)
        .encode(x=alt.X("step:N", sort=alt.SortField("order")), y="count:Q", text="pct:N")
    )
    chart = (bars + text_count + text_pct).properties(
        width=_DEFAULT_WIDTH,
        height=_DEFAULT_HEIGHT,
        title=alt.TitleParams(text=title),
    )
    return await _render(chart)
