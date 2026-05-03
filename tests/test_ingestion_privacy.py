"""Phase 4.2 verification: ingestion path persists privacy fields.

Confirms that POST /api/v1/track:
* derives ``visitor_hash`` (16 hex chars) from request IP + UA + project,
* parses the UA into ``browser`` / ``os`` / ``device_type``,
* never persists the raw UA into ``properties``.
"""

from __future__ import annotations

import string
import uuid

from sqlalchemy import select

from app.models.event import Event

_CHROME_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


async def _create_project(api_client, name: str) -> dict:
    resp = await api_client.post(
        "/api/v1/internal/projects",
        json={"name": name, "admin_chat_id": 111},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_track_persists_visitor_hash_and_parsed_ua(api_client, db_session):
    """A track call records visitor_hash, browser, os, device_type."""
    data = await _create_project(api_client, name="privacy-track.com")
    project_id = uuid.UUID(data["id"])

    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "purchase",
            "session_id": str(uuid.uuid4()),
            "properties": {"plan": "pro"},
        },
        headers={"User-Agent": _CHROME_DESKTOP_UA},
    )
    assert resp.status_code == 202, resp.text

    await db_session.invalidate()

    rows = (
        (
            await db_session.execute(
                select(Event).where(Event.project_id == project_id, Event.event_name == "purchase")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    ev = rows[0]

    # visitor_hash: 16 lowercase hex chars
    assert ev.visitor_hash is not None
    assert len(ev.visitor_hash) == 16
    assert all(c in string.hexdigits.lower() for c in ev.visitor_hash)

    # parsed UA fields populated
    assert ev.browser == "Chrome"
    assert ev.os == "Mac OS X"
    assert ev.device_type == "desktop"

    # raw UA is NOT in properties
    assert "user-agent" not in {k.lower() for k in ev.properties}
    assert _CHROME_DESKTOP_UA not in str(ev.properties)
    # plan key preserved
    assert ev.properties.get("plan") == "pro"


async def test_pageview_persists_visitor_hash_and_parsed_ua(api_client, db_session):
    """Pageview path also populates the privacy columns."""
    data = await _create_project(api_client, name="privacy-pv.com")
    project_id = uuid.UUID(data["id"])

    resp = await api_client.post(
        "/api/v1/pageview",
        json={
            "api_key": data["api_key"],
            "session_id": str(uuid.uuid4()),
            "url": "https://site.com/landing",
        },
        headers={"User-Agent": _CHROME_DESKTOP_UA},
    )
    assert resp.status_code == 202, resp.text

    await db_session.invalidate()

    rows = (
        (
            await db_session.execute(
                select(Event).where(Event.project_id == project_id, Event.event_name == "pageview")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    ev = rows[0]

    assert ev.visitor_hash is not None
    assert len(ev.visitor_hash) == 16
    assert ev.browser == "Chrome"
    assert ev.os == "Mac OS X"
    assert ev.device_type == "desktop"
    # url stays in properties (existing behavior); raw UA does NOT
    assert ev.properties.get("url") == "https://site.com/landing"
    assert _CHROME_DESKTOP_UA not in str(ev.properties)


async def test_track_drops_pii_keys_silently(api_client, db_session):
    """POST /track with a PII key: 202, DB row has only the clean keys."""
    data = await _create_project(api_client, name="privacy-pii.com")
    project_id = uuid.UUID(data["id"])

    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "signup",
            "session_id": str(uuid.uuid4()),
            "properties": {"email": "leak@x.com", "plan": "pro"},
        },
        headers={"User-Agent": _CHROME_DESKTOP_UA},
    )
    assert resp.status_code == 202, resp.text

    await db_session.invalidate()

    rows = (
        (
            await db_session.execute(
                select(Event).where(Event.project_id == project_id, Event.event_name == "signup")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].properties == {"plan": "pro"}


async def test_track_drops_oversized_properties_silently(api_client, db_session):
    """POST /track with ~5 KB properties: 202, DB row has empty properties dict."""
    data = await _create_project(api_client, name="privacy-oversize.com")
    project_id = uuid.UUID(data["id"])

    # 5 KB nested via a single big string value; passes the 100-entry cap.
    big_props = {"blob": "x" * 5120}
    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "bigevent",
            "session_id": str(uuid.uuid4()),
            "properties": big_props,
        },
        headers={"User-Agent": _CHROME_DESKTOP_UA},
    )
    assert resp.status_code == 202, resp.text

    await db_session.invalidate()

    rows = (
        (
            await db_session.execute(
                select(Event).where(Event.project_id == project_id, Event.event_name == "bigevent")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].properties == {}
