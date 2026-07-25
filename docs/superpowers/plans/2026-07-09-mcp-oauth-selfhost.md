# Self-Hosted MCP OAuth (Paste-Token) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** OAuth-only MCP clients (Claude Desktop) can connect to a self-hosted instance by pasting an existing `/mcp_token` token into a browser authorize page; standard OAuth 2.1 (DCR + PKCE S256) around it, access tokens are derived `mcp_tokens` rows verified by the existing `StaticTokenVerifier`.

**Architecture:** New OSS package `app/mcp/oauth/` (metadata, PKCE, stateless CSRF, DCR, authorize page, token exchange, in-process rate limit, Telegram issuance notification) + host-root well-known router, mounted from `app.main` ONLY on self-host (`mcp_enabled and mcp_oauth_enabled and get_mcp_token_verifier() is None`). No JWT: `/token` mints a new `mcp_tokens` row labeled `oauth:<client>`; existing verifier and `/mcp_token` list/revoke work unchanged. Plus: uvicorn proxy-headers fix (https redirects behind Cloudflare) and a favicon route.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Alembic (OSS head `0010` → new `0011`), python-telegram-bot, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-09-mcp-oauth-selfhost-design.md` (this repo, same branch). **Deviation locked in Task 1:** DB tables are `mcp_selfhost_oauth_clients` / `mcp_selfhost_oauth_codes` — the spec's names collide with the cloud overlay's existing `mcp_oauth_clients` / `mcp_authorization_codes` tables, and the OSS migration also runs on cloud deploys (`alembic upgrade heads`), which would crash them.

**Repo:** `/Users/leonardo/Progetti/telegram-analytics/server`, branch `feat/mcp-oauth-selfhost` (exists; holds the spec).

**Test command:** `cd /Users/leonardo/Progetti/telegram-analytics/server && DATABASE_URL=postgresql+asyncpg://tga:password@localhost/tganalytics_test python -m pytest <path> -q` (Postgres container `tga-test-pg` must be running). Lint gates: `python -m mypy app` (strict; ignore the pre-existing `app/services/aggregation.py:102` unused-ignore — local mypy 2.1 artifact, CI passes), `python -m ruff check app tests`, `python -m ruff format app tests`.

**Reference implementation:** the cloud repo's OAuth at `/Users/leonardo/Progetti/telegram-analytics/tgram-analytics-cloud-deploy/cloud/src/tgram_analytics_cloud/mcp/oauth/` (router.py DCR/token shapes, pkce.py, metadata.py) and `well_known.py`. COPY shapes from there; do not invent OAuth semantics. We deliberately drop: Telegram widget, JWT, refresh tokens, state table, revocation table.

---

### Task 0: Rebase branch onto latest main

`feat/mcp-oauth-selfhost` was cut before PR #23 merged; Task 10 edits `app/bot/handlers/mcp.py` which only exists on current main.

- [ ] **Step 0.1:**

```bash
cd /Users/leonardo/Progetti/telegram-analytics/server
git fetch origin main && git rebase origin/main feat/mcp-oauth-selfhost
```
Expected: clean rebase (the branch has only two docs commits). Verify: `ls app/bot/handlers/mcp.py` exists and `git log --oneline -4` shows the two spec commits atop `cb40662`.

---

### Task 1: Settings + spec table-name amendment

**Files:**
- Modify: `app/core/config.py` (after the MCP block added for `mcp_github_token`)
- Modify: `docs/superpowers/specs/2026-07-09-mcp-oauth-selfhost-design.md` §5
- Test: `tests/mcp/oauth/test_settings.py`

- [ ] **Step 1.1: Write failing test**

Create `tests/mcp/oauth/__init__.py` (empty) and `tests/mcp/oauth/test_settings.py`:

```python
"""mcp_oauth_enabled settings field."""

from app.core.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="123:abc",
        admin_chat_id=1,
        database_url="sqlite+aiosqlite://",
        secret_key="x" * 32,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_mcp_oauth_enabled_defaults_true():
    assert _settings().mcp_oauth_enabled is True


def test_mcp_oauth_enabled_env_off():
    assert _settings(mcp_oauth_enabled=False).mcp_oauth_enabled is False
```

- [ ] **Step 1.2:** Run `python -m pytest tests/mcp/oauth/test_settings.py -q` → FAIL (`AttributeError: mcp_oauth_enabled`).

- [ ] **Step 1.3: Implement.** In `app/core/config.py`, inside the `# ── MCP server ──` block after `mcp_github_token`:

```python
    # Browser OAuth for MCP clients that cannot send custom headers
    # (Claude Desktop). Self-host only: mounted when the default
    # StaticTokenVerifier is in use; a plugin-registered verifier
    # (cloud overlay) supplies its own OAuth and this flag is inert.
    mcp_oauth_enabled: bool = True
```

- [ ] **Step 1.4:** Run the test → PASS.

- [ ] **Step 1.5: Amend the spec** (§5 Data model): replace the two table names with `mcp_selfhost_oauth_clients` and `mcp_selfhost_oauth_codes`, adding: *"Renamed from the draft's `mcp_oauth_clients`/`mcp_oauth_authorization_codes`: the cloud overlay already owns tables by those names and the OSS migration chain also runs on cloud deploys (`alembic upgrade heads`), so identical names would break cloud."* Also update §4/§6 references if they name the tables.

- [ ] **Step 1.6: Commit**

```bash
git add app/core/config.py tests/mcp/oauth/ docs/superpowers/specs/2026-07-09-mcp-oauth-selfhost-design.md
git commit -m "feat(mcp-oauth): add mcp_oauth_enabled setting; rename oauth tables in spec to avoid cloud collision"
```

---

### Task 2: ORM models + migration 0011

