"""Tap heatmap queries in app.services.taps (real Postgres, rolled back)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.project import Project
from app.models.tap import Tap
from app.models.user import User
from app.schemas.event import TapPoint
from app.services import taps as svc


def _now() -> datetime:
    # Read the clock per call: rows written with the server default
    # received_at must fall inside the window however long the suite runs.
    return datetime.now(UTC)


@pytest.fixture()
async def project_id(db_session) -> uuid.UUID:
    user = User(telegram_user_id=700_000_000 + uuid.uuid4().int % 100_000_000)
    db_session.add(user)
    await db_session.flush()
    project = Project(
        name=f"taps-svc-{uuid.uuid4().hex[:8]}.com",
        api_key_hash=f"taps_hash_{uuid.uuid4().hex}",
        admin_chat_id=12345,
        owner_user_id=user.id,
    )
    db_session.add(project)
    await db_session.flush()
    return project.id


def _tap(pid, x, y, *, label=None, device="mobile", vw=390, path="/", ago=timedelta(hours=1)):
    return Tap(
        project_id=pid,
        path=path,
        device=device,
        vw=vw,
        kind="tap",
        x=x,
        y=y,
        label=label,
        received_at=_now() - ago,
    )


def _scroll(pid, depth, *, device="mobile", vw=390, path="/"):
    return Tap(
        project_id=pid,
        path=path,
        device=device,
        vw=vw,
        kind="scroll",
        depth=depth,
        received_at=_now() - timedelta(hours=1),
    )


def _window(pid, device="all"):
    now = _now()
    return {
        "project_id": pid,
        "path": "/",
        "device": device,
        "start": now - timedelta(days=7),
        "end": now + timedelta(minutes=1),
    }


async def test_insert_taps_writes_tap_and_scroll_rows(db_session, project_id):
    n = await svc.insert_taps(
        db_session,
        project_id=project_id,
        path="/",
        device="desktop",
        vw=1280,
        taps=[TapPoint(x=0.25, y=100, el="a"), TapPoint(x=0.75, y=200)],
        scroll=0.9,
    )
    assert n == 3
    assert await svc.count_taps(db_session, **_window(project_id)) == 2
    assert await svc.scroll_depth_median(db_session, **_window(project_id)) == pytest.approx(0.9)


async def test_top_elements_ranks_by_count(db_session, project_id):
    db_session.add_all(
        [
            *(_tap(project_id, 0.5, 10, label="Buy") for _ in range(3)),
            *(_tap(project_id, 0.5, 10, label="Menu") for _ in range(5)),
            _tap(project_id, 0.5, 10, label="Logo"),
            _tap(project_id, 0.5, 10),
            _tap(project_id, 0.5, 10),
            # Outside the window and on another page: not counted.
            _tap(project_id, 0.5, 10, label="Logo", ago=timedelta(days=30)),
            _tap(project_id, 0.5, 10, label="Logo", path="/other"),
        ]
    )
    await db_session.flush()

    rows = await svc.top_elements(db_session, **_window(project_id), limit=3)
    assert rows == [
        {"label": "Menu", "count": 5},
        {"label": "Buy", "count": 3},
        {"label": None, "count": 2},
    ]
    assert await svc.count_taps(db_session, **_window(project_id)) == 11


async def test_tap_grid_buckets_edges_into_last_cell(db_session, project_id):
    db_session.add_all(
        [
            _tap(project_id, 1.0, 0),  # x = 1.0 -> last column
            _tap(project_id, 0.0, 239),  # y = 239 -> row 0
            _tap(project_id, 0.7, 240),  # y = 240 -> row 1; x = 0.7 -> column 7
            _tap(project_id, 0.5, 50_000),  # past row 39 -> row 39
        ]
    )
    await db_session.flush()

    grid = await svc.tap_grid(db_session, **_window(project_id))
    assert len(grid) == svc.GRID_MAX_ROWS
    assert all(len(row) == svc.GRID_COLS for row in grid)
    assert grid[0][9] == 1
    assert grid[0][0] == 1
    assert grid[1][7] == 1
    assert grid[39][5] == 1
    assert sum(map(sum, grid)) == 4


async def test_tap_grid_trims_trailing_empty_rows(db_session, project_id):
    db_session.add_all([_tap(project_id, 0.15, 10), _tap(project_id, 0.15, 500)])
    await db_session.flush()

    grid = await svc.tap_grid(db_session, **_window(project_id))
    assert len(grid) == 3  # rows 0..2; row 2 holds y = 500
    assert grid[0][1] == 1
    assert grid[1] == [0] * 10
    assert grid[2][1] == 1


async def test_tap_grid_empty_without_taps(db_session, project_id):
    assert await svc.tap_grid(db_session, **_window(project_id)) == []
    assert await svc.tap_points(db_session, **_window(project_id)) == []
    assert await svc.median_viewport_width(db_session, **_window(project_id)) is None
    assert await svc.scroll_depth_median(db_session, **_window(project_id)) is None


async def test_device_all_ignores_filter(db_session, project_id):
    db_session.add_all(
        [
            _tap(project_id, 0.1, 10, device="mobile"),
            _tap(project_id, 0.1, 10, device="mobile"),
            _tap(project_id, 0.1, 10, device="desktop", vw=1440),
            _tap(project_id, 0.1, 10, device="tablet", vw=800),
        ]
    )
    await db_session.flush()

    assert await svc.count_taps(db_session, **_window(project_id, "all")) == 4
    assert await svc.count_taps(db_session, **_window(project_id, "mobile")) == 2
    assert await svc.count_taps(db_session, **_window(project_id, "desktop")) == 1
    assert len(await svc.tap_points(db_session, **_window(project_id, "tablet"))) == 1
    window = _window(project_id)
    counts = await svc.device_counts(
        db_session, project_id=project_id, path="/", start=window["start"], end=window["end"]
    )
    assert counts == {"mobile": 2, "desktop": 1, "tablet": 1}


async def test_tap_points_newest_first_with_limit(db_session, project_id):
    db_session.add_all(
        [
            _tap(project_id, 0.1, 1, ago=timedelta(hours=3)),
            _tap(project_id, 0.2, 2, ago=timedelta(hours=2)),
            _tap(project_id, 0.3, 3, ago=timedelta(hours=1)),
        ]
    )
    await db_session.flush()

    points = await svc.tap_points(db_session, **_window(project_id), limit=2)
    assert [y for _x, y in points] == [3, 2]
    assert points[0][0] == pytest.approx(0.3)


async def test_median_viewport_width_ignores_scroll_rows(db_session, project_id):
    db_session.add_all(
        [
            _tap(project_id, 0.1, 10, vw=360),
            _tap(project_id, 0.1, 10, vw=390),
            _tap(project_id, 0.1, 10, vw=412),
            _scroll(project_id, 0.5, vw=5000),
            _scroll(project_id, 0.5, vw=5000),
            _scroll(project_id, 0.5, vw=5000),
        ]
    )
    await db_session.flush()

    assert await svc.median_viewport_width(db_session, **_window(project_id)) == 390


async def test_median_viewport_width_ignores_zero_width(db_session, project_id):
    db_session.add_all(
        [
            _tap(project_id, 0.1, 10, vw=0),
            _tap(project_id, 0.1, 10, vw=0),
            _tap(project_id, 0.1, 10, vw=0),
            _tap(project_id, 0.1, 10, vw=390),
            _tap(project_id, 0.1, 10, vw=412),
        ]
    )
    await db_session.flush()

    assert await svc.median_viewport_width(db_session, **_window(project_id)) == 401
    # The taps still count; only the width median skips them.
    assert await svc.count_taps(db_session, **_window(project_id)) == 5


async def test_scroll_median_ignores_tap_rows(db_session, project_id):
    db_session.add_all(
        [
            _scroll(project_id, 0.2),
            _scroll(project_id, 0.4),
            _scroll(project_id, 0.9),
            _scroll(project_id, 1.0, device="desktop"),
            *(_tap(project_id, 0.5, 10) for _ in range(5)),
        ]
    )
    await db_session.flush()

    assert await svc.scroll_depth_median(db_session, **_window(project_id)) == pytest.approx(0.65)
    assert await svc.scroll_depth_median(
        db_session, **_window(project_id, "mobile")
    ) == pytest.approx(0.4)
    # Scroll rows are not taps.
    assert await svc.count_taps(db_session, **_window(project_id)) == 5
