"""Phase 3 — Event ingestion API tests.

Unit tests run without a DB; integration tests require DATABASE_URL + Docker.
"""

import uuid
from datetime import UTC, datetime, timedelta

# ── Unit tests (no DB) ────────────────────────────────────────────────────


def test_is_origin_allowed_empty_allowlist_accepts_all():
    from app.services.events import is_origin_allowed

    assert is_origin_allowed([], None) is True
    assert is_origin_allowed([], "https://anything.com") is True


def test_is_origin_allowed_accepts_missing_origin_for_server_to_server():
    """No Origin header → server-to-server caller, accept (allowlist guards
    only browser traffic; backend SDKs are authenticated by api key alone)."""
    from app.services.events import is_origin_allowed

    assert is_origin_allowed(["myapp.com"], None) is True


def test_is_origin_allowed_rejects_null_origin():
    """``Origin: null`` (sandboxed iframe / file://) is rejected when an
    allowlist is configured — distinct from a missing header."""
    from app.services.events import is_origin_allowed

    assert is_origin_allowed(["myapp.com"], "null") is False


def test_is_origin_allowed_accepts_matching_host():
    from app.services.events import is_origin_allowed

    assert is_origin_allowed(["myapp.com"], "https://myapp.com") is True


def test_is_origin_allowed_rejects_wrong_host():
    from app.services.events import is_origin_allowed

    assert is_origin_allowed(["myapp.com"], "https://other.com") is False


def test_is_origin_allowed_bare_host_in_allowlist():
    """Allowlist entry without scheme is compared against netloc."""
    from app.services.events import is_origin_allowed

    assert is_origin_allowed(["localhost:3000"], "http://localhost:3000") is True


# ── normalize_origin_entry ────────────────────────────────────────────────


def test_normalize_origin_entry_strips_scheme_and_slash():
    from app.services.events import normalize_origin_entry

    assert normalize_origin_entry("https://example.com/") == "example.com"
    assert normalize_origin_entry("HTTP://Example.com") == "example.com"


def test_normalize_origin_entry_bare_host_passthrough():
    from app.services.events import normalize_origin_entry

    assert normalize_origin_entry("example.com") == "example.com"
    assert normalize_origin_entry("  example.com  ") == "example.com"


def test_normalize_origin_entry_preserves_port():
    from app.services.events import normalize_origin_entry

    assert normalize_origin_entry("http://localhost:3000") == "localhost:3000"


def test_normalize_origin_entry_preserves_wildcard():
    from app.services.events import normalize_origin_entry

    assert normalize_origin_entry("*.example.com") == "*.example.com"
    assert normalize_origin_entry("*.EXAMPLE.com/") == "*.example.com"


def test_normalize_origin_entry_drops_path():
    from app.services.events import normalize_origin_entry

    assert normalize_origin_entry("https://example.com/some/path") == "example.com"


def test_normalize_origin_entry_returns_none_for_empty():
    from app.services.events import normalize_origin_entry

    assert normalize_origin_entry("") is None
    assert normalize_origin_entry("   ") is None
    assert normalize_origin_entry("*.") is None


def test_normalize_origin_entries_dedupes_and_drops_empty():
    from app.services.events import normalize_origin_entries

    result = normalize_origin_entries(
        ["https://a.com/", "a.com", "", "  ", "B.com", "https://b.com"]
    )
    assert result == ["a.com", "b.com"]


# ── Integration helpers ───────────────────────────────────────────────────


async def _create_project(api_client, *, name: str = "test.com", allowlist=None) -> dict:
    """Create a project via the internal API and return the full response body."""
    payload: dict = {"name": name, "admin_chat_id": 123456789}
    if allowlist is not None:
        payload["domain_allowlist"] = allowlist
    resp = await api_client.post("/api/v1/internal/projects", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Integration tests ─────────────────────────────────────────────────────


async def test_track_valid_event_returns_202(api_client):
    data = await _create_project(api_client)
    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "purchase",
            "session_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"


async def test_track_event_stored_in_db(api_client, db_session):
    from sqlalchemy import select

    from app.models.event import Event

    data = await _create_project(api_client, name="db-check.com")
    session_id = str(uuid.uuid4())
    await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "signup",
            "session_id": session_id,
            "properties": {"plan": "pro"},
        },
    )
    result = await db_session.execute(select(Event).where(Event.session_id == session_id))
    event = result.scalar_one_or_none()
    assert event is not None
    assert event.event_name == "signup"
    assert event.properties == {"plan": "pro"}


async def test_track_received_at_is_server_time(api_client, db_session):
    """received_at must be set by the server regardless of client timestamp."""
    from sqlalchemy import select

    from app.models.event import Event

    data = await _create_project(api_client, name="recv-at.com")
    session_id = str(uuid.uuid4())
    # Use a timestamp within the allowed window (< 1 year old) but clearly in the past
    old_ts = (datetime.now(UTC) - timedelta(days=30)).replace(microsecond=0)
    before = datetime.now(UTC)
    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "view",
            "session_id": session_id,
            "timestamp": old_ts.isoformat(),
        },
    )
    after = datetime.now(UTC)
    assert resp.status_code == 202
    # Ensure we see committed data from the API's separate connection
    await db_session.invalidate()
    result = await db_session.execute(select(Event).where(Event.session_id == session_id))
    event = result.scalar_one()
    # timestamp should be the client-provided value, not server time
    assert event.timestamp.year == old_ts.year
    assert event.timestamp.month == old_ts.month
    # received_at must be in the test window (server time)
    assert before <= event.received_at.replace(tzinfo=UTC) <= after


