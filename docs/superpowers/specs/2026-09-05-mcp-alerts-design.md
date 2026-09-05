# MCP alert tools + alert delivery history

Date: 2026-09-05. Status: approved design, pending implementation plan.

## Goal

Let an MCP client (Claude Code, Cursor) read the alerts configured on a
project, read the history of alert notifications the server sent, and
create, pause/resume, or delete alerts. Today the MCP has no alert tools
and the server keeps no record of what it sent.

## Non-goals

- Retention or purge of delivery history.
- Editing the condition of an existing alert (delete + create instead).
- Mute / silence from MCP (stays in the Telegram bot buttons).
- Any change to how or when Telegram notifications are sent.

## Data

New table `alert_deliveries`, alembic revision `0013` (down_revision `0012`).

| column | type | notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` default |
| `alert_id` | UUID, FK `alerts.id` ON DELETE SET NULL, nullable | history survives alert deletion |
| `project_id` | UUID, FK `projects.id` ON DELETE CASCADE, not null | |
| `event_name` | text, not null | snapshot |
| `condition` | `alert_condition` enum, not null | snapshot, reuse existing PG enum (`create_type=False`) |
| `threshold_n` | integer, nullable | snapshot |
| `fired_at` | timestamptz, not null, default `now()` | |
| `delivered` | boolean, not null | true when `bot.send_message` returned without raising |
| `error` | text, nullable | exception class name on failure, else NULL |

Index: `ix_alert_deliveries_project_fired` on `(project_id, fired_at DESC)`.

ORM model `app/models/alert_delivery.py`, class `AlertDelivery`, registered
in `app/models/__init__.py`.

## Write path

In `_run_alert_evaluation` (`app/api/ingestion.py`), inside the existing
`for alert in fired:` loop, after the `try: await bot.send_message(...)
except Exception:` block, call
`record_delivery(session, alert=alert, delivered=<bool>, error=<str|None>)`.
The row is flushed in the same transaction that already commits the
counter updates. A send failure still produces a row with
`delivered=false`. The Telegram message content and buttons do not change.

## Service layer (`app/services/alerts.py`)

- `record_delivery(session, *, alert, delivered, error=None) -> AlertDelivery`
  flushes one row using the alert's current `event_name`, `condition`,
  `threshold_n`, `project_id`.
- `list_deliveries(session, project_id, *, since: datetime, limit: int,
  event_name: str | None = None) -> list[AlertDelivery]` ordered by
  `fired_at DESC`.
- `set_alert_active(session, alert_id, project_id, is_active: bool) ->
  Alert | None` explicit set (existing `toggle_alert` stays for the bot).

Existing `create_alert`, `list_alerts`, `delete_alert` are reused unchanged.

## MCP tools (`app/mcp/tools/alerts.py`, `register_alert_tools(mcp)`)

Registered from `register_all_tools`. Every tool: read token via
`get_access_token()`, return `not authenticated` error part if missing;
parse `project_id` as UUID (bad input error part on failure); call
`assert_project_owned_by` before any service call; translate
`ProjectNotOwnedError` to the standard not-owned error part. Handlers
never raise.

| tool | params | annotations | returns |
|---|---|---|---|
| `list_alerts` | `project_id` | readOnly, idempotent, closed world | `ListAlertsResult{alerts: [AlertInfo]}` |
| `alert_history` | `project_id`, `period="7d"`, `limit=50` (1..500), `event_name=None` | readOnly, idempotent, closed world | `AlertHistoryResult{project_id, period, rows: [AlertDeliveryRow]}` |
| `create_alert` | `project_id`, `event_name`, `condition` (`every`/`every_n`/`threshold`), `threshold_n=None` | destructiveHint=False, idempotentHint=False | `AlertInfo` |
| `set_alert_active` | `project_id`, `alert_id`, `is_active: bool` | idempotentHint=True, destructiveHint=False | `AlertInfo` |
| `delete_alert` | `project_id`, `alert_id` | destructiveHint=True | `DeleteAlertResult{deleted: bool, alert_id}` |

`period` is parsed with the existing helper in `app/mcp/tools/_periods.py`.

`create_alert` validates through the existing `AlertCreate` pydantic
schema; a `ValidationError` becomes a bad-input error part with the
message. Direct create, no bot approval step. `delete_alert` writes an
`audit_events` row (`action="alert.delete"`, `target_type="alert"`,
`target_id=<alert_id>`, metadata `{project_id, event_name, condition}`)
via `write_audit`, in the same session, before commit. Alert not found
under the project -> error part `alert <id> not found`.

Schemas in `app/mcp/tools/_schemas.py`:

- `AlertInfo`: id, project_id, event_name, condition, threshold_n,
  counter, is_active, muted_until (iso or null), created_at (iso).
- `AlertDeliveryRow`: id, alert_id (nullable), event_name, condition,
  threshold_n, fired_at (iso), delivered, error.
- `ListAlertsResult`, `AlertHistoryResult`, `DeleteAlertResult`.

Sessions are committed by the tool via `open_session()` + explicit
`await session.commit()` for the three write tools, matching
`rotate_api_key`.

## Errors

Same boundaries as the project tools: no token, unowned project, bad UUID,
schema validation failure, alert not found. All return
`[TextContent(isError=True)]`, never raise.

## Tests

- `tests/mcp/test_alert_tools.py`: for each of the five tools, no-token,
  cross-user (mocked `assert_project_owned_by` raising), happy path with
  mocked services asserting kwargs. `create_alert` with `every_n` and no
  `threshold_n` returns an error part. `delete_alert` happy path asserts
  `write_audit` was called.
- `tests/test_alerts.py` (Postgres-backed OSS suite): `record_delivery`
  writes a row; `list_deliveries` filters by project, since, event_name,
  and orders newest first; `set_alert_active` sets and does not flip.
- `tests/test_e2e.py` or `tests/test_alerts.py`: an ingestion where
  `bot.send_message` raises still leaves one `alert_deliveries` row with
  `delivered=false` and `error` set.
- `tests/mcp/test_tool_annotations.py` and `test_tool_output_schema.py`
  pick the new tools up if they enumerate registered tools; add them to
  any explicit expected list.
- Migration round-trip covered by the existing alembic upgrade/downgrade
  test if present.

## Ship

1. PR against `tgram-analytics/server` `main`.
2. Merge. The cloud image tracks server `main`
   (`tgram-analytics/cloud-deploy` Dockerfile, `SERVER_REF=main`).
3. Redeploy Coolify app `tgram-analytics-cloud` (uuid
   `egck4wowko8s4kckw4ogswoc`). Alembic runs on start.
4. Verify live: call `list_alerts` and `alert_history` over
   `https://tg-analytics.leorigna.com/mcp`; create a test alert, fire an
   event, confirm one history row, delete the alert.
