"""Events marked as test: stored, excluded from analytics, shown in debug views."""

from __future__ import annotations

import uuid

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