async def test_track_invalid_api_key_returns_400(api_client):
    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": "proj_badbadbadbad",
            "event_name": "purchase",
            "session_id": "s1",
        },
    )
    assert resp.status_code == 400


async def test_track_missing_event_name_returns_422(api_client):
    data = await _create_project(api_client, name="missing-event.com")
    resp = await api_client.post(
        "/api/v1/track",
        json={"api_key": data["api_key"], "session_id": "s1"},
    )
    assert resp.status_code == 422


async def test_track_missing_session_id_returns_422(api_client):
    data = await _create_project(api_client, name="missing-session.com")
    resp = await api_client.post(
        "/api/v1/track",
        json={"api_key": data["api_key"], "event_name": "click"},
    )
    assert resp.status_code == 422


async def test_track_properties_non_object_returns_422(api_client):
    """properties must be a JSON object, not an array or string."""
    data = await _create_project(api_client, name="bad-props.com")
    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "click",
            "session_id": "s1",
            "properties": ["not", "an", "object"],
        },
    )
    assert resp.status_code == 422


async def test_track_properties_optional_defaults_empty(api_client, db_session):
    from sqlalchemy import select

    from app.models.event import Event

    data = await _create_project(api_client, name="no-props.com")
    sid = str(uuid.uuid4())
    await api_client.post(
        "/api/v1/track",
        json={"api_key": data["api_key"], "event_name": "ping", "session_id": sid},
    )
    result = await db_session.execute(select(Event).where(Event.session_id == sid))
    event = result.scalar_one()
    assert event.properties == {}


async def test_track_domain_allowlist_rejects_wrong_origin(api_client):
    data = await _create_project(api_client, name="strict.com", allowlist=["myapp.com"])
    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "click",
            "session_id": "s1",
        },
        headers={"Origin": "https://other.com"},
    )
    assert resp.status_code == 403


async def test_track_domain_allowlist_accepts_matching_origin(api_client):
    data = await _create_project(api_client, name="allowed.com", allowlist=["myapp.com"])
    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "click",
            "session_id": "s1",
        },
        headers={"Origin": "https://myapp.com"},
    )
    assert resp.status_code == 202


async def test_track_empty_allowlist_accepts_no_origin(api_client):
    """Empty allowlist = open; requests without Origin are fine."""
    data = await _create_project(api_client, name="open.com", allowlist=[])
    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": data["api_key"],
            "event_name": "view",
            "session_id": "s1",
        },
    )
    assert resp.status_code == 202


async def test_pageview_endpoint_returns_202(api_client):
    data = await _create_project(api_client, name="pageview-test.com")
    resp = await api_client.post(
        "/api/v1/pageview",
        json={
            "api_key": data["api_key"],
            "session_id": str(uuid.uuid4()),
            "url": "https://myapp.com/pricing",
        },
    )
    assert resp.status_code == 202


async def test_pageview_stores_event_name_as_pageview(api_client, db_session):
    from sqlalchemy import select

    from app.models.event import Event

    data = await _create_project(api_client, name="pv-name.com")
    sid = str(uuid.uuid4())
    await api_client.post(
        "/api/v1/pageview",
        json={
            "api_key": data["api_key"],
            "session_id": sid,
            "url": "https://myapp.com/about",
            "referrer": "https://google.com",
        },
    )
    result = await db_session.execute(select(Event).where(Event.session_id == sid))
    event = result.scalar_one()
    assert event.event_name == "pageview"
    assert event.url == "https://myapp.com/about"
    assert event.referrer == "https://google.com"


async def test_pageview_missing_url_returns_422(api_client):
    data = await _create_project(api_client, name="pv-no-url.com")
    resp = await api_client.post(
        "/api/v1/pageview",
        json={"api_key": data["api_key"], "session_id": "s1"},
    )
    assert resp.status_code == 422


async def test_alert_every_condition_fires(api_client, db_session):
    """Alert with condition=every should fire on every event insertion."""

    from app.models.alert import Alert, AlertCondition
    from app.services.events import evaluate_alerts

    data = await _create_project(api_client, name="alert-every.com")
    project_id = uuid.UUID(data["id"])

    # Create an alert manually in the DB
    alert = Alert(
        project_id=project_id,
        event_name="purchase",
        condition=AlertCondition.every,
    )
    db_session.add(alert)
    await db_session.flush()

    fired = await evaluate_alerts(db_session, project_id=project_id, event_name="purchase")
    assert len(fired) == 1
    assert fired[0].id == alert.id


async def test_alert_every_n_fires_on_nth_event(api_client, db_session):
    from app.models.alert import Alert, AlertCondition
    from app.services.events import evaluate_alerts

    data = await _create_project(api_client, name="alert-n.com")
    project_id = uuid.UUID(data["id"])

    alert = Alert(
        project_id=project_id,
        event_name="click",
        condition=AlertCondition.every_n,
        threshold_n=3,
        counter=0,
    )
    db_session.add(alert)
    await db_session.flush()

    # Should NOT fire on events 1 and 2
    for _ in range(2):
        result = await evaluate_alerts(db_session, project_id=project_id, event_name="click")
        assert result == []

    # Should fire on event 3 and reset counter
    fired = await evaluate_alerts(db_session, project_id=project_id, event_name="click")
    assert len(fired) == 1
    assert fired[0].counter == 0  # reset after firing
