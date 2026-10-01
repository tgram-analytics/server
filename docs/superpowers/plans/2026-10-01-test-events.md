# Test Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Events marked as test (payload `test: true`, or sent from a localhost origin) are stored but excluded from every analytics read path; debug views still show them, marked 🧪.

**Architecture:** One `is_test boolean` column on `events`, set at ingestion. One shared SQLAlchemy predicate `REAL_EVENTS` is applied to every analytics query; raw SQL uses `AND NOT is_test`. A static AST guard test fails CI when a new `.where()` on `Event` lacks the filter. Debug views opt out with the marker comment `# includes test events on purpose`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, asyncpg, Alembic, pytest (+anyio), Postgres 16.

**Spec:** `docs/superpowers/specs/2026-09-30-test-events-design.md`

---

## Local test database

The DB-backed tests skip unless `DATABASE_URL` is set. Start a throwaway Postgres once (check `free -h` first; it needs ~100 MB):

```bash
docker run -d --name tgram-test-events-pg -e POSTGRES_USER=tga -e POSTGRES_PASSWORD=testpassword -e POSTGRES_DB=tganalytics_test -p 55432:5432 postgres:16-alpine
export DATABASE_URL="postgresql+asyncpg://tga:testpassword@localhost:55432/tganalytics_test"
uv run alembic upgrade head
```

The toolchain is not on PATH: prefix every command with `uv run`. Every `pytest` command below assumes `DATABASE_URL` is exported. Remove the container at the end: `docker rm -f tgram-test-events-pg`.

## File map

| File | Change |
|---|---|
| `alembic/versions/0015_events_is_test.py` | Create: add `events.is_test` |
| `app/models/event.py` | `is_test` column, `REAL_EVENTS` predicate |
| `app/services/events.py` | `is_local_host()`, `insert_event(is_test=)`, alert threshold filter |
| `app/schemas/event.py` | `test` field on 3 request models, `is_test` on `EventResponse` |
| `app/api/ingestion.py` | compute `is_test`, skip alerts, drop test taps |
| `app/services/analytics.py` | filter 8 functions, `list_recent_events` returns `is_test` |
| `app/services/aggregation.py` | filter rollup |
| `app/services/funnels.py` | filter both step queries |
| `app/bot/handlers/reports.py`, `digest.py`, `export.py` | filter all queries |
| `app/bot/handlers/doctor.py` | report test count |
| `app/bot/handlers/events.py` | 🧪 in recent activity |
| `app/mcp/tools/_schemas.py`, `data.py`, `setup.py` | `is_test`, `test_count`, docstrings |
| `tests/test_events_is_test.py` | Create: ingestion + read path tests |
| `tests/test_local_host.py` | Create: `is_local_host` unit tests |
| `tests/test_real_events_guard.py` | Create: static guard |
| `README.md`, `openapi.json` | docs |

---

### Task 1: Column, model, predicate, insert param

**Files:**
- Create: `alembic/versions/0015_events_is_test.py`
- Modify: `app/models/event.py`
- Modify: `app/services/events.py` (`insert_event`)
- Modify: `app/schemas/event.py` (`EventResponse`)
- Test: `tests/test_events_is_test.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_events_is_test.py`:

```python
"""Events marked as test: stored, excluded from analytics, shown in debug views."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.event import Event

ADMIN_ID = 111


async def _project(session, owner_user_id, name: str):
    from app.services.projects import create_project

    project, _ = await create_project(
        session, name=name, admin_chat_id=ADMIN_ID, owner_user_id=owner_user_id
    )
    await session.flush()
    return project


async def test_insert_event_stores_is_test(db_session, singleton_user):
    from app.services.events import insert_event

    project = await _project(db_session, singleton_user.id, "is-test-insert.com")
    real = await insert_event(
        db_session, project_id=project.id, event_name="signup", session_id="s1", properties={}
    )
    test = await insert_event(
        db_session,
        project_id=project.id,
        event_name="signup",
        session_id="s2",
        properties={},
        is_test=True,
    )
    assert real.is_test is False
    assert test.is_test is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_events_is_test.py::test_insert_event_stores_is_test -v`
