"""Invalid-key traffic is throttled by IP before the DB key lookup runs.

Uses the DB-backed ``api_client`` fixture rather than the DB-free ``client``:
the ``track`` endpoint declares ``session: AsyncSession = Depends(get_session)``,
and ``get_session`` raises ``RuntimeError`` when ``init_db()`` has not run — so
under the DB-free ``client`` an invalid-key request 500s during dependency
resolution, before the endpoint body (and thus the per-IP limiter) is ever
reached. ``api_client`` initialises the test DB so the request flows through
``_resolve_project`` where the pre-auth IP throttle lives.

ASGITransport reports every request's client IP as 127.0.0.1, which is exactly
the "one IP flooding" scenario we want.
"""


def _reset_limiter() -> None:
    """Clear module-level limiter state so tests are deterministic and don't
    pollute one another (the deques persist for the life of the process)."""
    import app.api.ingestion as ing

    ing._rate_windows.clear()
    ing._rate_last_access.clear()


async def test_invalid_key_flood_is_rate_limited(api_client, monkeypatch):
    import app.api.ingestion as ing

    _reset_limiter()
    monkeypatch.setattr(ing, "_anon_rate_limit", 3, raising=False)

    statuses = []
    for _ in range(10):
        resp = await api_client.post(
            "/api/v1/track",
            json={
                "api_key": "proj_invalid",
                "event_name": "e",
                "session_id": "s",
                "properties": {},
            },
        )
        statuses.append(resp.status_code)

    assert 429 in statuses, f"invalid-key flood never rate-limited: {statuses}"


async def test_single_invalid_key_still_400(api_client, monkeypatch):
    """A single invalid-key request under the anon cap still returns 400 —
    proving the fix throttles floods without turning every rejection into 429."""
    import app.api.ingestion as ing

    _reset_limiter()
    monkeypatch.setattr(ing, "_anon_rate_limit", 20, raising=False)

    resp = await api_client.post(
        "/api/v1/track",
        json={
            "api_key": "proj_invalid",
            "event_name": "e",
            "session_id": "s",
            "properties": {},
        },
    )
    assert resp.status_code == 400, resp.text
