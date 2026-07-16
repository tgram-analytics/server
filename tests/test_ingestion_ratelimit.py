"""Two-tier per-IP throttle on the ingestion path.

Tier 1 (coarse valve) caps ALL traffic per IP at a high ceiling; tier 2
(invalid-key penalty) is a strict per-IP budget that counts only failed key
lookups and, once exhausted, rejects further requests BEFORE the DB lookup.

Uses the DB-backed ``api_client`` fixture rather than the DB-free ``client``:
the ``track`` endpoint declares ``session: AsyncSession = Depends(get_session)``,
and ``get_session`` raises ``RuntimeError`` when ``init_db()`` has not run — so
under the DB-free ``client`` a request 500s during dependency resolution,
before ``_resolve_project`` (where the throttle lives) is reached. ``api_client``
initialises the test DB so requests flow through it.

ASGITransport reports every request's client IP as 127.0.0.1 — the "one IP"
scenario. The autouse ``_isolate_ingestion_ratelimiter`` fixture in conftest
clears the limiter windows around each test, so no explicit reset is needed
here beyond the monkeypatched knobs.
"""

import uuid


async def _create_project(api_client, name: str) -> dict:
    resp = await api_client.post(
        "/api/v1/internal/projects",
        json={"name": name, "admin_chat_id": 111},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _invalid_payload() -> dict:
    return {
        "api_key": "proj_invalid",
        "event_name": "e",
        "session_id": "s",
        "properties": {},
    }


async def test_invalid_key_flood_is_rate_limited(api_client, monkeypatch):
    """Flooding invalid keys from one IP eventually 429s instead of endless 400s."""
    import app.api.ingestion as ing

    monkeypatch.setattr(ing, "_invalid_key_rate_limit", 3, raising=False)

    statuses = []
    for _ in range(10):
        resp = await api_client.post("/api/v1/track", json=_invalid_payload())
        statuses.append(resp.status_code)

    assert 429 in statuses, f"invalid-key flood never rate-limited: {statuses}"


async def test_invalid_key_flood_stops_hitting_db(api_client, monkeypatch):
    """Once the invalid-key budget is spent, further bad-key requests are 429'd
    (blocked before the DB lookup), not 400'd."""
    import app.api.ingestion as ing

    monkeypatch.setattr(ing, "_invalid_key_rate_limit", 2, raising=False)

    statuses = [
        (await api_client.post("/api/v1/track", json=_invalid_payload())).status_code
        for _ in range(6)
    ]

    # First requests are charged as bad-key attempts and return 400; once the
    # budget (2) is over, the pre-lookup check short-circuits to 429 and sticks.
    assert statuses[0] == 400, statuses
    assert statuses[-1] == 429 and statuses[-2] == 429, statuses


async def test_valid_key_not_throttled_by_invalid_key_limit(api_client, monkeypatch):
    """Regression guard: valid authenticated traffic must NOT be capped by the
    (low) invalid-key budget — a server-side SDK egressing from one IP keeps
    working past the bad-key limit."""
    import app.api.ingestion as ing

    # Invalid-key budget of 2 is far below the number of valid requests below.
    monkeypatch.setattr(ing, "_invalid_key_rate_limit", 2, raising=False)

    data = await _create_project(api_client, name=f"ratelimit-valid-{uuid.uuid4().hex[:8]}.com")
    api_key = data["api_key"]

    statuses = []
    for _ in range(10):
        resp = await api_client.post(
            "/api/v1/track",
            json={
                "api_key": api_key,
                "event_name": "e",
                "session_id": str(uuid.uuid4()),
                "properties": {},
            },
        )
        statuses.append(resp.status_code)

    assert all(s == 202 for s in statuses), f"valid key throttled by bad-key budget: {statuses}"
    assert 429 not in statuses, statuses


async def test_single_invalid_key_still_400(api_client, monkeypatch):
    """A single invalid-key request under the budget still returns 400 — proving
    the fix throttles floods without turning every rejection into 429."""
    import app.api.ingestion as ing

    monkeypatch.setattr(ing, "_invalid_key_rate_limit", 10, raising=False)

    resp = await api_client.post("/api/v1/track", json=_invalid_payload())
    assert resp.status_code == 400, resp.text
