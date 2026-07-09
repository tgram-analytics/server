# Self-Hosted MCP OAuth ("paste-token") Design

**Status:** draft for review
**Date:** 2026-07-09
**Repo:** `tgram-analytics/server` (OSS)
**Motivating problem:** Claude Desktop's "Add custom connector" is OAuth-only — it has no header field, so the static `/mcp_token` bearer can't be entered. Desktop hits our `/mcp/` 401, reads the `resource_metadata` URL from `WWW-Authenticate`, fetches it, gets **404** (no OAuth on self-host), and fails. Static tokens work only in header-capable clients (Claude Code CLI, Cursor).

## 1. Goal

Let OAuth-only MCP clients (Claude Desktop, and any future GUI client) connect to a self-hosted instance, without a Telegram Login Widget or BotFather domain setup. The "login" step is: the user pastes a token they already minted with `/mcp_token` into a small browser page. Everything else is standard OAuth 2.1 + the MCP authorization spec, so clients that auto-discover work unchanged.

**Non-goals (v1):** Telegram-widget login (that's the cloud's flow), refresh tokens, multi-user consent screens, scopes beyond `mcp:tools`.

## 2. Why paste-token (vs. full Telegram OAuth)

The user is the instance admin and already has a way to prove identity: minting a token via the admin-gated `/mcp_token` bot command. So the browser step doesn't need to re-authenticate via Telegram — it just needs to accept an existing token. This avoids the Login Widget, BotFather `/setdomain`, and the JWT/refresh machinery the cloud carries, while still speaking the OAuth the GUI clients require.

## 3. Key reuse — no new token verifier

The `/token` endpoint does **not** mint a JWT. It mints a **derived static token**: a new `mcp_tokens` row (raw `mcp_<hex>`, hashed at rest, bound to the same `user_id`, labeled `oauth:<client_name>`). That derived token is returned as the OAuth `access_token`. MCP calls are then verified by the **existing `StaticTokenVerifier`** — zero new verification path. Revocation and listing work through the existing `/mcp_token` UI (the derived token shows up there and is revocable).

Net: the OAuth layer is a browser front-end that exchanges a pasted master token for a client-scoped derived token, wrapped in the OAuth dance Desktop expects.

## 4. Endpoints

Mounted from `app.main` (not a plugin) when `settings.mcp_enabled and settings.mcp_oauth_enabled and get_mcp_token_verifier() is None`. The last clause means self-host only: when the cloud overlay has registered its own verifier + OAuth, OSS does not also mount this (no conflict).

| Method | Path | Purpose |
|---|---|---|
| GET | `/.well-known/oauth-protected-resource` and `/.well-known/oauth-protected-resource/mcp` | RFC 9728 resource metadata; `authorization_servers: [issuer]` |
| GET | `/.well-known/oauth-authorization-server` | RFC 8414 AS metadata: `authorization_endpoint`, `token_endpoint`, `registration_endpoint`, `code_challenge_methods_supported: ["S256"]`, `grant_types_supported: ["authorization_code"]`, `response_types_supported: ["code"]` |
| POST | `/mcp/oauth/register` | Dynamic Client Registration (RFC 7591). Stores client, returns `client_id` (public client; no secret required). Rate-limited. |
| GET | `/mcp/oauth/authorize` | Renders the "paste your MCP token" HTML page. Validates `response_type=code`, `client_id` registered, `redirect_uri` registered-match, `code_challenge` + `code_challenge_method=S256` present. |
| POST | `/mcp/oauth/authorize` | Form submit: the pasted token + oauth params (hidden fields). Validates token via `mcp_tokens` lookup; on success mints an authorization code and 302s to `redirect_uri?code=…&state=…`; on failure re-renders with a generic error. |
| POST | `/mcp/oauth/token` | `grant_type=authorization_code`. Verifies code (single-use, unexpired, client+redirect match) and PKCE (`S256(code_verifier) == code_challenge`), then mints the derived `mcp_tokens` access token. Returns `{access_token, token_type: "Bearer", scope: "mcp:tools"}`. |