Expected: FAIL with `TypeError: insert_event() got an unexpected keyword argument 'is_test'`

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0015_events_is_test.py`:

```python
"""Add events.is_test (test events are stored but excluded from analytics).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Constant default: metadata-only on Postgres 11+, no table rewrite.
    op.add_column(
        "events",
        sa.Column("is_test", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("events", "is_test")
```

- [ ] **Step 4: Add the column and predicate to the model**

In `app/models/event.py`, add after the `device_type` column (last line of the class):

```python
    # Test events (payload ``test: true`` or a localhost origin) are stored
    # but excluded from analytics. Filter reads with ``REAL_EVENTS``.
    is_test: Mapped[bool] = mapped_column(
        sa.Boolean,
        server_default=sa.false(),
        default=False,
        nullable=False,
    )


# Shared WHERE predicate for every analytics read. Debug views that must
# also show test events mark the query with "# includes test events on purpose".
REAL_EVENTS = Event.is_test.is_(False)
```

- [ ] **Step 5: Add the `is_test` param to `insert_event`**

In `app/services/events.py`, `insert_event` signature: add `is_test: bool = False,` after `device_type: str | None = None,`. In the `Event(...)` constructor add `is_test=is_test,` after `device_type=device_type,`.

- [ ] **Step 6: Add `is_test` to `EventResponse`**

In `app/schemas/event.py`, class `EventResponse`, add after `received_at: datetime`:

```python
    is_test: bool = False
```

- [ ] **Step 7: Apply migration and run the test**

Run: `uv run alembic upgrade head && uv run pytest tests/test_events_is_test.py::test_insert_event_stores_is_test -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add alembic/versions/0015_events_is_test.py app/models/event.py app/services/events.py app/schemas/event.py tests/test_events_is_test.py
git commit -m "feat(events): add is_test column and REAL_EVENTS predicate"
```

---

### Task 2: `is_local_host()` helper

**Files:**
- Modify: `app/services/events.py`
- Test: `tests/test_local_host.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_local_host.py`:

```python
"""is_local_host: which Origin / URL values count as local development."""

import pytest

from app.services.events import is_local_host


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost",
        "http://localhost:5173",
        "https://LOCALHOST:3000",
        "http://app.localhost:8080",
        "http://127.0.0.1:8000",
        "http://127.1.2.3",
        "http://[::1]:3000",
        "http://0.0.0.0:8000",
        "http://localhost:5173/pricing?x=1",
        "localhost:3000",
    ],
)
def test_local_values(value):
    assert is_local_host(value) is True


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "null",
        "/pricing",
        "https://example.com",
        "https://localhost.example.com",
        "https://127.example.com",
        "https://mylocalhost.com",
        "http://192.168.1.10:3000",
        "http://[not-an-ip",
    ],
)
def test_non_local_values(value):
    assert is_local_host(value) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_local_host.py -v`
Expected: FAIL with `ImportError: cannot import name 'is_local_host'`

- [ ] **Step 3: Implement**

In `app/services/events.py`, add `import ipaddress` and `from urllib.parse import urlsplit` to the imports, then add after `is_origin_allowed`:

```python
_LOCAL_HOSTNAMES = frozenset({"localhost", "0.0.0.0"})


def is_local_host(value: str | None) -> bool:
    """True when *value* (an Origin header or a page URL) points at a local
    development host: ``localhost``, ``*.localhost``, ``127.0.0.0/8``,
    ``::1`` or ``0.0.0.0``.

    Events from such hosts are stored as test events. Values without a host
    (``None``, ``"null"``, a bare path) are not local.
    """
    if not value:
        return False
    try:
        host = urlsplit(value if "//" in value else f"//{value}").hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.lower().rstrip(".")
    if host in _LOCAL_HOSTNAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_local_host.py -v`
Expected: PASS (20 passed). Note: `"/pricing"` becomes `"///pricing"` → hostname `None` → False. `"null"` → hostname `"null"` → not loopback → False.

- [ ] **Step 5: Commit**

```bash
git add app/services/events.py tests/test_local_host.py
git commit -m "feat(events): add is_local_host helper"
```

---

### Task 3: Ingestion sets `is_test`, skips alerts, drops test taps

**Files:**
- Modify: `app/schemas/event.py` (`TrackEventRequest`, `PageviewRequest`, `TapsRequest`)
- Modify: `app/api/ingestion.py` (`track`, `pageview`, `taps`)
- Test: `tests/test_events_is_test.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_events_is_test.py`:

```python
# ── Ingestion ─────────────────────────────────────────────────────────────────

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


async def _api_project(api_client, name: str) -> dict:
    resp = await api_client.post(
        "/api/v1/internal/projects", json={"name": name, "admin_chat_id": ADMIN_ID}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _stored(db_session, project_id: str) -> list[Event]:
    await db_session.invalidate()
    result = await db_session.execute(
        select(Event).where(Event.project_id == uuid.UUID(project_id))
    )
    return list(result.scalars().all())


async def test_track_default_is_not_test(api_client, db_session):
    data = await _api_project(api_client, "ingest-default.com")
    resp = await api_client.post(
        "/api/v1/track",
        json={"api_key": data["api_key"], "event_name": "signup", "session_id": "s1"},
        headers={"User-Agent": _CHROME_UA},
    )
    assert resp.status_code == 202, resp.text
    rows = await _stored(db_session, data["id"])
    assert [r.is_test for r in rows] == [False]


async def test_track_test_flag(api_client, db_session):
    data = await _api_project(api_client, "ingest-flag.com")
    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "signup",
            "session_id": "s1",
            "test": True,
        },
        headers={"User-Agent": _CHROME_UA},
    )
    assert resp.status_code == 202, resp.text
    rows = await _stored(db_session, data["id"])
    assert [r.is_test for r in rows] == [True]
    assert "test" not in rows[0].properties


async def test_track_localhost_origin_is_test(api_client, db_session):
    data = await _api_project(api_client, "ingest-origin.com")
    resp = await api_client.post(
        "/api/v1/track",
        json={"api_key": data["api_key"], "event_name": "signup", "session_id": "s1"},
        headers={"User-Agent": _CHROME_UA, "Origin": "http://localhost:5173"},
    )
    assert resp.status_code == 202, resp.text
    rows = await _stored(db_session, data["id"])
    assert [r.is_test for r in rows] == [True]


async def test_pageview_localhost_url_is_test(api_client, db_session):
    data = await _api_project(api_client, "ingest-url.com")
    resp = await api_client.post(
        "/api/v1/pageview",
        json={
            "api_key": data["api_key"],
            "session_id": "s1",
            "url": "http://127.0.0.1:8000/pricing",
        },
        headers={"User-Agent": _CHROME_UA},
    )
    assert resp.status_code == 202, resp.text
    rows = await _stored(db_session, data["id"])
    assert [r.is_test for r in rows] == [True]


async def test_pageview_public_url_is_not_test(api_client, db_session):
    data = await _api_project(api_client, "ingest-public.com")
    resp = await api_client.post(
        "/api/v1/pageview",
        json={
            "api_key": data["api_key"],
            "session_id": "s1",
            "url": "https://ingest-public.com/pricing",
        },
        headers={"User-Agent": _CHROME_UA, "Origin": "https://ingest-public.com"},
    )
    assert resp.status_code == 202, resp.text
    rows = await _stored(db_session, data["id"])
    assert [r.is_test for r in rows] == [False]


async def test_test_event_does_not_schedule_alerts(api_client, monkeypatch):
    import app.api.ingestion as ing

    calls: list[str] = []

    async def _record(project_id, event_name, properties=None):
        calls.append(event_name)

    monkeypatch.setattr(ing, "_run_alert_evaluation", _record)
    data = await _api_project(api_client, "ingest-alerts.com")
    base = {"api_key": data["api_key"], "session_id": "s1"}

    await api_client.post(
        "/api/v1/track",
        json={**base, "event_name": "test_one", "test": True},
        headers={"User-Agent": _CHROME_UA},
    )
    await api_client.post(
        "/api/v1/pageview",
        json={**base, "url": "http://localhost:3000/"},
        headers={"User-Agent": _CHROME_UA},
    )
    await api_client.post(
        "/api/v1/track",
        json={**base, "event_name": "real_one"},
        headers={"User-Agent": _CHROME_UA},
    )
    assert calls == ["real_one"]


async def test_test_taps_store_nothing(api_client, db_session):
    from app.models.tap import Tap

    data = await _api_project(api_client, "ingest-taps.com")
    body = {
        "api_key": data["api_key"],
        "session_id": "s1",
        "path": "/",
        "viewport": "desktop",
        "vw": 1280,
        "taps": [{"x": 0.5, "y": 100}],
        "scroll": 0.5,
    }
    r1 = await api_client.post(
        "/api/v1/taps", json={**body, "test": True}, headers={"User-Agent": _CHROME_UA}
    )
    r2 = await api_client.post(
        "/api/v1/taps",
        json=body,
        headers={"User-Agent": _CHROME_UA, "Origin": "http://localhost:5173"},
    )
    assert r1.status_code == 202 and r1.json() == {"status": "accepted"}
    assert r2.status_code == 202 and r2.json() == {"status": "accepted"}
    await db_session.invalidate()
    rows = (
        (await db_session.execute(select(Tap).where(Tap.project_id == uuid.UUID(data["id"]))))
        .scalars()
        .all()
    )
    assert rows == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_events_is_test.py -k "track or pageview or alerts or taps" -v`
Expected: FAIL. `test_track_test_flag` stores `is_test=False`; `test_test_event_does_not_schedule_alerts` records `["test_one", "pageview", "real_one"]`.

- [ ] **Step 3: Add the `test` field to the request schemas**

In `app/schemas/event.py`, add this line to `TrackEventRequest` (after `timestamp`), `PageviewRequest` (after `properties`) and `TapsRequest` (after `scroll`):

```python
    # Mark as test data: stored, excluded from analytics.
    test: bool = False
```

- [ ] **Step 4: Use it in `/track`**

In `app/api/ingestion.py`, add `is_local_host` to the import from `app.services.events`:

```python
from app.services.events import evaluate_alerts, insert_event, is_local_host, is_origin_allowed
```

In `track`, after `scrubbed, _dropped, _oversized = scrub_properties(...)`, add:

```python
    is_test = body.test or is_local_host(origin)
```

Pass `is_test=is_test,` to `insert_event(...)` after `device_type=device_type,`. Replace the final alert scheduling block with:

```python
    # Privacy: alert notifications render property keys/values into Telegram
    # messages, so they must see the same scrubbed dict that is persisted —
    # never the raw request properties. Test events never fire alerts.
    if not is_test:
        background_tasks.add_task(_run_alert_evaluation, project.id, body.event_name, scrubbed)
    return {"status": "accepted"}
```

- [ ] **Step 5: Use it in `/pageview`**

In `pageview`, after `scrubbed, _dropped, _oversized = scrub_properties(...)`, add:

```python
    is_test = body.test or is_local_host(origin) or is_local_host(body.url)
```

Pass `is_test=is_test,` to `insert_event(...)`. Replace the final alert scheduling block with:

```python
    # Privacy: same as /track — alerts must only ever see scrubbed properties.
    if not is_test:
        background_tasks.add_task(_run_alert_evaluation, project.id, "pageview", scrubbed)
    return {"status": "accepted"}
```

- [ ] **Step 6: Use it in `/taps`**

In `taps`, replace:

```python
    if device_type == "bot":
        # Same as /track: crawlers are not visitors.
        return {"status": "accepted"}
```

with:

```python
    if device_type == "bot" or body.test or is_local_host(origin):
        # Crawlers are not visitors, and taps have no debug view, so test
        # and localhost taps are not stored either.
        return {"status": "accepted"}
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_events_is_test.py tests/test_taps_ingestion.py tests/test_ingestion_privacy.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/schemas/event.py app/api/ingestion.py tests/test_events_is_test.py
git commit -m "feat(ingestion): mark test and localhost events, skip their alerts"
```

---

### Task 4: Exclude test events from analytics services

**Files:**
- Modify: `app/services/analytics.py`
- Modify: `app/services/aggregation.py`
- Modify: `app/services/events.py` (`evaluate_alerts`)
- Modify: `app/services/funnels.py`
- Test: `tests/test_events_is_test.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_events_is_test.py`:

```python
# ── Analytics read paths exclude test events ──────────────────────────────────


async def _seed_pair(session, project_id, *, event_name="signup", properties=None, sid="s"):
    """One real and one test event, same name, same time window."""
    now = datetime.now(UTC) - timedelta(minutes=5)
    props = properties or {}
    session.add(
        Event(
            project_id=project_id,
            event_name=event_name,
            session_id=f"{sid}-real",
            properties=props,
            timestamp=now,
        )
    )
    session.add(
        Event(
            project_id=project_id,
            event_name=event_name,
            session_id=f"{sid}-test",
            properties=props,
            timestamp=now,
            is_test=True,
        )
    )
    await session.flush()


def _window():
    now = datetime.now(UTC)
    return now - timedelta(days=1), now + timedelta(minutes=1)


async def test_analytics_functions_exclude_test(db_session, singleton_user):
    from app.services import analytics as a

    project = await _project(db_session, singleton_user.id, "analytics-excl.com")
    await _seed_pair(db_session, project.id, properties={"plan": "pro", "tags": ["x"]})
    start, end = _window()
    kw = {"project_id": project.id, "event_name": "signup", "start": start, "end": end}

    assert await a.count_events(db_session, **kw) == 1
    series = await a.events_over_time(db_session, granularity="day", **kw)
    assert sum(r["count"] for r in series) == 1
    assert await a.top_properties(db_session, property_key="plan", **kw) == [
        {"value": "pro", "count": 1}
    ]
    assert await a.top_array_elements(db_session, property_key="tags", **kw) == [
        {"value": "x", "count": 1}
    ]
    names = await a.list_event_names(db_session, project_id=project.id)
    assert [(n["event_name"], n["count"]) for n in names] == [("signup", 1)]
    cmp = await a.compare_periods(
        db_session,
        project_id=project.id,
        event_name="signup",
        current_start=start,
        current_end=end,
        previous_start=start - timedelta(days=1),
        previous_end=start,
    )
    assert cmp["current"] == 1


async def test_property_keys_ignore_test_only_keys(db_session, singleton_user):
    from app.services import analytics as a

    project = await _project(db_session, singleton_user.id, "keys-excl.com")
    now = datetime.now(UTC) - timedelta(minutes=5)
    db_session.add(
        Event(
            project_id=project.id,
            event_name="e",
            session_id="r",
            properties={"a": 1},
            timestamp=now,
        )
    )
    db_session.add(
        Event(
            project_id=project.id,
            event_name="e",
            session_id="t",
            properties={"mock_only": ["z"]},
            timestamp=now,
            is_test=True,
        )
    )
    await db_session.flush()
    start, end = _window()
    kw = {"project_id": project.id, "event_name": "e", "start": start, "end": end}

    assert await a.list_property_keys(db_session, **kw) == ["a"]
    assert await a.find_array_property_keys(db_session, **kw) == set()


async def test_test_events_only_project_has_no_event_names(db_session, singleton_user):
    from app.services import analytics as a

    project = await _project(db_session, singleton_user.id, "only-test.com")
    db_session.add(
        Event(project_id=project.id, event_name="e", session_id="t", properties={}, is_test=True)
    )
    await db_session.flush()
    assert await a.list_event_names(db_session, project_id=project.id) == []


async def test_aggregation_rollup_excludes_test(db_session, singleton_user):
    from app.models.aggregation import Aggregation, AggregationPeriod
    from app.services.aggregation import run_aggregation_cron

    project = await _project(db_session, singleton_user.id, "agg-excl.com")
    await _seed_pair(db_session, project.id)
    # Seed at "now" so the current hour bucket includes both rows.
    for e in (
        await db_session.execute(select(Event).where(Event.project_id == project.id))
    ).scalars():
        e.timestamp = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    await run_aggregation_cron(db_session)
    row = (
        await db_session.execute(
            select(Aggregation).where(
                Aggregation.project_id == project.id,
                Aggregation.period == AggregationPeriod.day,
            )
        )
    ).scalar_one()
    assert row.count == 1


async def test_threshold_alert_ignores_test_rows(db_session, singleton_user):
    from app.models.alert import Alert, AlertCondition
    from app.services.events import evaluate_alerts

    project = await _project(db_session, singleton_user.id, "alert-excl.com")
    db_session.add(
        Alert(
            project_id=project.id,
            event_name="signup",
            condition=AlertCondition.threshold,
            threshold_n=2,
        )
    )
    await _seed_pair(db_session, project.id)
    fired = await evaluate_alerts(db_session, project_id=project.id, event_name="signup")
    assert fired == []


async def test_funnel_excludes_test(db_session, singleton_user):
    from app.services.funnels import analyze_funnel, create_funnel

    project = await _project(db_session, singleton_user.id, "funnel-excl.com")
    now = datetime.now(UTC) - timedelta(minutes=10)
    for sid, is_test in (("real", False), ("test", True)):
        db_session.add(
            Event(
                project_id=project.id,
                event_name="view",
                session_id=sid,
                properties={},
                timestamp=now,
                is_test=is_test,
            )
        )
        db_session.add(
            Event(
                project_id=project.id,
                event_name="buy",
                session_id=sid,
                properties={},
                timestamp=now + timedelta(minutes=1),
                is_test=is_test,
            )
        )
    funnel = await create_funnel(
        db_session, project_id=project.id, name="f", steps=["view", "buy"], time_window=3600
    )
    start, end = _window()
    result = await analyze_funnel(db_session, funnel=funnel, start=start, end=end)
    assert [r["count"] for r in result] == [1, 1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_events_is_test.py -k "analytics or property_keys or only_project or aggregation or threshold or funnel" -v`
Expected: FAIL. Counts are 2 where 1 is expected.

- [ ] **Step 3: Filter `analytics.py`**

In `app/services/analytics.py`, change the import to:

```python
from app.models.event import REAL_EVENTS, Event
```

Add `REAL_EVENTS,` as the last positional argument of the `.where(...)` call in each of: `count_events`, `events_over_time`, `top_properties`, `list_property_keys` (inside the subquery). In `list_event_names` change `.where(Event.project_id == project_id)` to `.where(Event.project_id == project_id, REAL_EVENTS)`.

In both raw SQL strings (`top_array_elements`, `find_array_property_keys`), add this line directly after `WHERE project_id = :pid`:

```sql
          AND NOT is_test
```

Do NOT change `list_recent_events` here (Task 6 handles it).

- [ ] **Step 4: Filter `aggregation.py`**

In `app/services/aggregation.py`, change the import to `from app.models.event import REAL_EVENTS, Event`. In `run_aggregation_cron` change:

```python
            .where(Event.timestamp >= period_start, Event.timestamp <= now)
```

to:

```python
            .where(Event.timestamp >= period_start, Event.timestamp <= now, REAL_EVENTS)
```

Leave the retention `delete(Event)` in `run_retention_cron` unchanged: it must delete test rows too.

- [ ] **Step 5: Filter `evaluate_alerts`**

In `app/services/events.py`, change the import to `from app.models.event import REAL_EVENTS, Event`. In the threshold branch add `REAL_EVENTS,` after `Event.received_at >= today_start,`.

- [ ] **Step 6: Filter `funnels.py`**

In `app/services/funnels.py`, change the import to `from app.models.event import REAL_EVENTS, Event`. Add `REAL_EVENTS,` as the last argument of the `.where(...)` in the `step0` CTE and in the per-step `step_cte`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_events_is_test.py tests/test_funnels.py tests/test_alerts.py tests/test_event_array_properties.py tests/test_phase4.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/services/analytics.py app/services/aggregation.py app/services/events.py app/services/funnels.py tests/test_events_is_test.py
git commit -m "feat(analytics): exclude test events from queries, rollups, funnels and alerts"
```

---

### Task 5: Exclude test events from bot reports, digest and export

**Files:**
- Modify: `app/bot/handlers/reports.py`
- Modify: `app/bot/handlers/digest.py`
- Modify: `app/bot/handlers/export.py`
- Test: `tests/test_events_is_test.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_events_is_test.py`:

```python
# ── Bot reports, digest, export ───────────────────────────────────────────────


async def test_reports_menu_excludes_test(session_factory, singleton_user):
    from unittest.mock import AsyncMock, MagicMock

    from app.bot.handlers.reports import show_reports_menu

    async with session_factory() as session:
        project = await _project(session, singleton_user.id, "reports-excl.com")
        await _seed_pair(session, project.id)
        await session.commit()
        pid = str(project.id)

    query = MagicMock()
    query.edit_message_text = AsyncMock()
    await show_reports_menu(query, pid, singleton_user.id)
    text = query.edit_message_text.call_args[0][0]
    assert "Total events: <b>1</b>" in text
    assert "Unique sessions: <b>1</b>" in text


async def test_digest_excludes_test(session_factory, singleton_user):
    from app.bot.handlers.digest import _project_digest
    from app.models.alert import Alert, AlertCondition

    async with session_factory() as session:
        project = await _project(session, singleton_user.id, "digest-excl.com")
        session.add(
            Alert(project_id=project.id, event_name="signup", condition=AlertCondition.every)
        )
        await _seed_pair(session, project.id)
        await session.commit()
        d = await _project_digest(session, project, datetime.now(UTC))

    assert d.sessions_curr == 1
    assert [(r[0], r[1]) for r in d.events] == [("signup", 1)]


async def test_export_excludes_test(db_session, singleton_user):
    from app.bot.handlers.export import _build_csv

    project = await _project(db_session, singleton_user.id, "export-excl.com")
    await _seed_pair(db_session, project.id)
    data, count = await _build_csv(db_session, project.id)
    assert count == 1
    assert b"s-test" not in data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_events_is_test.py -k "reports or digest or export" -v`
Expected: FAIL (counts are 2).

- [ ] **Step 3: Filter `reports.py`**

In `app/bot/handlers/reports.py`, change the import to `from app.models.event import REAL_EVENTS, Event`. In each of the 4 `.where(...)` calls on `Event` (one in the top-event fallback, three in `show_reports_menu`), add `REAL_EVENTS` as the last argument. Example:

```python
            .where(Event.project_id == pid, Event.timestamp >= seven_days_ago, REAL_EVENTS)
```

- [ ] **Step 4: Filter `digest.py`**

In `app/bot/handlers/digest.py`, change the import to `from app.models.event import REAL_EVENTS, Event`. Add `REAL_EVENTS` as the last argument of the 3 `.where(...)` calls on `Event` in `_project_digest` (`sessions_curr`, `sessions_prev`, `counts_rows`). Do not touch the `Alert` query.

- [ ] **Step 5: Filter `export.py`**

In `app/bot/handlers/export.py`, change the import to `from app.models.event import REAL_EVENTS, Event`, and change the stream query to:

```python
        select(Event)
        .where(Event.project_id == project_id, REAL_EVENTS)
        .order_by(Event.timestamp)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_events_is_test.py tests/test_reports.py tests/test_export_handler.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/bot/handlers/reports.py app/bot/handlers/digest.py app/bot/handlers/export.py tests/test_events_is_test.py
git commit -m "feat(bot): exclude test events from reports, digest and export"
```

---

### Task 6: Debug views show test events, marked 🧪

**Files:**
- Modify: `app/services/analytics.py` (`list_recent_events`)
- Modify: `app/bot/handlers/events.py` (recent activity)
- Modify: `app/bot/handlers/doctor.py`
- Modify: `app/mcp/tools/_schemas.py`, `app/mcp/tools/data.py`, `app/mcp/tools/setup.py`
- Test: `tests/test_events_is_test.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_events_is_test.py`:

```python
# ── Debug views include test events ───────────────────────────────────────────


async def test_list_recent_events_includes_test_with_flag(db_session, singleton_user):
    from app.services.analytics import list_recent_events

    project = await _project(db_session, singleton_user.id, "recent-incl.com")
    await _seed_pair(db_session, project.id)
    rows = await list_recent_events(db_session, project_id=project.id)
    assert sorted(r["is_test"] for r in rows) == [False, True]


def test_history_groups_split_on_is_test():
    from app.bot.handlers.events import _group_consecutive

    now = datetime.now(UTC)
    groups = _group_consecutive(
        [
            {"event_name": "signup", "timestamp": now, "is_test": True},
            {"event_name": "signup", "timestamp": now, "is_test": True},
            {"event_name": "signup", "timestamp": now, "is_test": False},
        ]
    )
    assert [(g["event_name"], g["count"], g["is_test"]) for g in groups] == [
        ("signup", 2, True),
        ("signup", 1, False),
    ]


async def test_doctor_reports_test_count(session_factory, singleton_user):
    from unittest.mock import AsyncMock, MagicMock

    from app.bot.handlers.doctor import doctor_command

    async with session_factory() as session:
        project = await _project(session, singleton_user.id, "doctor-test.com")
        await _seed_pair(session, project.id)
        await session.commit()

    update = MagicMock()
    update.effective_chat.id = ADMIN_ID
    update.effective_user.id = ADMIN_ID
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    await doctor_command(update, MagicMock())
    text = update.message.reply_text.call_args[0][0]
    assert "Events: 2" in text
    assert "🧪 1 test" in text
```

MCP tool tests: open `tests/mcp/` and find the existing tests for `recent_events` and `verify_integration` (`grep -rln "recent_events\|verify_integration" tests/mcp`). In the same file and with the same fixtures, add two tests:
1. Seed one real and one test event (use `Event(..., is_test=True)` as in `_seed_pair`). Call `recent_events`. Assert the result has 2 rows and exactly one has `is_test is True`.
2. Seed the same pair. Call `verify_integration(project_id, since_minutes=30)`. Assert `count == 2` and `test_count == 1`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_events_is_test.py -k "recent or history or doctor" tests/mcp -v`
Expected: FAIL (`KeyError: 'is_test'`, missing `🧪`, missing `test_count`).

- [ ] **Step 3: Return `is_test` from `list_recent_events`**

In `app/services/analytics.py`, replace the body of `list_recent_events`:

```python
"""Return the most recent *limit* events for a project, newest first.

