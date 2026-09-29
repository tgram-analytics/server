"""POST /api/v1/taps: storage, validation, and the shared ingestion guards."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import select

from app.models.tap import Tap

_CHROME_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)

_GOOGLEBOT_SMARTPHONE_UA = (
    "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.7390.122 Mobile "
    "Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)


async def _create_project(api_client, name: str, allowlist: list[str] | None = None) -> dict:
    payload: dict = {"name": name, "admin_chat_id": 111}
    if allowlist is not None:
        payload["domain_allowlist"] = allowlist
    resp = await api_client.post("/api/v1/internal/projects", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _body(api_key: str, **overrides) -> dict:
    body = {
        "api_key": api_key,
        "session_id": str(uuid.uuid4()),
        "path": "/browse?sort=new",
        "viewport": "mobile",
        "vw": 390,
        "taps": [
            {"x": 0.512, "y": 1330, "el": 'button "Browse albums"'},
            {"x": 0.1, "y": 40},
        ],
        "scroll": 0.62,
    }
    body.update(overrides)
    return body


async def _rows(db_session, project_id: str) -> list[Tap]:
    await db_session.invalidate()
    result = await db_session.execute(
        select(Tap).where(Tap.project_id == uuid.UUID(project_id)).order_by(Tap.id)
    )
    return list(result.scalars().all())


async def test_taps_endpoint_stores_rows_without_session(api_client, db_session):
    data = await _create_project(api_client, "taps-store.com")
    body = _body(data["api_key"])

    resp = await api_client.post(
        "/api/v1/taps", json=body, headers={"User-Agent": _CHROME_MOBILE_UA}
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"status": "accepted"}

    rows = await _rows(db_session, data["id"])
    assert len(rows) == 3
    taps = [r for r in rows if r.kind == "tap"]
    scrolls = [r for r in rows if r.kind == "scroll"]
    assert len(taps) == 2
    assert len(scrolls) == 1

    first = taps[0]
    assert first.path == "/browse?sort=new"
    assert first.device == "mobile"
    assert first.vw == 390
    assert abs(first.x - 0.512) < 1e-6
    assert first.y == 1330
    assert first.label == 'button "Browse albums"'
    assert first.depth is None
    assert taps[1].label is None

    assert scrolls[0].x is None and scrolls[0].y is None
    assert abs(scrolls[0].depth - 0.62) < 1e-6

    # Nothing that could link two rows to one visitor exists in the table.
    columns = {c.name for c in Tap.__table__.columns}
    assert "session_id" not in columns
    assert "visitor_hash" not in columns
    assert "timestamp" not in columns
    db_columns = (
        await db_session.execute(
            sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = 'taps'")
        )
    ).scalars()
    assert set(db_columns) == columns
    assert body["session_id"] not in str([vars(r) for r in rows])


async def test_taps_scroll_only_batch_is_accepted(api_client, db_session):
    data = await _create_project(api_client, "taps-scroll-only.com")
    resp = await api_client.post("/api/v1/taps", json=_body(data["api_key"], taps=[], scroll=0.3))
    assert resp.status_code == 202, resp.text
    rows = await _rows(db_session, data["id"])
    assert [r.kind for r in rows] == ["scroll"]


async def test_taps_zero_viewport_width_is_accepted(api_client, db_session):
    """Hidden tabs and iframes report innerWidth = 0; the batch is kept."""
    data = await _create_project(api_client, "taps-vw-zero.com")
    resp = await api_client.post("/api/v1/taps", json=_body(data["api_key"], vw=0))
    assert resp.status_code == 202, resp.text
    rows = await _rows(db_session, data["id"])
    assert len(rows) == 3
    assert {r.vw for r in rows} == {0}


async def test_taps_rejects_more_than_50(api_client, db_session):
    data = await _create_project(api_client, "taps-too-many.com")
    taps = [{"x": 0.5, "y": i} for i in range(51)]
    resp = await api_client.post("/api/v1/taps", json=_body(data["api_key"], taps=taps))
    assert resp.status_code == 422
    assert await _rows(db_session, data["id"]) == []

    ok = await api_client.post("/api/v1/taps", json=_body(data["api_key"], taps=taps[:50]))
    assert ok.status_code == 202, ok.text


async def test_taps_rejects_out_of_range_fraction(api_client):
    data = await _create_project(api_client, "taps-range.com")
    key = data["api_key"]
    bad_bodies = [
        _body(key, taps=[{"x": 1.2, "y": 10}]),
        _body(key, taps=[{"x": -0.1, "y": 10}]),
        _body(key, taps=[{"x": 0.5, "y": -1}]),
        _body(key, taps=[{"x": 0.5, "y": 100_001}]),
        _body(key, scroll=1.5),
        _body(key, vw=-1),
        _body(key, vw=10_001),
        _body(key, viewport="watch"),
        _body(key, path=""),
    ]
    for body in bad_bodies:
        resp = await api_client.post("/api/v1/taps", json=body)
        assert resp.status_code == 422, body


async def test_taps_rejects_empty_batch(api_client, db_session):
    data = await _create_project(api_client, "taps-empty.com")
    resp = await api_client.post("/api/v1/taps", json=_body(data["api_key"], taps=[], scroll=None))
    assert resp.status_code == 422
    assert "nothing to store" in resp.text
    assert await _rows(db_session, data["id"]) == []


async def test_taps_drops_crawlers(api_client, db_session):
    data = await _create_project(api_client, "taps-bot.com")
    resp = await api_client.post(
        "/api/v1/taps",
        json=_body(data["api_key"]),
        headers={"User-Agent": _GOOGLEBOT_SMARTPHONE_UA},
    )
    assert resp.status_code == 202, resp.text
    assert await _rows(db_session, data["id"]) == []


async def test_taps_uses_project_rate_limit(api_client, async_engine):
    data = await _create_project(api_client, "taps-ratelimit.com")
    async with async_engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE projects SET rate_limit_per_second = 1 WHERE id = :id"),
            {"id": data["id"]},
        )

    first = await api_client.post("/api/v1/taps", json=_body(data["api_key"]))
    assert first.status_code == 202, first.text
    second = await api_client.post("/api/v1/taps", json=_body(data["api_key"]))
    assert second.status_code == 429


async def test_taps_respects_origin_allowlist(api_client, db_session):
    data = await _create_project(api_client, "taps-origin.com", allowlist=["taps-origin.com"])
    blocked = await api_client.post(
        "/api/v1/taps",
        json=_body(data["api_key"]),
        headers={"Origin": "https://evil.example"},
    )
    assert blocked.status_code == 403
    assert await _rows(db_session, data["id"]) == []

    allowed = await api_client.post(
        "/api/v1/taps",
        json=_body(data["api_key"]),
        headers={"Origin": "https://taps-origin.com"},
    )
    assert allowed.status_code == 202, allowed.text


async def test_taps_invalid_key_400(api_client):
    resp = await api_client.post("/api/v1/taps", json=_body("proj_invalid"))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid API key"


async def test_taps_invalid_key_spends_invalid_key_budget(api_client, monkeypatch):
    import app.api.ingestion as ing

    monkeypatch.setattr(ing, "_invalid_key_rate_limit", 2)
    codes = [
        (await api_client.post("/api/v1/taps", json=_body("proj_invalid"))).status_code
        for _ in range(3)
    ]
    assert codes == [400, 400, 429]


async def test_taps_label_truncated_to_80(api_client, db_session):
    data = await _create_project(api_client, "taps-label.com")
    long_label = "a" * 200
    resp = await api_client.post(
        "/api/v1/taps",
        json=_body(data["api_key"], taps=[{"x": 0.5, "y": 10, "el": long_label}], scroll=None),
    )
    assert resp.status_code == 202, resp.text
    rows = await _rows(db_session, data["id"])
    assert len(rows) == 1
    assert rows[0].label == "a" * 80


def test_openapi_has_taps():
    import json
    from pathlib import Path

    spec = json.loads((Path(__file__).resolve().parents[1] / "openapi.json").read_text())
    assert "/api/v1/taps" in spec["paths"]
    assert "post" in spec["paths"]["/api/v1/taps"]
