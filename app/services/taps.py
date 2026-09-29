"""Tap heatmap storage and queries.

Write side: :func:`insert_taps` stores one ingestion batch (the taps and the
scroll depth of one pageview).

Read side: every query is scoped to one project, one page path and a
half-open ``[start, end)`` window on ``received_at``, which is what the
``ix_taps_project_path_ts`` index serves. ``device="all"`` means no device
filter. Tap queries read ``kind = "tap"`` rows only; the scroll query reads
``kind = "scroll"`` rows only.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Integer, Numeric, cast, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tap import SCROLL_KIND, TAP_KIND, Tap
from app.schemas.event import TapPoint

ALL_DEVICES = "all"
DEVICES: tuple[str, ...] = ("mobile", "tablet", "desktop")

# Grid defaults: 10 columns over the document width, 240 CSS px per row,
# at most 40 rows (the first 9,600 px of the page; deeper taps land in the
# last row).
GRID_COLS = 10
GRID_ROW_PX = 240
GRID_MAX_ROWS = 40


# ── Write side ───────────────────────────────────────────────────────────────


async def insert_taps(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    path: str,
    device: str,
    vw: int,
    taps: Sequence[TapPoint],
    scroll: float | None,
) -> int:
    """Insert one row per tap plus one scroll row when *scroll* is set.

    Returns the number of rows inserted. Does not commit.
    """
    rows: list[dict[str, Any]] = [
        {
            "project_id": project_id,
            "path": path,
            "device": device,
            "vw": vw,
            "kind": TAP_KIND,
            "x": t.x,
            "y": t.y,
            "label": t.el,
        }
        for t in taps
    ]
    if scroll is not None:
        rows.append(
            {
                "project_id": project_id,
                "path": path,
                "device": device,
                "vw": vw,
                "kind": SCROLL_KIND,
                "depth": scroll,
            }
        )
    if not rows:
        return 0
    await session.execute(insert(Tap), rows)
    return len(rows)


# ── Read side ────────────────────────────────────────────────────────────────


def _filters(
    *,
    project_id: uuid.UUID,
    path: str,
    start: datetime,
    end: datetime,
    kind: str,
    device: str = ALL_DEVICES,
) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = [
        Tap.project_id == project_id,
        Tap.path == path,
        Tap.received_at >= start,
        Tap.received_at < end,
        Tap.kind == kind,
    ]
    if device != ALL_DEVICES:
        conds.append(Tap.device == device)
    return conds


async def count_taps(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    path: str,
    device: str,
    start: datetime,
    end: datetime,
) -> int:
    """Number of taps on *path* in the window."""
    stmt = select(func.count()).where(
        *_filters(
            project_id=project_id, path=path, start=start, end=end, kind=TAP_KIND, device=device
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def top_elements(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    path: str,
    device: str,
    start: datetime,
    end: datetime,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Most-tapped element labels, highest count first.

    Returns ``[{"label": str | None, "count": int}, ...]``. Taps without a
    label group under ``None``.
    """
    count = func.count().label("count")
    stmt = (
        select(Tap.label, count)
        .where(
            *_filters(
                project_id=project_id,
                path=path,
                start=start,
                end=end,
                kind=TAP_KIND,
                device=device,
            )
        )
        .group_by(Tap.label)
        .order_by(count.desc(), Tap.label.asc().nulls_last())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [{"label": label, "count": int(n)} for label, n in result.all()]


async def tap_points(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    path: str,
    device: str,
    start: datetime,
    end: datetime,
    limit: int = 5000,
) -> list[tuple[float, int]]:
    """``(x, y)`` of the most recent taps, newest first, at most *limit*."""
    stmt = (
        select(Tap.x, Tap.y)
        .where(
            *_filters(
                project_id=project_id,
                path=path,
                start=start,
                end=end,
                kind=TAP_KIND,
                device=device,
            )
        )
        .order_by(Tap.received_at.desc(), Tap.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [(float(x), int(y)) for x, y in result.all() if x is not None and y is not None]


async def tap_grid(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    path: str,
    device: str,
    start: datetime,
    end: datetime,
    cols: int = GRID_COLS,
    row_px: int = GRID_ROW_PX,
    max_rows: int = GRID_MAX_ROWS,
) -> list[list[int]]:
    """Tap counts in a ``rows x cols`` grid over the document.

    Row ``i`` covers ``y`` in ``[i*row_px, (i+1)*row_px)`` CSS px from the
    page top; column ``j`` covers ``x`` in ``[j/cols, (j+1)/cols)`` of the
    document width. ``x = 1.0`` falls in the last column; ``y`` past the
    last row falls in the last row. The grid is trimmed after the last
    non-empty row, so it is ``[]`` when there are no taps.
    """
    # x is REAL (float4). Casting to NUMERIC first keeps boundary values
    # exact (0.7 would otherwise read as 0.69999999 and land one column left).
    col = func.least(cast(func.floor(cast(Tap.x, Numeric) * cols), Integer), cols - 1).label("col")
    # Integer division: y // row_px renders as integer "/" in PostgreSQL.
    row = func.least(Tap.y // row_px, max_rows - 1).label("row")
    stmt = (
        select(row, col, func.count().label("n"))
        .where(
            *_filters(
                project_id=project_id,
                path=path,
                start=start,
                end=end,
                kind=TAP_KIND,
                device=device,
            ),
            Tap.x.is_not(None),
            Tap.y.is_not(None),
        )
        .group_by(row, col)
    )
    cells = (await session.execute(stmt)).all()
    if not cells:
        return []
    n_rows = max(int(r) for r, _c, _n in cells) + 1
    grid = [[0] * cols for _ in range(n_rows)]
    for r, c, n in cells:
        grid[int(r)][int(c)] += int(n)
    return grid


async def device_counts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    path: str,
    start: datetime,
    end: datetime,
) -> dict[str, int]:
    """Tap count per viewport bucket, e.g. ``{"mobile": 12, "desktop": 3}``."""
    stmt = (
        select(Tap.device, func.count())
        .where(*_filters(project_id=project_id, path=path, start=start, end=end, kind=TAP_KIND))
        .group_by(Tap.device)
    )
    result = await session.execute(stmt)
    return {device: int(n) for device, n in result.all()}


async def median_viewport_width(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    path: str,
    device: str,
    start: datetime,
    end: datetime,
) -> int | None:
    """Median ``vw`` over the taps in the window, or ``None`` without taps."""
    stmt = select(func.percentile_cont(0.5).within_group(Tap.vw)).where(
        *_filters(
            project_id=project_id, path=path, start=start, end=end, kind=TAP_KIND, device=device
        )
    )
    value = (await session.execute(stmt)).scalar_one_or_none()
    return None if value is None else round(float(value))


async def scroll_depth_median(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    path: str,
    device: str,
    start: datetime,
    end: datetime,
) -> float | None:
    """Median maximum scroll depth (0..1) per pageview, or ``None``."""
    stmt = select(func.percentile_cont(0.5).within_group(Tap.depth)).where(
        *_filters(
            project_id=project_id,
            path=path,
            start=start,
            end=end,
            kind=SCROLL_KIND,
            device=device,
        ),
        Tap.depth.is_not(None),
    )
    value = (await session.execute(stmt)).scalar_one_or_none()
    return None if value is None else float(value)