Includes test events: this feeds debug views (recent activity,
``recent_events``, ``verify_integration``).

Returns ``[{"event_name": str, "timestamp": datetime, "is_test": bool}, ...]``.
"""

# includes test events on purpose
result = await session.execute(
    select(Event.event_name, Event.received_at, Event.is_test)
    .where(Event.project_id == project_id)
    .order_by(Event.received_at.desc())
    .limit(limit)
)
return [
    {"event_name": r.event_name, "timestamp": r.received_at, "is_test": r.is_test} for r in result
]
```

- [ ] **Step 4: Mark test rows in the bot recent activity**

In `app/bot/handlers/events.py`:

`_HistoryGroup` gets a new field:

```python
class _HistoryGroup(TypedDict):
    event_name: str
    count: int
    latest: datetime
    is_test: bool
```

`_group_consecutive` body becomes:

```python
    grouped: list[_HistoryGroup] = []
    for evt in events:
        name: str = evt["event_name"]
        ts: datetime = evt["timestamp"]
        is_test: bool = bool(evt.get("is_test", False))
        if grouped and grouped[-1]["event_name"] == name and grouped[-1]["is_test"] == is_test:
            grouped[-1]["count"] += 1
        else:
            grouped.append({"event_name": name, "count": 1, "latest": ts, "is_test": is_test})
    return grouped
```

In `show_history_menu`, change the line loop to:

```python
    for g in groups:
        name = html.escape(g["event_name"])
        count = g["count"]
        rel = _format_relative(now, g["latest"])
        prefix = f"<b>({count})</b> " if count > 1 else ""
        mark = "🧪 " if g["is_test"] else ""
        lines.append(f"{mark}{prefix}{name}  <i>· {rel}</i>")
```

- [ ] **Step 5: Show the test count in `/doctor`**

In `app/bot/handlers/doctor.py`, replace the per-project query with:

```python
        # includes test events on purpose
        result = await session.execute(
            select(
                func.count().label("total"),
                func.count().filter(Event.is_test).label("test"),
                func.max(Event.received_at).label("last_seen"),
            ).where(Event.project_id == project.id)
        )
        row = result.one()
        total: int = int(row.total or 0)
        test_total: int = int(row.test or 0)
        last_seen: datetime | None = row.last_seen
        test_note = f" · 🧪 {test_total:,} test" if test_total else ""
```

Then append `{test_note}` to the two event lines that print a total:

```python
lines.append(f"  ⚠️ Events: {total:,}{test_note} (last {_relative(now, last_seen)} — stale)")
```

```python
            lines.append(f"  ✅ Events: {total:,}{test_note} (last {_relative(now, last_seen)})")
```

- [ ] **Step 6: MCP schemas**

In `app/mcp/tools/_schemas.py`:

```python
class RecentEventRow(BaseModel):
    """One row of :func:`recent_events`."""

    event_name: str
    timestamp: str | None = None
    is_test: bool = Field(False, description="True for test events (excluded from analytics).")
```

```python
class VerifyIntegrationResult(BaseModel):
    """Result of :func:`verify_integration`."""

    count: int
    since_minutes: int
    is_receiving: bool
    test_count: int = Field(0, description="How many of ``count`` are test events.")
```

- [ ] **Step 7: MCP `recent_events`**

In `app/mcp/tools/data.py`, `recent_events`: add to the docstring, before `Response:`:

```
        Includes test events (``is_test: true``). Test events are stored but
        every analytics tool ignores them.
```

Change the response line in the docstring to `Response: ``{"events": [{"event_name": str, "timestamp": iso, "is_test": bool}, ...]}``.` and build rows with:

```python
        events = [
            RecentEventRow(
                event_name=r["event_name"],
                timestamp=r["timestamp"].isoformat() if r["timestamp"] else None,
                is_test=r["is_test"],
            )
            for r in rows
        ]
```

- [ ] **Step 8: MCP `verify_integration`**

In `app/mcp/tools/setup.py`, `verify_integration`: add to the docstring `Counts test events too; ``test_count`` says how many.`, update the response line to include `"test_count": int`, and change the result block to:

```python
        in_window = [r for r in rows if r["timestamp"] is not None and r["timestamp"] >= start]
        count = len(in_window)
        return VerifyIntegrationResult(
            count=count,
            since_minutes=since_minutes,
            is_receiving=count > 0,
            test_count=sum(1 for r in in_window if r["is_test"]),
        )
```

- [ ] **Step 9: Analytics MCP tool docstrings**

Add the sentence `Test events are excluded.` to the docstring of: `query_events`, `compare_periods`, `top_pages`, `top_property_values`, `list_property_keys` (all in `app/mcp/tools/data.py`) and `list_event_names` (`app/mcp/tools/projects.py`). Do not change `top_taps` (test taps are never stored).

- [ ] **Step 10: Run tests to verify they pass**

Run: `uv run pytest tests/test_events_is_test.py tests/test_doctor_handler.py tests/mcp -v`
Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add app/services/analytics.py app/bot/handlers/events.py app/bot/handlers/doctor.py app/mcp/tools tests
git commit -m "feat(debug): show test events in recent activity, doctor and MCP debug tools"
```

---

### Task 7: Static guard against unfiltered event queries

**Files:**
- Create: `tests/test_real_events_guard.py`

- [ ] **Step 1: Write the guard (with a self-check)**

Create `tests/test_real_events_guard.py`:

```python
"""Static guard: every analytics query on ``events`` excludes test events.