All absolute URLs in metadata use `settings.mcp_effective_public_url` (https), never the request scheme (see §7).

## 5. Data model

Two new tables; reuse `mcp_tokens` for issued access tokens.

```
mcp_oauth_clients
  id             UUID PK
  client_id      TEXT UNIQUE
  client_name    TEXT
  redirect_uris  TEXT[]            -- exact-match allowlist
  created_at     TIMESTAMP

mcp_oauth_authorization_codes
  code           TEXT PK           -- opaque, single-use
  user_id        UUID FK -> users
  client_id      TEXT FK -> mcp_oauth_clients.client_id
  redirect_uri   TEXT
  code_challenge TEXT              -- S256 challenge
  expires_at     TIMESTAMP         -- 60s after issue
  used_at        TIMESTAMP NULL    -- set on first exchange; second use rejected
```

Migration: revision `0011_mcp_oauth` (down `0010`), OSS-owned. (The cloud's own oauth tables are separately namespaced — see the cloud alembic fix — so no collision.)

## 6. Flow

```
Claude Desktop            OSS server                          browser
   │ add connector (URL)      │                                  │
   │── POST /mcp/ ───────────▶│ 401 + WWW-Authenticate(resource_metadata)
   │── GET well-known ───────▶│ resource + AS metadata
   │── POST /register ───────▶│ client_id
   │── open /authorize ───────────────────────────────────────▶│ "Paste your MCP token" page
   │                          │◀── POST token + params ─────────│ user pastes /mcp_token value
   │                          │ validate token (mcp_tokens)     │
   │                          │ mint auth code (PKCE-bound)      │
   │◀── 302 redirect_uri?code&state ───────────────────────────│
   │── POST /token (code,verifier)▶ verify PKCE + code, mint derived mcp_ token
   │◀── access_token ─────────│
   │── POST /mcp/ Bearer ─────▶│ StaticTokenVerifier validates derived token → tools
```

## 7. Proxy / HTTPS correctness (required)

Behind Cloudflare, the app currently mints `http://` redirects (observed: `POST /mcp` → `307 Location: http://…/mcp/`). OAuth redirects and metadata MUST be https or clients reject them. Fix:

- Trust proxy headers: run uvicorn with `--proxy-headers --forwarded-allow-ips="*"` (or add Starlette `ProxyHeadersMiddleware`), so `X-Forwarded-Proto: https` is honored.
- Metadata/issuer URLs are built from `settings.mcp_effective_public_url`, not `request.url`, so they're https regardless.
- Document `MCP_PUBLIC_URL=https://…` for self-host behind a proxy.

## 8. Favicon (cosmetic)

Desktop shows a fallback icon because `/favicon.ico` → 404. Add a small route serving a static icon (bundled PNG/SVG) at `/favicon.ico` so connectors show a real logo.

## 9. Settings

```
mcp_oauth_enabled: bool = True      # effective only on self-host (default StaticTokenVerifier)
```

No signing key needed (no JWT). No BotFather setup.

## 10. Security

- **PKCE S256 required.** Reject missing/`plain` challenge at `/authorize`; reject mismatch at `/token`.
- **Auth code:** opaque (`secrets.token_urlsafe`), single-use (`used_at`), 60s TTL, bound to `user_id + client_id + redirect_uri + code_challenge`.
- **redirect_uri:** exact match against the client's registered `redirect_uris` at both `/authorize` and `/token` (no open redirect).
- **Pasted token:** validated via the existing constant-time hash lookup (`mcp_tokens`); never echoed back; generic error on failure. The page is served only on the MCP public host.
- **Derived access token:** a normal `mcp_tokens` row → hashed at rest, revocable and listable via `/mcp_token`, labeled `oauth:<client>`.
- **DCR:** rate-limited to prevent client-registration spam.
- **No token in logs.** Authorize POST body (token) is scrubbed by the existing redacting log filter; add the field name to its patterns if needed.

### 10.1 Threat: attacker-initiated authorize link (confused deputy)

DCR is open, so an attacker can register their own client + redirect_uri and
send the admin a link to the *genuine* authorize page. If the admin pastes
their token, the auth code is delivered to the attacker's redirect_uri and —
since the attacker generated the PKCE challenge — they can exchange it for a
derived access token. PKCE does not defend against this (the attacker *is*
the flow initiator). The design trains the user to paste a powerful
credential into a browser page, so this must be made visible and detectable:

- **Show who is asking.** The authorize page prominently renders the
  client_name and the redirect host: "Authorizing **{client_name}** —
  callback to **{redirect host}**". An unexpected client/host is the
  admin's cue to close the tab.
- **Telegram notification on every issuance.** When `/token` mints a derived
  token, the bot messages the admin: "🔑 New MCP client authorized:
  *{client_name}*. Not you? Revoke below." with an inline revoke button
  (reuses the `mcptok:revoke:` callback). Silent theft becomes a detectable
  event. Best-effort: notification failure must not fail the token grant.
- **CSRF token** on the authorize form (issued at GET, checked at POST).
- **Rate-limit the authorize POST** (per-IP) — pointless for guessing a
  256-bit token but blunts drive-by hammering and DoS.
- **Input hygiene:** token field is `type=password`, `autocomplete="off"`,
  `spellcheck="false"`; value never echoed back into the page.

Residual risk: the admin can still be socially engineered into approving an
attacker's client, but the client identity is displayed, the grant is
announced in Telegram, and the token is listed and revocable via
`/mcp_token`. Acceptable for a single-admin read-mostly surface. A stronger
"Confirm in Telegram" flow (bot sends approve/deny buttons; nothing pasted
at all) is the designated v2 upgrade and removes the paste habit entirely.

## 11. Testing

- Metadata endpoints return correct https URLs and S256-only.
- DCR round-trip; rate limit trips.
- `/authorize` GET renders; rejects bad client_id / redirect_uri / missing PKCE.
- `/authorize` GET output contains the registered client_name and redirect host.
- `/authorize` POST: valid token → code + redirect; invalid token → error, no code.
- `/authorize` POST without a valid CSRF token → rejected, no code minted.
- `/token` success sends the Telegram "new MCP client authorized" notification
  (bot mocked); notification failure does not fail the grant.
- `/token`: PKCE mismatch rejected; code single-use (second exchange 400); expired code rejected; redirect/client mismatch rejected.
- End-to-end: run the full authorize→token dance, then call `/mcp` `tools/list` with the derived token (real MCP SDK client) → success.
- OAuth endpoints NOT mounted when a custom verifier is registered (cloud) or `mcp_oauth_enabled=false`.
- Proxy-header test: with `X-Forwarded-Proto: https`, `/mcp` redirect Location is https.

## 12. Rollout

Single OSS PR: settings + 2 tables/migration + oauth module (`app/mcp/oauth/`) + mount in `app.main` + proxy-headers + favicon + `/mcp` command copy (mention "Claude Desktop: add the server URL and paste your token when the browser opens"). Reuse the cloud oauth router's endpoint shapes where possible (DCR/PKCE/code storage), swapping Telegram-login → paste-token and JWT → derived `mcp_tokens` row.

## 13. Open questions

1. Bundle an icon asset (PNG) in the repo for the favicon, or ship an inline SVG route? (Lean: small inline SVG, no binary in git.)
2. Should the authorize page also offer a one-click "I don't have a token yet → run /mcp_token" hint with the bot deep-link? (Nice-to-have.)
3. Derived-token TTL: match static tokens (no expiry, revoke manually) for v1; add expiry later if needed.
