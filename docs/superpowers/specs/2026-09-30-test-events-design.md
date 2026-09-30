# Test events — design

Date: 2026-09-30
Status: draft, awaiting review

## Goal

Let a client mark events as **test** (internal use, development builds, mock or
seed data, CI). Test events are accepted and stored like any other event, but
they do not count in analytics: charts, reports, digests, funnels, rollups,
comparisons, exports and alerts ignore them. Debug views still show them, so a
developer can confirm that a new integration works.

## Decisions

| Topic | Decision |
|---|---|
| How an event is marked | Payload flag `test: true`, plus automatic marking of localhost origins |
| Where test events are visible | Debug views only: `recent_events`, `verify_integration`, `/doctor` |
| Alerts | Never fire on a test event |
| Storage | `is_test` column on `events`, one shared read filter, guard test |

Rejected alternatives:

- **Separate `test_events` table.** No leak risk, but ingestion, retention,
  export and debug views would handle two tables forever.
- **Postgres view `real_events`.** One filter location, but a second mapped
  class and different names for reads and writes.
- **Separate test API key.** No SDK change, but adds key management to the bot
  and rotation flows.

## Out of scope

- `include_test` toggle on charts, reports or MCP query tools.
- IP exclusion lists and an "exclude my visits" cookie.
- Bulk delete of test data. The retention job already deletes test rows with
  real rows.
- Accepting localhost when a project's `domain_allowlist` rejects it. Such
  requests keep returning 403, as today.

## Data model

Migration `0015_events_is_test` (main is at `0014_taps`):

```sql
ALTER TABLE events ADD COLUMN is_test boolean NOT NULL DEFAULT false;
```

Postgres 11+ adds a column with a constant default as a metadata-only change,
so the table is not rewritten.

(The `taps` table does not get the column. See "Taps" below.) No new index: the existing
`(project_id, event_name, timestamp)` index still drives every query, and the
extra predicate is evaluated on the matched rows.

`Event.is_test: Mapped[bool]`, `server_default=sa.false()`, `nullable=False`.

## Ingestion

`TrackEventRequest` and `PageviewRequest` (`app/schemas/event.py`) get:

```python
test: bool = False
```

`app/api/ingestion.py` computes `is_test` and passes it to `insert_event`:

```python
is_test = body.test or is_local_host(origin) or is_local_host(page_url)
```

- `origin` is the `Origin` request header.
- `page_url` is `body.url` for pageviews, `None` for track calls.
- `is_local_host()` lives in `app/services/events.py`, next to
  `is_origin_allowed`. It parses the host and returns `True` for
  `localhost`, `*.localhost`, `127.0.0.0/8`, `::1` and `0.0.0.0`. It returns
  `False` for `None` or an unparsable value.

The flag is not stored in `properties`. `EventResponse` gets `is_test: bool`.

When `is_test` is true, the ingestion handler does not schedule
`_run_alert_evaluation`.

## Read side

### Shared filter

`app/models/event.py`:

```python
REAL_EVENTS = Event.is_test.is_(False)
```

Raw SQL strings use `AND NOT is_test`.

### Queries that exclude test events

| File | Queries |
|---|---|
| `app/services/analytics.py` | `count_events`, `events_over_time`, `top_properties`, `top_array_elements` (raw SQL), `find_array_property_keys` (raw SQL), `list_event_names`, `list_property_keys`, `compare_periods` |
| `app/services/aggregation.py` | hourly rollup `select` |
| `app/services/events.py` | `evaluate_alerts` threshold count |
| `app/services/funnels.py` | both step queries |
| `app/bot/handlers/reports.py` | all 4 queries |
| `app/bot/handlers/digest.py` | all 3 queries |
| `app/bot/handlers/export.py` | the export query |

MCP tools `query_events`, `top_pages`, `top_property_values`, `top_taps`,
`compare_periods`, `list_event_names` and `list_property_keys` go through
`analytics.py`, so they inherit the filter. The implementation plan confirms
each one by test.

### Queries that include test events