A ``.where(...)`` that references ``Event.`` must also contain ``REAL_EVENTS``,
unless the statement carries the marker comment
``# includes test events on purpose`` (debug views). ``delete(Event)`` is
exempt (retention must delete test rows too). Raw SQL strings that read
``FROM events`` must contain ``NOT is_test``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
MARKER = "# includes test events on purpose"
_EVENT_REF = re.compile(r"\bEvent\.")
_FROM_EVENTS = re.compile(r"\bFROM\s+events\b", re.IGNORECASE)


def _is_delete_chain(node: ast.AST) -> bool:
    while isinstance(node, (ast.Call, ast.Attribute)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id == "delete"
        node = node.func if isinstance(node, ast.Call) else node.value
    return False


def find_violations(source: str, filename: str = "<src>") -> list[str]:
    tree = ast.parse(source)
    lines = source.splitlines()
    out: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "where"
        ):
            args_src = " ".join(ast.get_source_segment(source, a) or "" for a in node.args)
            if not _EVENT_REF.search(args_src) or "REAL_EVENTS" in args_src:
                continue
            if _is_delete_chain(node.func.value):
                continue
            start = max(node.lineno - 3, 0)
            window = "\n".join(lines[start : node.end_lineno])
            if MARKER in window:
                continue
            out.append(f"{filename}:{node.lineno}: .where() on Event without REAL_EVENTS")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _FROM_EVENTS.search(node.value) and "NOT is_test" not in node.value:
                out.append(f"{filename}:{node.lineno}: raw SQL on events without NOT is_test")
    return out


