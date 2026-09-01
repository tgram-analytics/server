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
_DEFAULT_HEIGHT = 720

# Vega-Lite does not wrap titles: a long single-line title stretches the whole
# canvas sideways and squeezes the plot into a corner.  We wrap by hand and
# hard-cap the number of lines so the chart always stays the dominant element.
_TITLE_LINE_CHARS = 34
_TITLE_MAX_LINES = 2
_SUBTITLE_LINE_CHARS = 58
_SUBTITLE_MAX_LINES = 2

# Funnel layout: horizontal rows so long event names stay readable and the
# chart grows downwards (not sideways) as steps are added.
_FUNNEL_WIDTH = 540
_FUNNEL_ROW_HEIGHT = 46
_FUNNEL_MIN_HEIGHT = 240
_FUNNEL_LABEL_LIMIT = 210
_FUNNEL_TRACK = "#EEF3F7"


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
                    "fontSize": 22,
                    "fontWeight": 700,
                    "color": _INK,
                    "anchor": "start",
                    "offset": 20,
                    "subtitleFont": _FONT,
                    "subtitleColor": _MUTED,
                    "subtitleFontSize": 13,
                    "subtitleFontWeight": 600,
                    "subtitlePadding": 8,
                },
                "axis": {
                    "labelFont": _FONT,
                    "titleFont": _FONT,
                    "labelColor": _MUTED,
                    "labelFontSize": 13,
                    "titleColor": _MUTED,
                    "domain": False,
                    "ticks": False,
                    "labelPadding": 10,
                    "gridColor": _GRID,
                    "gridDash": [],
                    "gridWidth": 1,
                },
                "axisX": {"grid": False},
                "axisY": {"tickCount": 5},
                "legend": {
                    "labelFont": _FONT,
                    "titleFont": _FONT,
                    "labelColor": _INK,
                    "labelFontSize": 13,
                    "symbolSize": 120,
                    "padding": 12,
                },
                "range": {"category": _TG_PALETTE},
                "bar": {"color": _TG_BLUE, "cornerRadiusEnd": 4},
                "line": {"color": _TG_BLUE, "strokeWidth": 3},
                "point": {"color": _TG_BLUE, "filled": True, "size": 110},
                "area": {"color": _TG_BLUE, "opacity": 0.18},
            }
        }
    )


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%-d %b")


def _wrap(text: str, width: int, max_lines: int) -> list[str]:
    """Greedy word-wrap *text* into at most *max_lines* lines of *width* chars.

    Overflow is ellipsised.  Vega-Lite accepts a list of strings as a title or
    subtitle and renders one line per entry.
    """
    words = str(text).split()
    if not words:
        return [""]

    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)

    truncated = len(lines) > max_lines
    lines = lines[:max_lines]
    # A single token longer than *width* (no spaces to break on) still has to
    # be cut, otherwise it widens the canvas exactly like the unwrapped title.
    lines = [ln if len(ln) <= width else ln[: width - 1].rstrip() + "…" for ln in lines]
    if truncated and not lines[-1].endswith("…"):
        lines[-1] = lines[-1][: width - 1].rstrip() + "…"
    return lines