| Surface | Behaviour |
|---|---|
| `list_recent_events` (bot) | Test rows are prefixed with 🧪 |
| MCP `recent_events` | Each row gets `"is_test": true/false` |
| MCP `verify_integration` | Counts all events. Response adds `test_count: int` |
| `/doctor` (`app/bot/handlers/doctor.py`) | Counts all events, reports how many are test |

Each of these call sites gets a comment: `# includes test events on purpose`.

### Not filtered

The retention `delete(Event)` in `aggregation.py` is not filtered. It must delete test rows too.

### Guard test

`tests/test_real_events_guard.py` scans `app/**/*.py`. For every line that
starts an events query (`select(Event`, `select(` followed by `Event.` columns,
`FROM events`), the enclosing statement must contain `REAL_EVENTS`,
`NOT is_test`, or the marker comment `# includes test events on purpose`.
A `delete(Event)` is exempt. New queries that forget the filter fail CI.

## Taps (heatmaps)

`POST /api/v1/taps` (`TapsRequest`) gets the same `test: bool = False` field
and the same localhost rule. A test taps request returns
`{"status": "accepted"}` and stores nothing, the same way the endpoint already
treats bot traffic. Taps have no debug view, so a column would add storage
with no reader.

## SDKs

Each SDK is a separate, small PR and release. The server does not depend on
them: raw JSON clients can send `"test": true` from day one, and web
development on localhost is covered by the automatic rule.

| SDK | API |
|---|---|
| JS (`tgram-analytics-js`) | `TGA.init(key, { test: true })` adds `test: true` to every request |
| Flutter (`tgram-analytics-flutter`) | `TGA.init(key, test: kDebugMode)` |
| Python (`tgram-analytics-py`) | `TGA(api_key, test=True)` |

## Documentation

The docs describe the feature in plain, neutral terms. Every page below is a
place a user or an AI agent reads.

| Location | Change |
|---|---|
| `README.md` → "Track events (REST API)" | Add `test` to the request body example and field list. Add a short "Test events" subsection: what it does, the localhost rule, where test events still show. |
| `README.md` → "JavaScript SDK" and "Flutter SDK" | One line each with the `test` option |
| `openapi.json` | Regenerate so `test` appears on both request schemas and `is_test` on the response |
| Site `docs.html` (repo `tgram-analytics`) | New `<h3 id="test-events">` under "SDK Setup", with a snippet per SDK and the localhost rule. Link to it from the Quick Start. |
| Site `llms.txt` | One line under "Event tracking": test events are stored but excluded from analytics |
| SDK READMEs (js, flutter, py) | "Test mode" section in each. These files are also served to AI agents by the MCP docs federation (`app/mcp/docs/sources.py`), so `get_integration_guide` picks the change up with no server change. |
| MCP tool docstrings | `recent_events` and `verify_integration` docstrings mention that they include test events. Analytics tool docstrings mention that they exclude them. |

Example text for the README subsection:

> **Test events.** Send `"test": true` with any event to mark it as test data.
> Test events are stored, but reports, digests, funnels, exports, alerts and
> the MCP analytics tools ignore them. `recent_events`, `verify_integration`
> and `/doctor` still show them, marked 🧪, so you can check an integration.
> Events from `localhost`, `127.0.0.1` and `::1` are marked as test
> automatically.

## Testing

- Ingestion: `test: true` stores `is_test = true`. Default stores `false`.
- Taps: a test or localhost taps request stores no rows and returns 202.
- Localhost rule: one case per host variant in `Origin`, one in pageview `url`,
  and negative cases (`localhost.example.com`, `127.example.com`, missing
  header).
- Alerts: a test event does not schedule alert evaluation, and test rows do
  not count toward a threshold.
- One test per excluded read path: seed 1 real and 1 test event, assert only
  the real one counts.
- `recent_events` returns `is_test`. `verify_integration` returns
  `test_count`.
- Retention deletes old test rows.
- Guard test, plus a self-check that it fails on a sample unfiltered query.

## Rollout

1. Server PR: migration, ingestion, read paths, guard test, README,
   `openapi.json`, docstrings. Deploy and verify on the live instance: send one
   test and one real event, confirm only the real one appears in a report and
   both appear in `recent_events`.
2. Site PR in `tgram-analytics`: `docs.html` and `llms.txt`.
3. SDK PRs, one per repo, each with its README section.