def test_guard_catches_unfiltered_query():
    bad = (
        "q = select(Event).where(Event.project_id == pid)\n"
        "s = text('SELECT 1 FROM events WHERE project_id = :p')\n"
    )
    assert len(find_violations(bad)) == 2


def test_guard_accepts_filtered_marked_and_delete():
    ok = (
        "q = select(Event).where(Event.project_id == pid, REAL_EVENTS)\n"
        "# includes test events on purpose\n"
        "r = select(Event).where(Event.project_id == pid)\n"
        "d = delete(Event).where(Event.received_at < cutoff)\n"
        "s = text('SELECT 1 FROM events WHERE project_id = :p AND NOT is_test')\n"
    )
    assert find_violations(ok) == []


def test_no_unfiltered_event_queries_in_app():
    violations: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = str(path.relative_to(APP_DIR.parent))
        violations += find_violations(path.read_text(), rel)
    assert violations == [], "Unfiltered event queries:\n" + "\n".join(violations)
```

- [ ] **Step 2: Run the guard**

Run: `uv run pytest tests/test_real_events_guard.py -v`
Expected: PASS. If `test_no_unfiltered_event_queries_in_app` lists a violation, it is a query Tasks 4-6 missed: add `REAL_EVENTS`, or (only for a debug view) the marker comment. Do not weaken the guard.

- [ ] **Step 3: Self-check against a reverted filter**

Temporarily remove `REAL_EVENTS` from `count_events` in `app/services/analytics.py`, run `pytest tests/test_real_events_guard.py::test_no_unfiltered_event_queries_in_app -v`, confirm it FAILS naming `app/services/analytics.py`, then restore the line (`git checkout app/services/analytics.py` if nothing else changed there since the last commit).

- [ ] **Step 4: Commit**

```bash
git add tests/test_real_events_guard.py
git commit -m "test: guard that analytics queries exclude test events"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md`
- Modify: `openapi.json` (generated)

- [ ] **Step 1: README, REST section**

In `README.md`, section `### Track events (REST API)`, after the closing fence of the curl example, insert:

