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


async def test_insert_event_stores_is_test(singleton_user, db_session):
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


async def test_analytics_functions_exclude_test(singleton_user, db_session):
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


async def test_property_keys_ignore_test_only_keys(singleton_user, db_session):
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


async def test_test_events_only_project_has_no_event_names(singleton_user, db_session):
    from app.services import analytics as a

    project = await _project(db_session, singleton_user.id, "only-test.com")
    db_session.add(
        Event(project_id=project.id, event_name="e", session_id="t", properties={}, is_test=True)
    )
    await db_session.flush()
    assert await a.list_event_names(db_session, project_id=project.id) == []


async def test_aggregation_rollup_excludes_test(singleton_user, db_session, monkeypatch):
    from app.models.aggregation import Aggregation, AggregationPeriod
    from app.services.aggregation import run_aggregation_cron

    # Freeze the clock the service reads so the day bucket is stable at any hour.
    fixed = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr("app.services.aggregation.datetime", _Frozen)

    project = await _project(db_session, singleton_user.id, "agg-excl.com")
    await _seed_pair(db_session, project.id)
    for e in (
        await db_session.execute(select(Event).where(Event.project_id == project.id))
    ).scalars():
        e.timestamp = fixed - timedelta(minutes=1)
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


async def test_threshold_alert_ignores_test_rows(singleton_user, db_session):
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


async def test_funnel_excludes_test(singleton_user, db_session):
    from app.services.funnels import analyze_funnel, create_funnel

    project = await _project(db_session, singleton_user.id, "funnel-excl.com")
    now = datetime.now(UTC) - timedelta(minutes=10)
    for sid, view_test, buy_test in (
        ("real", False, False),
        ("test", True, True),
        ("mixed", False, True),
    ):
        db_session.add(
            Event(
                project_id=project.id,
                event_name="view",
                session_id=sid,
                properties={},
                timestamp=now,
                is_test=view_test,
            )
        )
        db_session.add(
            Event(
                project_id=project.id,
                event_name="buy",
                session_id=sid,
                properties={},
                timestamp=now + timedelta(minutes=1),
                is_test=buy_test,
            )
        )
    funnel = await create_funnel(
        db_session, project_id=project.id, name="f", steps=["view", "buy"], time_window=3600
    )
    start, end = _window()
    result = await analyze_funnel(db_session, funnel=funnel, start=start, end=end)
    assert [r["count"] for r in result] == [2, 1]


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


async def test_export_excludes_test(singleton_user, db_session):
    from app.bot.handlers.export import _build_csv

    project = await _project(db_session, singleton_user.id, "export-excl.com")
    await _seed_pair(db_session, project.id)
    data, count = await _build_csv(db_session, project.id)
    assert count == 1
    assert b"s-test" not in data