def _title_params(title: str, subtitle: str | None = None) -> alt.TitleParams:
    """Wrapped title/subtitle so a long label never inflates the canvas."""
    if subtitle:
        return alt.TitleParams(
            text=_wrap(title, _TITLE_LINE_CHARS, _TITLE_MAX_LINES),
            subtitle=_wrap(subtitle, _SUBTITLE_LINE_CHARS, _SUBTITLE_MAX_LINES),
        )
    return alt.TitleParams(text=_wrap(title, _TITLE_LINE_CHARS, _TITLE_MAX_LINES))


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
            title=_title_params(title, subtitle),
        )
    )
    return chart  # type: ignore[no-any-return]


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
        color=alt.Gradient(  # type: ignore[no-untyped-call]
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
    points = base.mark_point(color=_TG_BLUE, filled=True, size=110, stroke="white", strokeWidth=2)
    peak_text = (
        alt.Chart(alt.InlineData(values=[peak_label]))
        .mark_text(dy=-18, font=_FONT, fontSize=14, fontWeight=700, color=_INK)
        .encode(x=alt.X("date:O", sort=None), y="count:Q", text=alt.Text("count:Q", format=","))
    )

    chart = (area + points + peak_text).properties(
        width=_DEFAULT_WIDTH,
        height=_DEFAULT_HEIGHT,
        title=_title_params(title, period_label.upper()),
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
            point={"filled": True, "size": 110, "stroke": "white", "strokeWidth": 2},
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
            point={"filled": True, "size": 90, "stroke": "white", "strokeWidth": 2},
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
            title=_title_params(title, period_label.upper()),
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
            height=56,
            color=alt.Gradient(  # type: ignore[no-untyped-call]
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
            dx=8,
            font=_FONT,
            fontSize=13,
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
        width=_DEFAULT_WIDTH,
        height=_DEFAULT_HEIGHT,
        title=_title_params(title),
    )
    return await _render(chart)


# ── pie ─────────────────────────────────────────────────────────────────
async def generate_pie_chart(
    data: list[dict[str, Any]],
    *,
    title: str,
    label_key: str = "source",
) -> bytes:
    """Donut chart with Telegram-themed multi-color slices.

    Each slice carries a "%" label; each legend entry carries the raw count
    so users can read both proportion (on the chart) and sample size (on the
    legend) at a glance. Legend is sorted by descending count to match the
    visual stacking order.
    """
    if not data:
        return await _render(_empty_data_message(title))

    total = sum(r["count"] for r in data) or 1
    values = [
        {
            "label": r[label_key],
            "count": r["count"],
            "pct": f"{r['count'] / total * 100:.1f}%",
            "legend": f"{r[label_key]} · {r['count']:,}",
        }
        for r in data
    ]

    base = alt.Chart(alt.InlineData(values=values)).encode(
        theta=alt.Theta("count:Q", stack=True),
        color=alt.Color(
            "legend:N",
            scale=alt.Scale(range=_TG_PALETTE),
            legend=alt.Legend(
                title=None,
                orient="bottom",
                direction="horizontal",
                symbolSize=140,
                labelFontSize=14,
                labelLimit=200,
                offset=24,
                rowPadding=8,
                columnPadding=24,
            ),
            sort=alt.SortField(field="count", order="descending"),
        ),
        order=alt.Order("count:Q", sort="descending"),
    )
    arcs = base.mark_arc(innerRadius=160, outerRadius=270, stroke="white", strokeWidth=2)
    pct_labels = base.mark_text(
        radius=215, font=_FONT, fontSize=14, fontWeight=700, color="white"
    ).encode(text="pct:N")

    chart = (arcs + pct_labels).properties(
        width=_DEFAULT_WIDTH,
        height=_DEFAULT_HEIGHT,
        title=_title_params(title),
    )
    return await _render(chart)


# ── funnel ──────────────────────────────────────────────────────────────
async def generate_funnel_chart(
    data: list[dict[str, Any]],
    *,
    title: str,
    subtitle: str | None = None,
) -> bytes:
    """Horizontal funnel rows in the Telegram-blue ramp.

    Rows run horizontally (not as vertical columns) so long event names read
    straight across instead of colliding on a cramped x-axis, and so the chart
    grows *downwards* as steps are added rather than sideways.
    """
    if not data:
        return await _render(_empty_data_message(title, subtitle))

    base_count = data[0]["count"] or 1
    max_count = max(r["count"] for r in data) or 1
    last = max(len(data) - 1, 1)

    rows = []
    for i, r in enumerate(data):
        pct = round(r["count"] / base_count * 100)
        # Spread the blue ramp evenly over however many steps there are, so the
        # shading always reads deep → pale from the first step to the last.
        shade = _TG_FUNNEL_STOPS[round(i / last * (len(_TG_FUNNEL_STOPS) - 1))]
        rows.append(
            {
                "step": str(r["step"]),
                "count": r["count"],
                "label": f"{r['count']:,}  ·  {pct}%",
                "shade": shade,
                "track": max_count,
                "order": i,
            }
        )

    source = alt.InlineData(values=rows)
    step_axis = alt.Y(
        "step:N",
        sort=alt.SortField("order"),
        axis=alt.Axis(
            title=None,
            labelLimit=_FUNNEL_LABEL_LIMIT,
            labelFontSize=14,
            labelFontWeight=600,
            labelColor=_INK,
            grid=False,
        ),
    )
    # Headroom on the right so the value label never spills off the canvas.
    count_scale = alt.Scale(domain=[0, max_count * 1.24], nice=False)
    bar_height = alt.RelativeBandSize(0.62)

    # A pale full-width track behind every row keeps near-zero steps visible
    # instead of collapsing them into an invisible sliver.
    track = (
        alt.Chart(source)
        .mark_bar(cornerRadiusEnd=6, height=bar_height, color=_FUNNEL_TRACK)
        .encode(y=step_axis, x=alt.X("track:Q", scale=count_scale, axis=None))
    )
    bars = (
        alt.Chart(source)
        .mark_bar(cornerRadiusEnd=6, height=bar_height)
        .encode(
            y=step_axis,
            x=alt.X("count:Q", scale=count_scale, axis=None),
            color=alt.Color("shade:N", scale=None, legend=None),
        )
    )
    labels = (
        alt.Chart(source)
        .mark_text(
            align="left",
            baseline="middle",
            dx=10,
            font=_FONT,
            fontSize=14,
            fontWeight=700,
            color=_INK,
        )
        .encode(
            y=step_axis,
            x=alt.X("count:Q", scale=count_scale, axis=None),
            text="label:N",
        )
    )

    chart = (track + bars + labels).properties(
        width=_FUNNEL_WIDTH,
        height=max(_FUNNEL_MIN_HEIGHT, _FUNNEL_ROW_HEIGHT * len(rows)),
        title=_title_params(title, subtitle),
    )
    return await _render(chart)