````markdown
#### Test events

Add `"test": true` to any `/track`, `/pageview` or `/taps` body to mark it as
test data:

```bash
curl -X POST https://your-server.com/api/v1/track \
  -H "Content-Type: application/json" \
  -d '{"api_key": "proj_xxxxxxxxxxxx", "event_name": "purchase", "session_id": "dev", "test": true}'
```

Test events are stored, but reports, digests, funnels, exports, alerts and the
MCP analytics tools ignore them. Recent activity, `/doctor`, and the MCP
`recent_events` and `verify_integration` tools still show them, marked 🧪, so
you can check an integration. Test taps are not stored.

Events sent from `localhost`, `*.localhost`, `127.0.0.1`, `::1` or `0.0.0.0`
(by `Origin` header or pageview URL) are marked as test automatically. If a
project has an origin allowlist, localhost requests are still rejected unless
the allowlist includes them.
````

Do not add a `test` option to the JS or Flutter README snippets in this PR: the SDKs do not have it yet. Their READMEs get it in the SDK PRs.

- [ ] **Step 2: Regenerate the OpenAPI spec**

Run: `uv run python scripts/export_openapi.py && uv run python scripts/export_openapi.py --check`
Expected: `openapi.json is up to date.` and `git diff --stat openapi.json` shows the new `test` and `is_test` fields.