**Files:**
- Create: `app/mcp/oauth/__init__.py`, `app/models/mcp_oauth.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/0011_mcp_selfhost_oauth.py`

- [ ] **Step 2.1:** Create `app/mcp/oauth/__init__.py`:

```python
"""Self-host OAuth for MCP clients that cannot send custom headers.

Claude Desktop's connector UI is OAuth-only. This package fronts the
existing static-token auth with a standard OAuth 2.1 surface: DCR
(RFC 7591) + PKCE S256 (RFC 7636) + a browser authorize page where the
admin pastes a token minted via the /mcp_token bot command. The /token
endpoint exchanges the auth code for a *derived* ``mcp_tokens`` row, so
the existing ``StaticTokenVerifier`` validates every MCP call — there is
no JWT and no second verification path.

Mounted by ``app.main`` only when the default verifier is in use (the
cloud overlay registers its own verifier + OAuth and must not conflict).
"""
```

- [ ] **Step 2.2:** Create `app/models/mcp_oauth.py`:

```python
"""ORM models for the self-host MCP OAuth layer.

Table names carry the ``mcp_selfhost_`` prefix because the cloud overlay
already owns ``mcp_oauth_clients`` / ``mcp_authorization_codes`` and the
OSS migration chain also runs on cloud deploys (``alembic upgrade heads``).
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MCPSelfhostOAuthClient(Base):
    """A dynamically-registered OAuth client (RFC 7591). Public client, no secret."""

    __tablename__ = "mcp_selfhost_oauth_clients"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    client_id: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    client_name: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


class MCPSelfhostOAuthCode(Base):
    """Single-use PKCE-bound authorization code (60s TTL)."""

    __tablename__ = "mcp_selfhost_oauth_codes"

    code: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_id: Mapped[str] = mapped_column(
        sa.Text,
        sa.ForeignKey("mcp_selfhost_oauth_clients.client_id", ondelete="CASCADE"),
        nullable=False,
    )
    redirect_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    code_challenge: Mapped[str] = mapped_column(sa.Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
```

Register both names in `app/models/__init__.py` (import + `__all__`, same pattern as `MCPToken`).

