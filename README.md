# tgram-analytics · server

> Self-hosted, privacy-first analytics controlled entirely through a Telegram bot.
> No dashboard. No third parties. Just Telegram.
> Or skip the setup — try the hosted version at [@MyTelegramAnalyticsBot](https://t.me/MyTelegramAnalyticsBot).

[![CI](https://github.com/tgram-analytics/server/actions/workflows/ci.yml/badge.svg)](https://github.com/tgram-analytics/server/actions/workflows/ci.yml)
[![License: FSL-1.1-ALv2](https://img.shields.io/badge/License-FSL--1.1--ALv2-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)

---

## Quick start

### 1 — Prerequisites

- Docker & Docker Compose v2
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram chat ID (message [@userinfobot](https://t.me/userinfobot) to find it)

### 2 — Configure

```bash
git clone https://github.com/tgram-analytics/server.git
cd server
cp .env.example .env
# Edit .env and fill in TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, and SECRET_KEY
```

Generate a `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 3 — Run

```bash
docker compose up
```

The server starts on `http://localhost:8000`.
Verify it's running:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### 4 — Add your first project

Open Telegram and message your bot:

```
/add myapp.com
```

The bot replies with your API key (`proj_xxxx`) and a ready-to-use JS snippet.

---

## Usage

### Track events (REST API)

```bash
curl -X POST https://your-server.com/api/v1/track \
  -H "Content-Type: application/json" \
  -d '{
    "api_key": "proj_xxxxxxxxxxxx",
    "event_name": "purchase",
    "session_id": "uuid-here",
    "properties": {"amount": 49, "plan": "pro"}
  }'
```

### JavaScript SDK

```html
<script src="https://your-server.com/sdk/tga.min.js"></script>
<script>
  TGA.init('proj_xxxxxxxxxxxx', { serverUrl: 'https://your-server.com' });
  TGA.track('purchase', { amount: 49 });
</script>
```

### Flutter SDK

```dart
await TgAnalytics.init(
  apiKey: 'proj_xxxxxxxxxxxx',
  serverUrl: 'https://your-server.com',
);
await TgAnalytics.track('purchase', properties: {'amount': 49});
```

### Multi-value properties

Event `properties` accept **arrays of scalars** in addition to single
scalars — useful for multi-select inputs, A/B variant memberships, and
any set-style attribute that would otherwise be lossy to flatten:

```bash
curl -X POST https://your-server.com/api/v1/track \
  -H "Content-Type: application/json" \
  -d '{
    "api_key": "proj_xxxxxxxxxxxx",
    "event_name": "onboarding_completed",
    "session_id": "uuid-here",
    "properties": {
      "role": "creator",
      "interest": ["vertical_to_horizontal", "unsure"]
    }
  }'
```

Arrays may only contain scalars (`string`, `number`, `boolean`, `null`).
Nested objects, nested arrays, and `undefined` are rejected with **422**.

#### Sort behaviour

The server **sorts every array property at write time**. There's no
naming convention to remember — `interest`, `tags`, `flags`, anything
goes — so `["a", "b"]` and `["b", "a"]` collapse to the same JSONB
value and the "most common combos" query is a trivial `GROUP BY`
without read-time normalisation. Mixed-type arrays (e.g. `[1, "a"]`)
can't be `<`-compared in Python; those fall back to insertion order
instead of failing the request.

> **Order-sensitive use cases.** Because every array is sorted, a list
> like `recent_searches: ["pizza", "pasta"]` loses its original order
> on the wire. If insertion order matters, serialize to a string
> (`"pizza,pasta"`) or use an object shape with positional keys.

#### Canonical dashboard queries

The JSONB `properties` column lets the same event power two
complementary dashboards:

```sql
-- 1. Per-element count (e.g. a pie chart of how often each
--    interest is selected):
SELECT elem, count(*) AS n
FROM events,
     jsonb_array_elements_text(properties->'interest') AS elem
WHERE event_name = 'onboarding_completed'
GROUP BY elem
ORDER BY n DESC;

-- 2. Most common combinations of selected values:
SELECT properties->'interest' AS combo, count(*) AS n
FROM events
WHERE event_name = 'onboarding_completed'
GROUP BY combo
ORDER BY n DESC
LIMIT 20;
```

No schema migration is needed — Postgres `JSONB` stores arrays natively.

### Browser vs. server calls

One `proj_` API key handles both: embed it in your frontend **and** use it from
your backend — events land in the same project.

The **domain allowlist** (set per project via `/projects` → **Settings**) is a browser-only guard against
abuse of the public key embedded in your JS bundle. It works like this:

| Caller | `Origin` header | Behavior |
|---|---|---|
| Browser on allowed host | `https://myapp.com` | ✅ accepted |
| Browser on other host | `https://evil.com` | ❌ 403 |
| Backend SDK (Python/Node/curl) | *(absent)* | ✅ accepted — API key auth only |
| Sandboxed iframe / `file://` | `null` | ❌ 403 when allowlist is set |

Allowlist entries support bare hosts (`myapp.com`), full URLs, and wildcards
(`*.myapp.com` matches any subdomain, but not the apex — add both explicitly
if you need `myapp.com` and `www.myapp.com`).

An empty allowlist allows all origins.

### Bot commands

| Command | Description |
|---|---|
| `/start` | Home menu (first run shows a guided welcome) |
| `/add <name>` | Create a new project and get its API key |
| `/projects` | List all projects |
| `/events` | Browse event types for a project |
| `/report [event]` | Get a chart for an event (with period/granularity controls) |
| `/digest` | Last-7-days recap: sessions and alerted-event counts with week-over-week deltas, per project |
| `/overview` | Multi-line visits chart across all projects |
| `/alerts` | List active alerts across all projects |
| `/doctor` | Health check across all projects (silent projects, open allowlists) |
| `/mcp` | Setup instructions for connecting an AI agent (MCP) |
| `/mcp_token` | Manage static MCP access tokens (`/mcp_token new [label]` to create) |
| `/help` | Show this command reference |
| `/cancel` | Cancel the current multi-step operation |

Project settings (retention, domain allowlist, API-key rotation) have no
slash command — open `/projects`, pick a project, and use its **Settings**
menu.

---

## Connect Claude (MCP)

The server exposes an MCP endpoint at `/mcp` so Claude Code, Claude
Desktop, Cursor, and other MCP clients can query your analytics and
help you integrate the SDKs.

1. In Telegram, send `/mcp_token new claude` to your bot. Copy the
   `mcp_...` token — it is shown only once.
2. Add the server to Claude Code:

   ```bash
   claude mcp add --transport http tgram https://your-server.example.com/mcp \
     --header "Authorization: Bearer mcp_..."
   ```

Claude Desktop is supported via Settings → Connectors → Add custom
connector (paste an `/mcp_token` token in the browser page that opens).

Revoke tokens anytime with `/mcp_token`. Set `MCP_ENABLED=false` to
remove the endpoint entirely. `MCP_PUBLIC_URL` overrides the base URL
used in metadata and CORS/Host allow-lists (defaults to
`WEBHOOK_BASE_URL`).

---

## Development

### Setup

```bash
python -m venv .venv
source .venv/bin/activate
make install
cp .env.example .env   # edit values
```

### Run locally (with Docker DB)

```bash
make dev-db          # start postgres in Docker
make migrate         # apply migrations
uvicorn app.main:app --reload
```

### Tests

```bash
make test            # run all tests
make test-cov        # with coverage report
```

### Code quality

```bash
make lint            # ruff linter
make typecheck       # mypy
make check           # both
```

### Database migrations

```bash
# Create a new migration
make migration MSG="add users table"

# Apply migrations
make migrate

# Roll back one step
make downgrade
```

### Database requirements

- **PostgreSQL ≥ 15**. Required for reliable core `gen_random_uuid()` and
  JSONB features used by newer migrations.
- The `pgcrypto` extension is enabled automatically by migration `0004`. On
  managed Postgres this just works; on self-managed Postgres the role running
  the first migration needs superuser (or a DBA must pre-enable the extension).

---

## Privacy posture

The server is designed so that self-hosters meet GDPR's data-minimisation
expectations out of the box, and so the managed version we operate at
@MyTelegramAnalyticsBot inherits the same protections.

- **Visitor identification.** No cookies, no client-side fingerprinting.
  Each event is tagged with a 16-character hash of
  `sha256(daily_salt || project_id || client_ip || user_agent)`. The
  daily salt rotates every UTC midnight, so the same visitor cannot be
  re-identified across days. Raw IP and raw User-Agent are never persisted.
- **User-Agent parsing.** UA strings are parsed by `ua-parser` into
  `browser`, `os`, and `device_type` (mobile/tablet/desktop/bot/unknown).
  The raw string is dropped before insertion.
- **PII tripwire on `properties`.** A small key denylist (email, phone,
  ssn, password, token, credit_card, card_number, cvv, iban, tax_id)
  silently drops matching keys at ingestion time and increments an
  internal counter — the request still returns `202` so a hostile
  caller cannot probe the rule by watching status codes. Properties
  larger than 4 KB are zeroed out the same way.
- **Log redaction.** A root-logger filter masks `proj_<64hex>`,
  `sk_(live|test)_*` API keys, and inline `email=…` / `password=…`
  patterns in every emitted log line.
- **Retention.** A nightly APScheduler job (03:00 UTC) deletes events
  older than each project's `retention_days`. A value of `0` keeps
  events forever — the default for self-host.
- **Audit log.** Destructive actions (project create/delete, settings
  changes, API-key rotation, suspension) are written to an append-only
  `audit_events` table. A Postgres trigger rejects UPDATE and DELETE
  for every row and every role, including the application role itself.

### `REDIS_URL` (optional)

`get_today_salt()` falls back to a process-local cache when `REDIS_URL`
is unset, which is fine for single-replica self-host. **For multi-replica
deployments, set `REDIS_URL`** so all replicas share the same daily salt;
otherwise the same visitor will hash to different IDs depending on which
replica handled the request.

---

## Architecture

```
app/
├── api/          REST endpoints (track, pageview, projects)
├── bot/          Telegram bot handlers and conversation state
├── core/         Config, database engine, security utilities
├── models/       SQLAlchemy ORM models
├── schemas/      Pydantic request/response schemas
└── services/     Analytics, charts, scheduler, alerts
```

See [PROJECT.md](../PROJECT.md) for full architecture documentation.

---

## Extension points

The server exposes a small, stable set of hooks in [`app/extensions.py`](app/extensions.py) that downstream packages may use to customize behavior without forking. Six registries are available:

| Hook | Purpose | Cardinality |
|---|---|---|
| `register_user_resolver(callable)` | Replace the default singleton User resolver | one (raises if registered twice) |
| `register_project_pre_create(callable)` | Append a pre-flush quota/policy check | many (run in registration order) |
| `register_bot_filter(filter)` | Append a bot-handler filter, AND-combined with the admin chat gate | many |
| `register_http_router(prefix, router_or_app, lifespan=None)` | Mount a FastAPI `APIRouter` (or any ASGI app) at startup, with an optional lifespan composed into the main app's | many |
| `register_mcp_token_verifier(verifier)` | Replace the default static MCP bearer-token verifier (backed by the `mcp_tokens` table) | one (raises if registered twice) |
| `register_mcp_whoami_extra(callable)` | Append a hook that contributes extra fields to the `whoami` MCP tool output (merged last-write-wins) | many (run in registration order) |

A plugin is any Python module that calls one or more of these from a top-level `register()` function. Plugins are discovered at server startup via two mechanisms (in this order):

1. **Python entry points** in the `tgram_analytics.extensions` group. In your plugin's `pyproject.toml`:

    ```toml
    [project.entry-points."tgram_analytics.extensions"]
    my-plugin = "my_plugin:register"
    ```

2. **`TGA_EXTENSIONS` env var**, comma-separated module paths whose `register()` is called:

    ```bash
    export TGA_EXTENSIONS=my_plugin,another_plugin
    uvicorn app.main:app
    ```

### Extending Settings

To add new env vars to `Settings`, subclass it in your plugin and monkey-patch the class. Pydantic propagates `model_config` automatically, including the `extra="ignore"` policy that lets unknown env vars pass through without error.

```python
# my_plugin/__init__.py
from app.core import config as app_config


class ExtendedSettings(app_config.Settings):
    my_extra_var: str = "default"


def register() -> None:
    app_config.Settings = ExtendedSettings  # type: ignore[misc]
```

A working reference plugin lives at [`tests/fixtures/resolver_plugin.py`](tests/fixtures/resolver_plugin.py). For the loader contract see [`app/plugins.py`](app/plugins.py); for the registry surface see [`app/extensions.py`](app/extensions.py).

---

## Deployment

### VPS (Docker Compose)

```bash
cp .env.example .env
# Fill in all values, especially WEBHOOK_BASE_URL=https://your-domain.com
docker compose up -d
```

Point your reverse proxy (Nginx/Caddy) at port `8000`.

### Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template)

Add the environment variables from `.env.example` in the Railway dashboard.

### Updating

```bash
git pull
docker compose up -d --build
```

Database migrations run automatically when the container starts — no manual
step. Your `.env` and the `pgdata` volume are untouched by updates; check the
release notes for any new environment variables before pulling.

On Railway, redeploy from the latest commit in the dashboard.

---

## Contributing

Contributions are welcome. Please open an issue before submitting a pull request for anything beyond a typo or small bug fix, so we can discuss the approach first.

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Write tests alongside your code
4. Ensure `make check` and `make test` pass
5. Open a pull request

Please follow the existing code style (ruff-enforced) and keep PRs focused.

---

## Disclaimer
> This project is an independent open-source project, not affiliated
> with or endorsed by Telegram Messenger LLP or its parent company in any way.
> "Telegram" is a trademark of Telegram Messenger LLP.

## License

[Functional Source License, Version 1.1, ALv2 Future License (FSL-1.1-ALv2)](LICENSE).

In plain language:

- **You can self-host and use this freely** for your own needs — personal,
  internal business, non-commercial research, professional services.
- **You cannot resell it as a competing hosted analytics service.** For two
  years after each release we reserve the right to be the only ones offering
  this as a commercial managed product.
- **On the second anniversary of each release, that release automatically
  relicenses to Apache License 2.0** — fully permissive, forever.

See the [FSL FAQ](https://fsl.software/) for details.

The client SDKs (`tgram-analytics-js`, `-py`, `-flutter`) remain under MIT so you
can ship them with any project.