- [ ] **Step 3: Commit**

```bash
git add README.md openapi.json
git commit -m "docs: document test events"
```

---

### Task 9: Full verification and PR

- [ ] **Step 1: Lint, types, full suite**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy app && uv run pytest -q`
Expected: all clean. If `ruff format --check` fails, run `uv run ruff format .` and commit the result. Report the exact pass/fail counts.

- [ ] **Step 2: Migration round-trip**

Run: `uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: no errors.

- [ ] **Step 3: Check the branch, then push and open the PR**

```bash
git branch --show-current
git fetch origin && git log --oneline origin/main -3
gh pr list --head "$(git branch --show-current)" --state all
git push -u origin HEAD
gh pr create --title "Test events: store but exclude from analytics" --body-file /tmp/pr-body.md
```

The PR body states: what `test: true` does, the localhost rule, which surfaces exclude and which include test events, the migration (`0015`, metadata-only), and that SDK and site docs follow in separate PRs. End it with:

```
🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

- [ ] **Step 4: After merge and deploy, verify live**

Send one real and one test event to the live instance with a test project, then confirm with the tgram MCP: `recent_events` shows both (one with `is_test: true`), `query_events` / `list_event_names` count only the real one. Report what was checked.

---

## Follow-ups (separate PRs, not in this plan)

1. Site repo `tgram-analytics/tgram-analytics`: `docs.html` section `#test-events` linked from Quick Start; one line in `llms.txt` under "Event tracking".
2. SDK repos `tgram-analytics-js`, `tgram-analytics-flutter`, `tgram-analytics-py`: a `test` option that adds `"test": true` to every request body (and taps for JS), plus a "Test mode" README section. Read each SDK's real init API first: the Flutter SDK uses `TgAnalytics.init(apiKey:, serverUrl:)`, so the option there is `test: kDebugMode`, not the `TGA.init` form in the spec table. Update the spec's SDK table when these land.
