"""Full stack: DCR -> authorize (paste token) -> /token -> MCP tools/list."""

import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.mcp.oauth.pkce import s256_challenge
from tests.mcp.conftest import _boot_server

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.mark.asyncio
async def test_oauth_issued_token_calls_tools(async_engine, session_factory):
    from sqlalchemy import text

    from app.models.user import User
    from app.services import mcp_tokens as token_svc

    db_url = async_engine.url.render_as_string(hide_password=False)
    async with session_factory() as session:
        user = User(telegram_user_id=930_777)
        session.add(user)
        await session.flush()
        master, _ = await token_svc.create_token(session, user_id=user.id, label="master")
        await session.commit()
        uid = user.id

    try:
        async with _boot_server(mcp_enabled=True, real_db=True, database_url=db_url) as base:
            async with httpx.AsyncClient(base_url=base, timeout=15.0) as web:
                cid = (
                    await web.post(
                        "/mcp/oauth/register",
                        json={"client_name": "e2e", "redirect_uris": [REDIRECT]},
                    )
                ).json()["client_id"]
                challenge = s256_challenge("e2e-verifier")
                page = await web.get(
                    "/mcp/oauth/authorize",
                    params={
                        "response_type": "code",
                        "client_id": cid,
                        "redirect_uri": REDIRECT,
                        "state": "s",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    },
                )
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
                submit = await web.post(
                    "/mcp/oauth/authorize",
                    data={
                        "token": master,
                        "csrf_token": csrf,
                        "client_id": cid,
                        "redirect_uri": REDIRECT,
                        "state": "s",
                        "code_challenge": challenge,
                    },
                    follow_redirects=False,
                )
                code = parse_qs(urlparse(submit.headers["location"]).query)["code"][0]
                token_resp = await web.post(
                    "/mcp/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "code_verifier": "e2e-verifier",
                        "client_id": cid,
                        "redirect_uri": REDIRECT,
                    },
                )
                access = token_resp.json()["access_token"]

            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            async with (
                streamablehttp_client(
                    url=base.rstrip("/") + "/mcp/",
                    headers={"Authorization": f"Bearer {access}"},
                ) as (read, write, _),
                ClientSession(read, write) as mcp_session,
            ):
                await mcp_session.initialize()
                tools = await mcp_session.list_tools()
                assert "whoami" in {t.name for t in tools.tools}
                result = await mcp_session.call_tool("whoami", {})
                assert not result.isError
    finally:
        async with session_factory() as session:
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(uid)})
            await session.execute(text("DELETE FROM mcp_selfhost_oauth_clients"))
            await session.commit()