- [ ] **Step 2.3:** Create `alembic/versions/0011_mcp_selfhost_oauth.py` (match 0010's header style):

```python
"""Create mcp_selfhost_oauth_clients and mcp_selfhost_oauth_codes.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-09 00:00:00.000000

Names are mcp_selfhost_* because the cloud overlay owns mcp_oauth_* and
this chain also runs on cloud deploys (upgrade heads).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_selfhost_oauth_clients",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", sa.Text, nullable=False, unique=True),
        sa.Column("client_name", sa.Text, nullable=False, server_default=""),
        sa.Column("redirect_uris", ARRAY(sa.Text), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_table(
        "mcp_selfhost_oauth_codes",
        sa.Column("code", sa.Text, primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "client_id",
            sa.Text,
            sa.ForeignKey("mcp_selfhost_oauth_clients.client_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("redirect_uri", sa.Text, nullable=False),
        sa.Column("code_challenge", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("mcp_selfhost_oauth_codes")
    op.drop_table("mcp_selfhost_oauth_clients")
```

- [ ] **Step 2.4:** Verify + full suite (conftest builds tables from metadata; `test_migrations_apply_cleanly` exercises the chain):

```bash
python -c "from app.models import MCPSelfhostOAuthClient, MCPSelfhostOAuthCode; print('ok')"
DATABASE_URL=postgresql+asyncpg://tga:password@localhost/tganalytics_test python -m pytest tests/ -x -q
```
Expected: `ok`, suite green.

- [ ] **Step 2.5: Commit** — `git add app/mcp/oauth/__init__.py app/models/ alembic/versions/0011_mcp_selfhost_oauth.py && git commit -m "feat(mcp-oauth): selfhost oauth client + code tables (migration 0011)"`

---

### Task 3: PKCE + stateless CSRF helpers

**Files:**
- Create: `app/mcp/oauth/pkce.py` (verbatim copy of cloud `oauth/pkce.py` — 21 lines, shown in reference repo)
- Create: `app/mcp/oauth/csrf.py`
- Test: `tests/mcp/oauth/test_pkce_csrf.py`

- [ ] **Step 3.1: Write failing tests** — `tests/mcp/oauth/test_pkce_csrf.py`:

```python
"""PKCE S256 + stateless CSRF token helpers."""

import time

from app.mcp.oauth.csrf import issue_csrf, verify_csrf
from app.mcp.oauth.pkce import s256_challenge, verify_s256

SECRET = "s" * 32


def test_pkce_roundtrip():
    challenge = s256_challenge("some-verifier-string")
    assert verify_s256("some-verifier-string", challenge)
    assert not verify_s256("wrong-verifier", challenge)


def test_csrf_roundtrip_bound_to_client():
    tok = issue_csrf(secret=SECRET, client_id="abc")
    assert verify_csrf(tok, secret=SECRET, client_id="abc")
    assert not verify_csrf(tok, secret=SECRET, client_id="OTHER")


def test_csrf_tamper_rejected():
    tok = issue_csrf(secret=SECRET, client_id="abc")
    assert not verify_csrf(tok + "x", secret=SECRET, client_id="abc")
    assert not verify_csrf("garbage", secret=SECRET, client_id="abc")


def test_csrf_expiry(monkeypatch):
    tok = issue_csrf(secret=SECRET, client_id="abc", ttl_seconds=1)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 5)
    assert not verify_csrf(tok, secret=SECRET, client_id="abc")
```

- [ ] **Step 3.2:** Run → FAIL (modules missing).

- [ ] **Step 3.3: Implement.** Copy `pkce.py` verbatim from the cloud reference. Create `app/mcp/oauth/csrf.py`:

```python
"""Stateless CSRF token for the authorize form.

``nonce.expiry.sig`` where ``sig = HMAC-SHA256(secret, nonce|expiry|client_id)``.
Stateless (no DB row, no session): the GET renders the token into a hidden
form field; the POST must return it. Binding client_id into the MAC means a
token minted for one client's page cannot authorize another client. Signed
with ``settings.secret_key`` (already required, 32+ chars).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

_DEFAULT_TTL = 15 * 60  # the admin may take a while to find their token


def _sign(secret: str, nonce: str, expiry: int, client_id: str) -> str:
    msg = f"{nonce}|{expiry}|{client_id}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def issue_csrf(*, secret: str, client_id: str, ttl_seconds: int = _DEFAULT_TTL) -> str:
    nonce = secrets.token_urlsafe(16)
    expiry = int(time.time()) + ttl_seconds
    return f"{nonce}.{expiry}.{_sign(secret, nonce, expiry, client_id)}"


def verify_csrf(token: str, *, secret: str, client_id: str) -> bool:
    try:
        nonce, expiry_s, sig = token.split(".", 2)
        expiry = int(expiry_s)
    except ValueError:
        return False
    if time.time() > expiry:
        return False
    return hmac.compare_digest(sig, _sign(secret, nonce, expiry, client_id))
```

- [ ] **Step 3.4:** Run → 4 PASS.
- [ ] **Step 3.5: Commit** — `git add app/mcp/oauth/pkce.py app/mcp/oauth/csrf.py tests/mcp/oauth/test_pkce_csrf.py && git commit -m "feat(mcp-oauth): PKCE S256 + stateless CSRF helpers"`

---

### Task 4: Discovery metadata + host-root well-known router

**Files:**
- Create: `app/mcp/oauth/metadata.py`
- Create: `app/mcp/well_known.py`
- Test: `tests/mcp/oauth/test_metadata.py`

- [ ] **Step 4.1: Failing tests** — `tests/mcp/oauth/test_metadata.py`:

```python
"""RFC 8414 / RFC 9728 discovery documents."""

import httpx
import pytest
from fastapi import FastAPI

from app.mcp.oauth.metadata import build_authorization_server_metadata
from app.mcp.well_known import build_well_known_router

BASE = "https://tga.example.com"


def test_as_metadata_shape():
    doc = build_authorization_server_metadata(BASE)
    assert doc["issuer"] == BASE
    assert doc["authorization_endpoint"] == f"{BASE}/mcp/oauth/authorize"
    assert doc["token_endpoint"] == f"{BASE}/mcp/oauth/token"
    assert doc["registration_endpoint"] == f"{BASE}/mcp/oauth/register"
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["grant_types_supported"] == ["authorization_code"]  # no refresh in v1
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]


@pytest.mark.asyncio
async def test_well_known_routes():
    app = FastAPI()
    app.include_router(build_well_known_router(public_url=BASE))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        for path in (
            "/.well-known/oauth-protected-resource/mcp",
            "/.well-known/oauth-protected-resource",
        ):
            r = await client.get(path)
            assert r.status_code == 200
            body = r.json()
            assert body["resource"] == f"{BASE}/mcp"
            assert body["authorization_servers"] == [BASE]
            assert body["scopes_supported"] == ["mcp:tools"]
        r = await client.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        assert r.json()["issuer"] == BASE
```

- [ ] **Step 4.2:** Run → FAIL (modules missing).

- [ ] **Step 4.3: Implement.** `app/mcp/oauth/metadata.py` — adapt the cloud `oauth/metadata.py` builder with one change (`grant_types_supported: ["authorization_code"]`, no refresh):

```python
"""Authorization-server metadata document (RFC 8414).

Single pure builder shared by the host-root well-known router and the
OAuth router so the two copies can never drift. URLs derive from the
configured public URL, never the request scheme (behind Cloudflare the
request scheme can read http; see the proxy-headers fix in app.main).
"""

from __future__ import annotations


def build_authorization_server_metadata(public_url: str) -> dict[str, object]:
    base = public_url.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/mcp/oauth/authorize",
        "token_endpoint": f"{base}/mcp/oauth/token",
        "registration_endpoint": f"{base}/mcp/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }
```

`app/mcp/well_known.py` — adapt the cloud `well_known.py` (same three routes; single `public_url` kwarg; resource = `f"{base}/mcp"`; `resource_documentation` → `"https://github.com/tgram-analytics/server"`; docstring notes FastMCP advertises the host-root path in WWW-Authenticate, which is exactly the 404 that broke Claude Desktop discovery):

```python
"""Host-root OAuth discovery metadata for the self-host MCP OAuth layer.

FastMCP's 401 advertises ``resource_metadata=<host>/.well-known/
oauth-protected-resource/mcp`` — before this router existed that URL
404'd on self-host, which is precisely why OAuth-only clients (Claude
Desktop) failed to connect. Serves RFC 9728 protected-resource metadata
(canonical + bare paths) and mirrors the RFC 8414 authorization-server
document at host root (required when the issuer is a bare host).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.mcp.oauth.metadata import build_authorization_server_metadata


def build_well_known_router(*, public_url: str) -> APIRouter:
    base = public_url.rstrip("/")
    resource_payload: dict[str, object] = {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": ["mcp:tools"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://github.com/tgram-analytics/server",
    }
    as_metadata = build_authorization_server_metadata(base)
    router = APIRouter(tags=["mcp-well-known"])

    @router.get("/.well-known/oauth-protected-resource/mcp", response_model=None)
    async def protected_resource_with_path() -> dict[str, object]:
        return resource_payload

    @router.get("/.well-known/oauth-protected-resource", response_model=None)
    async def protected_resource_bare() -> dict[str, object]:
        return resource_payload

    @router.get("/.well-known/oauth-authorization-server", response_model=None)
    async def authorization_server() -> dict[str, object]:
        return as_metadata

    return router
```

- [ ] **Step 4.4:** Run → PASS.
- [ ] **Step 4.5: Commit** — `git add app/mcp/oauth/metadata.py app/mcp/well_known.py tests/mcp/oauth/test_metadata.py && git commit -m "feat(mcp-oauth): discovery metadata + host-root well-known router"`

---

### Task 5: In-process rate limiter

**Files:**
- Create: `app/mcp/oauth/rate_limit.py`
- Test: `tests/mcp/oauth/test_rate_limit.py`

- [ ] **Step 5.1: Failing tests:**

```python
"""Fixed-window in-process rate limiter."""

from app.mcp.oauth.rate_limit import RateLimiter


def test_allows_up_to_limit_then_blocks():
    rl = RateLimiter(limit=3, window_seconds=60)
    assert all(rl.allow("1.2.3.4", now=100.0) for _ in range(3))
    assert not rl.allow("1.2.3.4", now=100.0)


def test_window_resets():
    rl = RateLimiter(limit=1, window_seconds=60)
    assert rl.allow("k", now=100.0)
    assert not rl.allow("k", now=130.0)
    assert rl.allow("k", now=161.0)


def test_keys_independent():
    rl = RateLimiter(limit=1, window_seconds=60)
    assert rl.allow("a", now=100.0)
    assert rl.allow("b", now=100.0)
```

- [ ] **Step 5.2:** Run → FAIL.

- [ ] **Step 5.3: Implement** `app/mcp/oauth/rate_limit.py`:

```python
"""Fixed-window per-key rate limiter, in-process.

Good enough for the self-host OAuth surface: single-replica installs are
the OSS default (the same assumption the visitor-salt fallback makes).
Guards DCR spam and authorize-POST hammering; NOT a security control
against token guessing (tokens are 256-bit — guessing is hopeless).
"""

from __future__ import annotations

import time


class RateLimiter:
    def __init__(self, *, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._buckets: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        ts = time.time() if now is None else now
        start, count = self._buckets.get(key, (ts, 0))
        if ts - start >= self._window:
            start, count = ts, 0
        if count >= self._limit:
            self._buckets[key] = (start, count)
            return False
        self._buckets[key] = (start, count + 1)
        # Opportunistic purge so long-running processes don't accumulate keys.
        if len(self._buckets) > 10_000:
            cutoff = ts - self._window
            self._buckets = {k: v for k, v in self._buckets.items() if v[0] > cutoff}
        return True
```

- [ ] **Step 5.4:** Run → PASS. **Step 5.5: Commit** — `git commit -m "feat(mcp-oauth): in-process fixed-window rate limiter"` (add both files).

---

### Task 6: Service layer — register client, mint/exchange code

**Files:**
- Create: `app/mcp/oauth/service.py`
- Test: `tests/mcp/oauth/test_service.py`

- [ ] **Step 6.1: Failing tests** — `tests/mcp/oauth/test_service.py`. Uses the Postgres `session_factory` + a seeded user (same idiom as `tests/test_mcp_tokens_service.py` — read it first and reuse its user-creation helper):

```python
"""OAuth service: DCR, code mint/exchange, derived-token issuance."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.mcp.oauth import service as svc
from app.mcp.oauth.pkce import s256_challenge
from app.models.user import User

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


async def _user(session) -> User:
    u = User(telegram_user_id=910_000 + uuid.uuid4().int % 10_000)
    session.add(u)
    await session.flush()
    return u


@pytest.mark.asyncio
async def test_register_client_roundtrip(db_session):
    client = await svc.register_client(db_session, client_name="Claude", redirect_uris=[REDIRECT])
    assert client.client_id
    found = await svc.get_client(db_session, client.client_id)
    assert found is not None and found.redirect_uris == [REDIRECT]


@pytest.mark.asyncio
async def test_mint_and_exchange_code_issues_derived_token(db_session):
    user = await _user(db_session)
    client = await svc.register_client(db_session, client_name="C", redirect_uris=[REDIRECT])
    challenge = s256_challenge("verifier-123")
    code = await svc.mint_code(
        db_session,
        user_id=user.id,
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        code_challenge=challenge,
    )
    raw = await svc.exchange_code(
        db_session,
        code=code,
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        code_verifier="verifier-123",
    )
    assert raw is not None and raw.startswith("mcp_")
    from app.services.mcp_tokens import lookup_active_token

    row = await lookup_active_token(db_session, raw)
    assert row is not None and row.user_id == user.id
    assert row.label.startswith("oauth:")


@pytest.mark.asyncio
async def test_exchange_rejects_pkce_mismatch(db_session):
    user = await _user(db_session)
    client = await svc.register_client(db_session, client_name="C", redirect_uris=[REDIRECT])
    code = await svc.mint_code(
        db_session,
        user_id=user.id,
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        code_challenge=s256_challenge("right"),
    )
    assert (
        await svc.exchange_code(
            db_session,
            code=code,
            client_id=client.client_id,
            redirect_uri=REDIRECT,
            code_verifier="wrong",
        )
        is None
    )


@pytest.mark.asyncio
async def test_exchange_code_single_use(db_session):
    user = await _user(db_session)
    client = await svc.register_client(db_session, client_name="C", redirect_uris=[REDIRECT])
    code = await svc.mint_code(
        db_session,
        user_id=user.id,
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        code_challenge=s256_challenge("v"),
    )
    kwargs = dict(code=code, client_id=client.client_id, redirect_uri=REDIRECT, code_verifier="v")
    assert await svc.exchange_code(db_session, **kwargs) is not None
    assert await svc.exchange_code(db_session, **kwargs) is None  # second use dead


@pytest.mark.asyncio
async def test_exchange_rejects_expired_and_mismatches(db_session):
    user = await _user(db_session)
    client = await svc.register_client(db_session, client_name="C", redirect_uris=[REDIRECT])
    code = await svc.mint_code(
        db_session,
        user_id=user.id,
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        code_challenge=s256_challenge("v"),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert (
        await svc.exchange_code(
            db_session,
            code=code,
            client_id=client.client_id,
            redirect_uri=REDIRECT,
            code_verifier="v",
        )
        is None
    )
    # redirect mismatch
    code2 = await svc.mint_code(
        db_session,
        user_id=user.id,
        client_id=client.client_id,
        redirect_uri=REDIRECT,
        code_challenge=s256_challenge("v"),
    )
    assert (
        await svc.exchange_code(
            db_session,
            code=code2,
            client_id=client.client_id,
            redirect_uri="https://evil.example/cb",
            code_verifier="v",
        )
        is None
    )
```

- [ ] **Step 6.2:** Run → FAIL (module missing).

- [ ] **Step 6.3: Implement** `app/mcp/oauth/service.py`:

```python
"""DB operations for the self-host OAuth layer.

``exchange_code`` returns the RAW derived ``mcp_`` token (or ``None`` on
any failure — expired, used, PKCE/client/redirect mismatch). All failure
modes collapse to ``None`` so the router emits one uniform
``invalid_grant`` and nothing leaks about which check failed.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.oauth.pkce import verify_s256
from app.models.mcp_oauth import MCPSelfhostOAuthClient, MCPSelfhostOAuthCode
from app.services import mcp_tokens as token_svc

CODE_TTL_SECONDS = 60
_LABEL_MAX = 40


async def register_client(
    session: AsyncSession, *, client_name: str, redirect_uris: list[str]
) -> MCPSelfhostOAuthClient:
    client = MCPSelfhostOAuthClient(
        client_id=secrets.token_urlsafe(24),
        client_name=client_name[:200],
        redirect_uris=redirect_uris,
    )
    session.add(client)
    await session.flush()
    return client


async def get_client(session: AsyncSession, client_id: str) -> MCPSelfhostOAuthClient | None:
    result = await session.execute(
        select(MCPSelfhostOAuthClient).where(MCPSelfhostOAuthClient.client_id == client_id)
    )
    return result.scalar_one_or_none()


async def mint_code(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    expires_at: datetime | None = None,
) -> str:
    code = secrets.token_urlsafe(32)
    session.add(
        MCPSelfhostOAuthCode(
            code=code,
            user_id=user_id,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            expires_at=expires_at or datetime.now(UTC) + timedelta(seconds=CODE_TTL_SECONDS),
        )
    )
    await session.flush()
    return code


async def exchange_code(
    session: AsyncSession,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
) -> str | None:
    result = await session.execute(
        select(MCPSelfhostOAuthCode).where(MCPSelfhostOAuthCode.code == code).with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    if row.expires_at < datetime.now(UTC):
        return None
    if row.client_id != client_id or row.redirect_uri != redirect_uri:
        return None
    if not verify_s256(code_verifier, row.code_challenge):
        return None

    row.used_at = datetime.now(UTC)
    client = await get_client(session, client_id)
    label = f"oauth:{(client.client_name if client else client_id)}"[:_LABEL_MAX]
    raw, _ = await token_svc.create_token(session, user_id=row.user_id, label=label)
    await session.flush()
    return raw
```

- [ ] **Step 6.4:** Run → 5 PASS. Note: `db_session` (top-level conftest) rolls back per test — service `flush()`es and the fixture's transaction isolates tests.
- [ ] **Step 6.5: Commit** — `git commit -m "feat(mcp-oauth): service layer (DCR, PKCE-bound codes, derived-token exchange)"` (add both files).

---

### Task 7: Authorize page rendering

**Files:**
- Create: `app/mcp/oauth/pages.py`
- Test: `tests/mcp/oauth/test_pages.py`

- [ ] **Step 7.1: Failing tests:**

```python
"""Authorize-page HTML rendering (client identity + hardened token field)."""

from app.mcp.oauth.pages import render_authorize_page


def _page(**overrides) -> str:
    params = dict(
        client_name="Claude",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        client_id="cid",
        state="st",
        code_challenge="ch",
        csrf_token="csrf123",
        error=None,
    )
    params.update(overrides)
    return render_authorize_page(**params)


def test_shows_client_identity():
    html = _page()
    assert "Claude" in html
    assert "claude.ai" in html  # redirect HOST shown, so admin sees who receives the code


def test_token_field_hardened():
    html = _page()
    assert 'type="password"' in html
    assert 'autocomplete="off"' in html
    assert 'name="csrf_token"' in html and "csrf123" in html


def test_html_escapes_client_name():
    html = _page(client_name="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_error_shown_generically():
    html = _page(error="invalid_token")
    assert "check the token" in html.lower()
```

- [ ] **Step 7.2:** Run → FAIL.

- [ ] **Step 7.3: Implement** `app/mcp/oauth/pages.py`:

```python
"""HTML for the paste-token authorize page.

Plain stdlib rendering (html.escape + f-string) — no Jinja: one page,
and keeping it dependency-light makes the escaping obvious to audit.
The page MUST show who is being authorized (client name + redirect
host): the confused-deputy defense from the spec — an attacker can DCR
their own client and link the admin to this (genuine) page, so the
admin needs to see whose callback will receive the code.
"""

from __future__ import annotations

from html import escape
from urllib.parse import urlparse

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:26rem;margin:12vh auto;padding:0 1rem}"
    "input[type=password]{width:100%;padding:.5rem;font-family:monospace}"
    "button{margin-top:1rem;padding:.5rem 1.5rem}"
    ".who{background:#f2f2f7;border-radius:8px;padding:.8rem 1rem}"
    ".err{color:#b00020}"
)


def render_authorize_page(
    *,
    client_name: str,
    redirect_uri: str,
    client_id: str,
    state: str,
    code_challenge: str,
    csrf_token: str,
    error: str | None = None,
) -> str:
    host = urlparse(redirect_uri).netloc or redirect_uri
    err_html = (
        '<p class="err">That didn&#39;t work — check the token and try again.</p>' if error else ""
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Authorize MCP access</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_STYLE}</style></head><body>
<h1>tgram-analytics</h1>
<div class="who">Authorizing <b>{escape(client_name)}</b><br>
code will be sent to <b>{escape(host)}</b></div>
<p>Paste an MCP token from your Telegram bot
(<code>/mcp_token new &lt;label&gt;</code>). If you didn&#39;t start this
in your own MCP client, close this tab.</p>
{err_html}
<form method="post" action="">
  <input type="password" name="token" autocomplete="off" spellcheck="false"
         placeholder="mcp_..." required>
  <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
  <input type="hidden" name="client_id" value="{escape(client_id)}">
  <input type="hidden" name="redirect_uri" value="{escape(redirect_uri)}">
  <input type="hidden" name="state" value="{escape(state)}">
  <input type="hidden" name="code_challenge" value="{escape(code_challenge)}">
  <button type="submit">Authorize</button>
</form>
</body></html>"""
```

- [ ] **Step 7.4:** Run → 4 PASS. **Step 7.5: Commit** — `git commit -m "feat(mcp-oauth): authorize page with client identity + hardened token field"` (add both files).

---

### Task 8: Telegram issuance notification

**Files:**
- Create: `app/mcp/oauth/notify.py`
- Test: `tests/mcp/oauth/test_notify.py`

- [ ] **Step 8.1: Failing tests:**

```python
"""Best-effort Telegram alert on derived-token issuance."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.oauth.notify import notify_token_issued


@pytest.mark.asyncio
async def test_sends_message_with_revoke_button():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with patch("app.bot.setup.get_bot", return_value=bot):
        await notify_token_issued(admin_chat_id=42, client_name="Claude", token_id="abc-123")
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 42
    assert "Claude" in kwargs["text"]
    markup = kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "mcptok:revoke:abc-123"


@pytest.mark.asyncio
async def test_failure_is_swallowed():
    with patch("app.bot.setup.get_bot", side_effect=RuntimeError("bot down")):
        await notify_token_issued(admin_chat_id=42, client_name="C", token_id="x")
    # no raise = pass
```

- [ ] **Step 8.2:** Run → FAIL.

- [ ] **Step 8.3: Implement** `app/mcp/oauth/notify.py` (pattern from `app/api/ingestion.py:89-171` — late-import `get_bot`, send, never raise):

```python
"""Telegram notification when an OAuth flow issues a derived MCP token.

The spec's detectability control: silent token theft (confused-deputy
authorize link) becomes a visible event with a one-tap revoke. Strictly
best-effort — a Telegram outage must never fail the OAuth grant, so
every failure path is swallowed (logged at WARNING).
"""

from __future__ import annotations

import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger("app.mcp.oauth")


async def notify_token_issued(*, admin_chat_id: int, client_name: str, token_id: str) -> None:
    try:
        from app.bot.setup import get_bot

        bot = get_bot()
        await bot.send_message(
            chat_id=admin_chat_id,
            text=(
                "🔑 New MCP client authorized via OAuth: "
                f"<b>{escape(client_name)}</b>\n"
                "Not you? Revoke it now."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🗑 Revoke", callback_data=f"mcptok:revoke:{token_id}")]]
            ),
        )
    except Exception:
        logger.warning("failed to send MCP OAuth issuance notification", exc_info=True)
```

- [ ] **Step 8.4:** Run → 2 PASS. **Step 8.5: Commit** — `git commit -m "feat(mcp-oauth): Telegram issuance notification with inline revoke"` (add both files).

---

### Task 9: OAuth router — /register, /authorize GET+POST, /token

**Files:**
- Create: `app/mcp/oauth/router.py`
- Test: `tests/mcp/oauth/test_router.py`

Crib request/response shapes from the cloud `oauth/router.py` (`DCRRegisterRequest`/`DCRRegisterResponse` around line 280, token endpoint at 547) — but validation goes through Task 6's service and auth is paste-token, not Telegram.

- [ ] **Step 9.1: Failing tests** — `tests/mcp/oauth/test_router.py`. In-process FastAPI app; DB via the `session_factory` fixture wired through `app.core.database.get_session_factory` monkeypatching (read how `tests/test_mcp_token_handler.py` wires handlers to the test DB and reuse that mechanism):

```python
"""HTTP surface: DCR, authorize page, code grant, token exchange."""

import uuid
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.mcp.oauth.pkce import s256_challenge
from app.mcp.oauth.router import build_oauth_router
from app.models.user import User
from app.services import mcp_tokens as token_svc

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


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
```

- [ ] **Step 9.2:** Run → FAIL (module missing).

- [ ] **Step 9.3: Implement** `app/mcp/oauth/router.py`:

```python
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

import logging
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.mcp.oauth import service as svc
from app.mcp.oauth.csrf import issue_csrf, verify_csrf
from app.mcp.oauth.notify import notify_token_issued
from app.mcp.oauth.pages import render_authorize_page
from app.mcp.oauth.rate_limit import RateLimiter

logger = logging.getLogger("app.mcp.oauth")

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


def _open_session():
    from app.core.database import get_session_factory

    return get_session_factory()()


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
            url=f"{redirect_uri}?{urlencode({'code': code, 'state': state})}",
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
        return {
            "access_token": raw,
            "token_type": "Bearer",
            "scope": "mcp:tools",
        }

    return router
```

- [ ] **Step 9.4:** Run `python -m pytest tests/mcp/oauth/test_router.py -q` (with DATABASE_URL) → 8 PASS. If the module-level limiters leak between tests (the DCR-rate-limit test fills the register bucket), add an autouse fixture in this test file that re-instantiates `router_module._register_limiter`/`_authorize_limiter` per test — reset state, don't raise limits.
- [ ] **Step 9.5:** Full suite + commit — `git commit -m "feat(mcp-oauth): oauth router (DCR, paste-token authorize, code->derived-token exchange)"` (add router + tests).

---

### Task 10: Mount in app.main, favicon, /mcp copy

**Files:**
- Modify: `app/main.py` (lifespan — right after the existing MCP mount block)
- Modify: `app/api/health.py` (favicon route)
- Modify: `app/bot/handlers/mcp.py` (Desktop line)
- Test: `tests/mcp/oauth/test_mount_gating.py`

- [ ] **Step 10.1: Failing tests** — `tests/mcp/oauth/test_mount_gating.py`, reusing the booted-uvicorn fixtures from `tests/mcp/conftest.py` (`app_client`, `_boot_server`):

```python
"""OAuth surface mounts on self-host, not with a plugin verifier, not when disabled."""

import httpx
import pytest

from tests.mcp.conftest import _boot_server


@pytest.mark.asyncio
async def test_selfhost_mounts_oauth(app_client):
    r = await app_client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    r = await app_client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    assert "authorization_endpoint" in r.json()
    r = await app_client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")


@pytest.mark.asyncio
async def test_plugin_verifier_disables_oss_oauth():
    from app import extensions as ext

    async with _boot_server(
        mcp_enabled=True, pre_boot=lambda: ext.register_mcp_token_verifier(object())
    ) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            r = await client.get("/.well-known/oauth-authorization-server")
            assert r.status_code == 404


@pytest.mark.asyncio
async def test_oauth_flag_off_unmounts():
    async with _boot_server(mcp_enabled=True, extra_env={"MCP_OAUTH_ENABLED": "false"}) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            assert (await client.get("/.well-known/oauth-authorization-server")).status_code == 404
```

Extend `_boot_server` in `tests/mcp/conftest.py` with two optional kwargs, wired into its existing env/registry handling:

```python
async def _boot_server(
    *,
    mcp_enabled: bool,
    real_db: bool = False,
    database_url: str | None = None,
    extra_env: dict[str, str] | None = None,
    pre_boot: Callable[[], None] | None = None,
) -> AsyncIterator[str]:
```
— merge `extra_env` into the env dict (and into the save/restore `prev` handling), and call `pre_boot()` AFTER `ext._reset_for_tests()` but BEFORE `main_mod.create_app()`. Add `from collections.abc import AsyncIterator, Callable` to its imports.

- [ ] **Step 10.2:** Run → FAIL (`/.well-known/...` 404 on self-host boot; favicon 404).

- [ ] **Step 10.3: Implement mount.** In `app/main.py`, immediately after the existing `if settings.mcp_enabled and not plugin_owns_mcp:` block (inside it, after `await stack.enter_async_context(mcp_lifespan(app))`), add:

```python
# Self-host OAuth for header-less MCP clients (Claude Desktop).
# Only when the DEFAULT verifier is in use: a plugin-registered
# verifier (cloud overlay) brings its own OAuth and well-known.
if settings.mcp_oauth_enabled and get_mcp_token_verifier() is None:
    from app.mcp.oauth.router import build_oauth_router
    from app.mcp.well_known import build_well_known_router

    app.include_router(build_oauth_router(), prefix="/mcp/oauth")
    app.include_router(build_well_known_router(public_url=settings.mcp_effective_public_url))
```

(`get_mcp_token_verifier` is already imported in that block.)

- [ ] **Step 10.4: Favicon.** In `app/api/health.py` add (with `from fastapi.responses import Response` import):

```python
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#2AABEE"/>'
    '<rect x="6" y="17" width="4" height="9" rx="1" fill="#fff"/>'
    '<rect x="14" y="11" width="4" height="15" rx="1" fill="#fff"/>'
    '<rect x="22" y="6" width="4" height="20" rx="1" fill="#fff"/>'
    "</svg>"
)


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Tiny inline SVG so MCP connectors and browsers show a real icon."""
    return Response(
        content=_FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )
```

- [ ] **Step 10.5: /mcp copy.** In `app/bot/handlers/mcp.py`, replace the "Claude Desktop / Cursor: add an HTTP MCP server..." sentence in the self-host reply with:

```python
"Claude Desktop: Settings → Connectors → <b>Add custom connector</b>,"

f"URL <code>{esc}</code> — a browser page opens; paste a token from "
"/mcp_token there.\n"
"Cursor: add an HTTP MCP server with URL "
f"<code>{esc}</code> and header "
"<code>Authorization: Bearer YOUR_TOKEN</code>.\n\n"
```

Check `tests/test_mcp_command_handler.py` still passes (it asserts on URL + `/mcp_token new` + `--transport http`, all still present).

- [ ] **Step 10.6:** Run gating tests + `tests/test_mcp_command_handler.py` + full suite → green.
- [ ] **Step 10.7: Commit** — `git commit -m "feat(mcp-oauth): mount self-host oauth + well-known, favicon route, /mcp Desktop copy"`

---

### Task 11: Proxy-headers https fix

**Files:**
- Modify: `Dockerfile:43` (CMD), `.env.example`, `README.md`
- Modify: `tests/mcp/conftest.py` (`_boot_server` uvicorn config)
- Test: append to `tests/mcp/oauth/test_mount_gating.py`

- [ ] **Step 11.1: Failing test** (append):

```python
@pytest.mark.asyncio
async def test_forwarded_proto_yields_https_redirect(app_client):
    """Behind a TLS-terminating proxy the /mcp 307 must point at https, not http."""
    r = await app_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Accept": "application/json, text/event-stream",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "203.0.113.9",
        },
        follow_redirects=False,
    )
    assert r.status_code == 307
    assert r.headers["location"].startswith("https://")
```

Note: `app_client` has `follow_redirects=True` client-wide; per-request `follow_redirects=False` overrides it.

- [ ] **Step 11.2:** Run → FAIL (`location` starts `http://` — uvicorn isn't trusting forwarded headers).

- [ ] **Step 11.3: Implement.** In `tests/mcp/conftest.py` `_boot_server`, extend `uvicorn.Config(...)` with:

```python
proxy_headers = (True,)
forwarded_allow_ips = ("*",)
```

In `Dockerfile` line 43, change the exec tail from:

```
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
```
to:
```
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
```

In `.env.example`, extend the MCP block comment: `# Behind a TLS proxy (Cloudflare, Coolify) set MCP_PUBLIC_URL=https://your-host so OAuth metadata and connect instructions use https.` In `README.md` "Connect Claude (MCP)" section, add one line: Claude Desktop is supported via Settings → Connectors → Add custom connector (paste an `/mcp_token` token in the browser page that opens).

- [ ] **Step 11.4:** Run the new test → PASS. Full suite → green.
- [ ] **Step 11.5: Commit** — `git commit -m "fix(mcp): trust proxy headers so /mcp redirects are https behind TLS proxies"`

---

### Task 12: End-to-end — OAuth-issued token drives the MCP SDK client

**Files:**
- Test: `tests/mcp/oauth/test_e2e_oauth.py`

- [ ] **Step 12.1: Write the test.** Reuses `_boot_server(real_db=True, database_url=...)` + the flow helpers from Task 9, then the MCP SDK client exactly as `tests/mcp/test_mount.py::test_mcp_client_lists_tools_with_static_token` does (read it; copy the streamable-http wiring):

```python
"""Full stack: DCR -> authorize (paste token) -> /token -> MCP tools/list."""

import re
import uuid
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

            async with streamablehttp_client(
                url=base.rstrip("/") + "/mcp/",
                headers={"Authorization": f"Bearer {access}"},
            ) as (read, write, _):
                async with ClientSession(read, write) as mcp_session:
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
```

(The issuance notification fires against the stubbed bot inside `_boot_server` — `init_bot` is patched, so `get_bot()` raises and `notify_token_issued` swallows it; that's the intended best-effort path.)

- [ ] **Step 12.2:** Run → PASS. If the 302 from authorize or the token POST 400s, debug via the Task 9 unit tests first — the e2e should only fail on wiring, not logic.
- [ ] **Step 12.3:** Full suite + lint:

```bash
DATABASE_URL=postgresql+asyncpg://tga:password@localhost/tganalytics_test python -m pytest tests/ -q
python -m mypy app   # only the pre-existing aggregation.py unused-ignore may appear
python -m ruff check app tests && python -m ruff format app tests
```

- [ ] **Step 12.4: Commit** — `git commit -m "test(mcp-oauth): end-to-end DCR->authorize->token->MCP tools/list"`

---

### Task 13: Push + PR (orchestrator gate)

- [ ] **Step 13.1:** `git push -u origin feat/mcp-oauth-selfhost`
- [ ] **Step 13.2:** Open PR against `main`: title `feat(mcp): self-host OAuth (paste-token) so Claude Desktop can connect`. Body: problem (Desktop is OAuth-only; discovery 404'd), the derived-token design (no JWT, StaticTokenVerifier unchanged, revocable via /mcp_token), the confused-deputy mitigations (client identity on page, Telegram issuance alert + revoke button, CSRF, rate limits), table naming vs cloud, proxy-headers fix, favicon. Wait for CI; if hosted runners stall (seen before), `gh run rerun <id> --failed`.

---

## Self-review notes (applied)

- Spec §4 endpoints → Tasks 4 (well-known), 9 (register/authorize/token). §5 tables → Task 2 (renamed; spec amended in Task 1). §6 flow → Task 12 e2e. §7 proxy/https → Task 11. §8 favicon → Task 10. §9 settings → Task 1. §10 security: PKCE (T3/T6/T9), single-use+TTL (T6), redirect exact-match (T6/T9), CSRF (T3/T9), client identity (T7), rate limits (T5/T9), Telegram notification (T8/T9), password-field hygiene (T7). §10.1 mitigations all covered. §11 test list → mapped 1:1 across task tests (incl. gating, proxy-header, notification-failure tolerance).
- Spec's "no token in logs": form bodies are never logged by the app; the raw `mcp_` token passes through `lookup_active_token` (hash immediately). The existing `RedactingFilter` token pattern covers accidental `token=...` log lines. No code task needed; noted here deliberately rather than adding speculative patterns (YAGNI).
- Type consistency: `build_oauth_router()` (T9) matches the mount (T10); `build_well_known_router(public_url=...)` (T4) matches T10; service signatures in T6 match T9's calls; `_boot_server(extra_env=, pre_boot=)` (T10) matches T11/T12 usage.
- YAGNI cuts vs cloud: no refresh tokens, no state table, no revocation table (derived tokens ARE `mcp_tokens` rows — `/mcp_token` revoke covers them), no slowapi dep.
