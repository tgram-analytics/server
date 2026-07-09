"""FastAPI router for the self-host paste-token OAuth flow.

Endpoints (mounted at ``/mcp/oauth`` by ``app.main``):

- ``POST /register`` — Dynamic Client Registration (RFC 7591), public
  clients only, rate-limited.
- ``GET /authorize`` — validates OAuth params, renders the paste-token
  page (client identity + CSRF).
- ``POST /authorize`` — CSRF check, master-token check via the existing
  ``mcp_tokens`` hash lookup, mints a 60s single-use code, 302 back.
- ``POST /token`` — authorization_code grant; PKCE + single-use checks
  in the service; returns a derived ``mcp_tokens`` access token.

Error philosophy: parameter errors that predate user interaction are
plain 400s (the client is broken/malicious, not the human); a wrong
pasted token re-renders the page with a generic message; every /token
failure is a uniform ``invalid_grant``.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.oauth import service as svc
from app.mcp.oauth.csrf import issue_csrf, verify_csrf
from app.mcp.oauth.notify import notify_token_issued
from app.mcp.oauth.pages import render_authorize_page
from app.mcp.oauth.rate_limit import RateLimiter

_register_limiter = RateLimiter(limit=20, window_seconds=60)
_authorize_limiter = RateLimiter(limit=10, window_seconds=60)


class DCRRegisterRequest(BaseModel):
    client_name: str = Field(default="", max_length=200)
    redirect_uris: list[str] = Field(min_length=1, max_length=10)


class DCRRegisterResponse(BaseModel):
    client_id: str
    client_name: str
    redirect_uris: list[str]
    token_endpoint_auth_method: str = "none"


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _open_session() -> AsyncSession:
    from app.core.database import get_session_factory

    return get_session_factory()()


def _redirect_with(redirect_uri: str, params: dict[str, str]) -> str:
    """Append OAuth params to *redirect_uri*, merging into any existing query.

    A registered redirect_uri may already carry a query (RFC 6749 §3.1.2
    permits it), so a naive ``f"{uri}?{...}"`` would emit a second ``?`` and
    break the callback. ``parse_qsl``/``urlencode`` preserve the original
    pairs and append ours.
    """
    parts = urlparse(redirect_uri)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q.update(params)
    return urlunparse(parts._replace(query=urlencode(q)))


def build_oauth_router() -> APIRouter:
    router = APIRouter(tags=["mcp-oauth"])

    @router.post("/register", response_model=DCRRegisterResponse, status_code=201)
    async def register(request: Request, body: DCRRegisterRequest):  # type: ignore[no-untyped-def]
        if not _register_limiter.allow(_client_ip(request)):
            return JSONResponse(status_code=429, content={"error": "rate_limited"})
        async with _open_session() as session:
            client = await svc.register_client(
                session, client_name=body.client_name, redirect_uris=body.redirect_uris
            )
            await session.commit()
            return DCRRegisterResponse(
                client_id=client.client_id,
                client_name=client.client_name,
                redirect_uris=client.redirect_uris,
            )

    @router.get("/authorize", response_class=HTMLResponse)
    async def authorize_page(  # type: ignore[no-untyped-def]
        response_type: str = Query(""),
        client_id: str = Query(""),
        redirect_uri: str = Query(""),
        state: str = Query(""),
        code_challenge: str = Query(""),
        code_challenge_method: str = Query(""),
    ):
        if response_type != "code" or not code_challenge or code_challenge_method != "S256":
            return HTMLResponse("invalid authorization request", status_code=400)
        async with _open_session() as session:
            client = await svc.get_client(session, client_id)
        if client is None or redirect_uri not in client.redirect_uris:
            return HTMLResponse("unknown client or redirect_uri", status_code=400)

        from app.core.config import get_settings

        csrf = issue_csrf(secret=get_settings().secret_key, client_id=client_id)
        return HTMLResponse(
            render_authorize_page(
                client_name=client.client_name or client.client_id,
                redirect_uri=redirect_uri,
                client_id=client_id,
                state=state,
                code_challenge=code_challenge,
                csrf_token=csrf,
            )
        )

    @router.post("/authorize")
    async def authorize_submit(  # type: ignore[no-untyped-def]
        request: Request,
        token: str = Form(""),
        csrf_token: str = Form(""),
        client_id: str = Form(""),
        redirect_uri: str = Form(""),
        state: str = Form(""),
        code_challenge: str = Form(""),
    ):
        if not _authorize_limiter.allow(_client_ip(request)):
            return HTMLResponse("rate limited — try again in a minute", status_code=429)

        from app.core.config import get_settings

        settings = get_settings()
        if not verify_csrf(csrf_token, secret=settings.secret_key, client_id=client_id):
            return HTMLResponse("invalid or expired form — reload the page", status_code=400)

        async with _open_session() as session:
            client = await svc.get_client(session, client_id)
            if client is None or redirect_uri not in client.redirect_uris:
                return HTMLResponse("unknown client or redirect_uri", status_code=400)

            from app.services.mcp_tokens import lookup_active_token

            row = await lookup_active_token(session, token)
            if row is None:
                fresh = issue_csrf(secret=settings.secret_key, client_id=client_id)
                return HTMLResponse(
                    render_authorize_page(
                        client_name=client.client_name or client.client_id,
                        redirect_uri=redirect_uri,
                        client_id=client_id,
                        state=state,
                        code_challenge=code_challenge,
                        csrf_token=fresh,
                        error="invalid_token",
                    )
                )

            code = await svc.mint_code(
                session,
                user_id=row.user_id,
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
            )
            await session.commit()

        return RedirectResponse(
            url=_redirect_with(redirect_uri, {"code": code, "state": state}),
            status_code=302,
        )

    @router.post("/token")
    async def token_exchange(  # type: ignore[no-untyped-def]
        grant_type: str = Form(""),
        code: str = Form(""),
        code_verifier: str = Form(""),
        client_id: str = Form(""),
        redirect_uri: str = Form(""),
    ):
        if grant_type != "authorization_code":
            return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})
        async with _open_session() as session:
            raw = await svc.exchange_code(
                session,
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
            )
            if raw is None:
                await session.rollback()
                return JSONResponse(status_code=400, content={"error": "invalid_grant"})
            from app.services.mcp_tokens import lookup_active_token

            issued = await lookup_active_token(session, raw)
            client = await svc.get_client(session, client_id)
            await session.commit()

        from app.core.config import get_settings

        await notify_token_issued(
            admin_chat_id=get_settings().admin_chat_id,
            client_name=(client.client_name if client else client_id) or client_id,
            token_id=str(issued.id) if issued else "",
        )
        # RFC 6749 §5.1: token responses must not be cached.
        return JSONResponse(
            content={
                "access_token": raw,
                "token_type": "Bearer",
                "scope": "mcp:tools",
            },
            headers={"Cache-Control": "no-store"},
        )

    return router
