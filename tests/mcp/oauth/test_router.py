"""HTTP surface: DCR, authorize page, code grant, token exchange."""

import uuid
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import app.mcp.oauth.router as router_module
from app.mcp.oauth.pkce import s256_challenge
from app.mcp.oauth.rate_limit import RateLimiter
from app.mcp.oauth.router import build_oauth_router
from app.models.user import User
from app.services import mcp_tokens as token_svc

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture(autouse=True)
def _fresh_limiters(monkeypatch):
    """Re-instantiate the module-level limiters so state never leaks between
    tests (the DCR-rate-limit test deliberately fills the register bucket).
    Same limits as production — reset state, never raise limits."""
    for name in ("_register_limiter", "_authorize_limiter"):
        old = getattr(router_module, name)
        monkeypatch.setattr(
            router_module, name, RateLimiter(limit=old._limit, window_seconds=old._window)
        )


@pytest_asyncio.fixture
async def oauth_client(session_factory, monkeypatch):
    """ASGI client for the oauth router, wired to the Postgres test DB."""
    monkeypatch.setattr("app.core.database.get_session_factory", lambda: session_factory)
    app = FastAPI()
    app.include_router(build_oauth_router(), prefix="/mcp/oauth")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def seeded(session_factory):
    """A committed user + raw master token; cleaned up after."""
    from sqlalchemy import text

    async with session_factory() as session:
        user = User(telegram_user_id=920_000 + uuid.uuid4().int % 10_000)
        session.add(user)
        await session.flush()
        raw, _ = await token_svc.create_token(session, user_id=user.id, label="master")
        await session.commit()
        uid = user.id
    yield uid, raw
    async with session_factory() as session:
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(uid)})
        await session.execute(text("DELETE FROM mcp_selfhost_oauth_clients"))
        await session.commit()


async def _register(client) -> str:
    r = await client.post(
        "/mcp/oauth/register",
        json={"client_name": "Claude", "redirect_uris": [REDIRECT]},
    )
    assert r.status_code == 201
    return r.json()["client_id"]


def _authorize_params(client_id: str, challenge: str) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "state": "xyz",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }


@pytest.mark.asyncio
async def test_dcr_and_authorize_page(oauth_client):
    cid = await _register(oauth_client)
    r = await oauth_client.get(
        "/mcp/oauth/authorize", params=_authorize_params(cid, s256_challenge("v"))
    )
    assert r.status_code == 200
    assert "Claude" in r.text and "claude.ai" in r.text
    assert 'name="csrf_token"' in r.text


@pytest.mark.asyncio
async def test_authorize_rejects_unknown_client_and_bad_pkce(oauth_client):
    r = await oauth_client.get(
        "/mcp/oauth/authorize", params=_authorize_params("nope", s256_challenge("v"))
    )
    assert r.status_code == 400
    cid = await _register(oauth_client)
    params = _authorize_params(cid, s256_challenge("v"))
    params["code_challenge_method"] = "plain"
    assert (await oauth_client.get("/mcp/oauth/authorize", params=params)).status_code == 400
    params = _authorize_params(cid, s256_challenge("v"))
    params["redirect_uri"] = "https://evil.example/cb"
    assert (await oauth_client.get("/mcp/oauth/authorize", params=params)).status_code == 400


async def _post_authorize(client, cid: str, token: str, challenge: str, csrf: str):
    return await client.post(
        "/mcp/oauth/authorize",
        data={
            "token": token,
            "csrf_token": csrf,
            "client_id": cid,
            "redirect_uri": REDIRECT,
            "state": "xyz",
            "code_challenge": challenge,
        },
    )


def _extract_csrf(html: str) -> str:
    import re

    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf token not in page"
    return m.group(1)


@pytest.mark.asyncio
async def test_full_flow_issues_working_derived_token(oauth_client, seeded, session_factory):
    user_id, master = seeded
    cid = await _register(oauth_client)
    challenge = s256_challenge("verifier-1")
    page = await oauth_client.get("/mcp/oauth/authorize", params=_authorize_params(cid, challenge))
    csrf = _extract_csrf(page.text)

    with patch("app.mcp.oauth.router.notify_token_issued", new=AsyncMock()) as notify:
        r = await _post_authorize(oauth_client, cid, master, challenge, csrf)
        assert r.status_code == 302
        loc = urlparse(r.headers["location"])
        assert loc.netloc == "claude.ai"
        q = parse_qs(loc.query)
        assert q["state"] == ["xyz"]
        code = q["code"][0]

        r2 = await oauth_client.post(
            "/mcp/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": "verifier-1",
                "client_id": cid,
                "redirect_uri": REDIRECT,
            },
        )
    assert r2.status_code == 200
    body = r2.json()
    assert body["token_type"] == "Bearer" and body["access_token"].startswith("mcp_")
    notify.assert_awaited_once()

    async with session_factory() as session:
        row = await token_svc.lookup_active_token(session, body["access_token"])
        assert row is not None and row.user_id == user_id and row.label.startswith("oauth:")


@pytest.mark.asyncio
async def test_authorize_post_bad_token_no_code(oauth_client, seeded):
    _, _master = seeded
    cid = await _register(oauth_client)
    challenge = s256_challenge("v")
    page = await oauth_client.get("/mcp/oauth/authorize", params=_authorize_params(cid, challenge))
    csrf = _extract_csrf(page.text)
    r = await _post_authorize(oauth_client, cid, "mcp_" + "0" * 64, challenge, csrf)
    assert r.status_code == 200  # re-rendered page, no redirect
    assert "check the token" in r.text.lower()


@pytest.mark.asyncio
async def test_authorize_post_bad_csrf_rejected(oauth_client, seeded):
    _, master = seeded
    cid = await _register(oauth_client)
    challenge = s256_challenge("v")
    r = await _post_authorize(oauth_client, cid, master, challenge, "forged.csrf.token")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_token_rejects_wrong_verifier_and_replay(oauth_client, seeded):
    _, master = seeded
    cid = await _register(oauth_client)
    challenge = s256_challenge("good")
    page = await oauth_client.get("/mcp/oauth/authorize", params=_authorize_params(cid, challenge))
    csrf = _extract_csrf(page.text)
    with patch("app.mcp.oauth.router.notify_token_issued", new=AsyncMock()):
        r = await _post_authorize(oauth_client, cid, master, challenge, csrf)
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": "WRONG",
            "client_id": cid,
            "redirect_uri": REDIRECT,
        }
        assert (await oauth_client.post("/mcp/oauth/token", data=form)).status_code == 400
        form["code_verifier"] = "good"
        assert (await oauth_client.post("/mcp/oauth/token", data=form)).status_code == 200
        assert (await oauth_client.post("/mcp/oauth/token", data=form)).status_code == 400


@pytest.mark.asyncio
async def test_dcr_rate_limited(oauth_client):
    last = None
    for _ in range(25):
        last = await oauth_client.post(
            "/mcp/oauth/register",
            json={"client_name": "spam", "redirect_uris": [REDIRECT]},
        )
    assert last is not None and last.status_code == 429
