"""Request-body size limit rejects oversized payloads before parsing."""


async def test_oversized_body_rejected_413(client):
    big = "a" * (2 * 1024 * 1024)  # 2 MB
    resp = await client.post(
        "/api/v1/track",
        content=big,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413


async def test_normal_body_not_rejected(client):
    # A normal-sized body passes the size middleware and reaches request
    # parsing. The DB-free ``client`` fixture skips init_db (the track handler's
    # DB session dependency runs for any *parseable* JSON body), so we send a
    # small body that fails JSON parsing: FastAPI returns 422 before any DB
    # dependency runs. The point of this test is only that the size middleware
    # does NOT reject a small body with 413.
    small = b'{"not": "closed"'  # small + malformed JSON -> 422, never 413
    resp = await client.post(
        "/api/v1/track",
        content=small,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code != 413
    assert resp.status_code == 422
