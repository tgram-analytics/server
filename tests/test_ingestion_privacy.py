"""Phase 4.2 verification: ingestion path persists privacy fields.

Confirms that POST /api/v1/track:
* derives ``visitor_hash`` (16 hex chars) from request IP + UA + project,
* parses the UA into ``browser`` / ``os`` / ``device_type``,
* never persists the raw UA into ``properties``.
"""

from __future__ import annotations

import string
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

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


# ── Alert notifications must never see unscrubbed properties ───────────────
#
# The ingestion endpoints scrub PII-named keys before persisting, but the
# alert-evaluation background task renders property keys/values into Telegram
# messages that persist in chat history. It must receive the SAME scrubbed
# dict that is stored — never the raw request properties.


async def test_track_passes_scrubbed_properties_to_alert_evaluation(api_client):
    """POST /track: the alert background task receives scrubbed properties."""
    data = await _create_project(api_client, name="alert-scrub-track.com")
    project_id = uuid.UUID(data["id"])

    with patch("app.api.ingestion._run_alert_evaluation", new=AsyncMock()) as mock_eval:
        resp = await api_client.post(
            "/api/v1/track",
            json={
                "api_key": data["api_key"],
                "event_name": "signup",
                "session_id": str(uuid.uuid4()),
                "properties": {"email": "leak@x.com", "password": "hunter2", "plan": "pro"},
            },
            headers={"User-Agent": _CHROME_DESKTOP_UA},
        )
        assert resp.status_code == 202, resp.text

    mock_eval.assert_awaited_once()
    args = mock_eval.await_args.args
    assert args[0] == project_id
    assert args[1] == "signup"
    assert args[2] == {"plan": "pro"}


async def test_pageview_passes_scrubbed_properties_to_alert_evaluation(api_client):
    """POST /pageview: the alert background task receives scrubbed properties."""
    data = await _create_project(api_client, name="alert-scrub-pv.com")
    project_id = uuid.UUID(data["id"])

    with patch("app.api.ingestion._run_alert_evaluation", new=AsyncMock()) as mock_eval:
        resp = await api_client.post(
            "/api/v1/pageview",
            json={
                "api_key": data["api_key"],
                "session_id": str(uuid.uuid4()),
                "url": "https://site.com/landing",
                "properties": {"email": "leak@x.com", "plan": "pro"},
            },
            headers={"User-Agent": _CHROME_DESKTOP_UA},
        )
        assert resp.status_code == 202, resp.text

    mock_eval.assert_awaited_once()
    args = mock_eval.await_args.args
    assert args[0] == project_id
    assert args[1] == "pageview"
    assert args[2] == {"plan": "pro", "url": "https://site.com/landing"}


async def test_alert_notification_renders_no_pii(api_client, session_factory):
    """End-to-end: a fired alert's Telegram message contains no PII values."""
    from app.models.alert import AlertCondition
    from app.services.alerts import create_alert

    data = await _create_project(api_client, name="alert-scrub-notify.com")
    project_id = uuid.UUID(data["id"])

    async with session_factory() as session:
        await create_alert(
            session,
            project_id=project_id,
            event_name="signup",
            condition=AlertCondition.every,
        )
        await session.commit()

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    # ASGITransport runs BackgroundTasks before the request call returns, so
    # the notification is sent (to the mocked bot) within this block.
    with patch("app.bot.setup.get_bot", return_value=mock_bot):
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

    mock_bot.send_message.assert_awaited_once()
    text = mock_bot.send_message.call_args.kwargs["text"]
    assert "plan" in text
    assert "pro" in text
    assert "leak@x.com" not in text
    assert "email" not in text.lower()
