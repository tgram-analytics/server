"""Webhook authenticates via the X-Telegram-Bot-Api-Secret-Token header.

The bot token used to live in the URL path (/webhook/{token}), which leaked
it verbatim into access logs. It is now replaced by a random shared secret
that Telegram echoes back in a header, keeping the token out of the request
path entirely.
"""

from unittest.mock import AsyncMock, MagicMock

# Must match WEBHOOK_SECRET set by the ``client`` fixture in conftest.
TEST_SECRET = "test-webhook-secret"


async def test_webhook_rejects_missing_secret_header(client):
    resp = await client.post("/webhook", json={"update_id": 1})
    assert resp.status_code == 403


async def test_webhook_rejects_wrong_secret_header(client):
    resp = await client.post(
        "/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert resp.status_code == 403


async def test_webhook_rejects_non_ascii_secret_header(client):
    # A raw byte >127 on the wire: Starlette latin-1-decodes it into a
    # non-ASCII str, and hmac.compare_digest(str, str) would raise TypeError
    # on that path. The handler must still return a clean 403, not a 500.
    # The value is passed as bytes so httpx does not ASCII-reject it locally.
    resp = await client.post(
        "/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": b"wrong\xff"},
    )
    assert resp.status_code == 403


async def test_webhook_old_token_in_url_path_is_gone(client):
    # The bot token must no longer be a valid URL path segment.
    resp = await client.post(
        "/webhook/1234567890:test-token-for-testing-only", json={"update_id": 1}
    )
    assert resp.status_code == 404


async def test_webhook_correct_secret_dispatches_update(client, monkeypatch):
    """A POST carrying the correct secret header reaches the handler.

    ``get_application`` would raise if the bot were uninitialised, so we
    monkeypatch it to a mock whose ``process_update`` is awaitable and assert
    the handler returns 200 past the auth check.
    """
    mock_app = MagicMock()
    mock_app.bot = MagicMock()
    mock_app.process_update = AsyncMock()

    monkeypatch.setattr("app.api.webhook.get_application", lambda: mock_app)
    monkeypatch.setattr(
        "app.api.webhook.Update",
        MagicMock(de_json=MagicMock(return_value=MagicMock())),
    )

    resp = await client.post(
        "/webhook",
        json={"update_id": 42},
        headers={"X-Telegram-Bot-Api-Secret-Token": TEST_SECRET},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mock_app.process_update.assert_called_once()
