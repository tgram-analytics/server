# Weekly digest redesign

**Date:** 2026-05-08
**File touched:** `server/app/bot/handlers/digest.py`

## Goal

Give the user a panoramic of how each project is doing using three signals only:

1. **Visits** (unique sessions, last 7 days)
2. **Core events** — events with an active alert configured (user-curated "what matters")
3. **Growth vs prior 7 days** — on sessions and on each core event

Total event counts and the "Top: pageview, …" line are removed. Pageview noise disappears unless the user has explicitly alerted on `pageview`.

## Output format

```
📰 Weekly digest
1 May – 8 May  ·  7 projects
─────────────────
📦 nomadgroups.wiki
  👤 Sessions: 76  ▼ -8.4%
  🎯 checkout_started: 27  ▲ +12.5%
  🎯 dialog_opened: 41  ▼ -5.2%

📦 onda.pics
  👤 Sessions: 38  ▲ +72.7%
  💤 No alerts — set one with /alerts to track core events

📦 scova.events
  👤 Sessions: 159  🆕
  🎯 search: 14  🆕
  🎯 onboarding-step-view: 11  🆕
```

Header (`📰 Weekly digest`, period, project count, divider) is unchanged from current.

## Rules

- **Sessions line:** unchanged. Unique `session_id` for `[now-7d, now)`, WoW delta vs `[now-14d, now-7d)`. Uses existing `_format_delta` helper.
- **Core events:** `Alert` rows for the project where `is_active = true`. Muted alerts (`muted_until` set) still count — muting is a notification preference, not a deselection signal.
- **One line per alerted event,** ordered by **current count desc**, ties broken by `event_name` ascending for stable output.
- **Zero-count alerts are rendered.** If `signups` had 5 last week and 0 this week, line reads `🎯 signups: 0  ▼ -100%`. Disappearance is signal.
- **No alerts configured** (zero rows in `alerts` for the project, or all rows `is_active = false`): single nudge line `💤 No alerts — set one with /alerts to track core events`.
- **Empty projects are still rendered.** Goal is panoramic; skipping projects breaks that.
- **No top-events line, no total-events line.**

## Data access

Replace the current per-project queries (total_curr, total_prev, sessions_curr, sessions_prev, top_events) with:

1. **Sessions current + prior** — keep current two queries.
2. **Active alerts for project** — `SELECT event_name FROM alerts WHERE project_id = :pid AND is_active = true`.
3. **Per-event counts (current + prior in one query)** — only if step 2 returned rows:

   ```sql
   SELECT
     event_name,
     COUNT(*) FILTER (WHERE timestamp >= :week_ago) AS curr,
     COUNT(*) FILTER (WHERE timestamp >= :two_weeks_ago AND timestamp < :week_ago) AS prev
   FROM events
   WHERE project_id = :pid
     AND event_name = ANY(:alerted_names)
     AND timestamp >= :two_weeks_ago
   GROUP BY event_name
   ```

   Left-joined in Python against the alert list so events with zero rows in both windows still get a `0 / 0` entry (rendered as `0  —`).

Net query count per project: 4 (down from 5), regardless of alert count.

## Edge cases

| Case | Behavior |
|---|---|
| Alert exists but `event_name` never recorded | Line: `🎯 <name>: 0  —` |
| Alert with current=0, prior>0 | `🎯 <name>: 0  ▼ -100%` |
| Alert with current>0, prior=0 | `🎯 <name>: N  🆕` (existing `_format_delta` handles this) |
| Same `event_name` configured in multiple alerts (different conditions) | Deduplicate `event_name`s before the count query; one line per name |
| Alert is `is_active = false` | Treated as no alert |
| Project has alerts but zero sessions and zero alerted events | Render full block; alert lines all show 0 |
| User has no projects | Existing 📭 message, unchanged |

## Code-level changes

In `server/app/bot/handlers/digest.py`:

- `_project_digest_lines` rewritten:
  - Drop `total_curr`, `total_prev`, `top_events` queries.
  - Add the alerts query and the FILTER-aggregated counts query.
  - Build lines: header, sessions, then either alert lines or the no-alerts nudge.
- `_format_delta` reused as-is.
- New import: `from app.models.alert import Alert`.
- HTML escaping: alert `event_name` already needs `html.escape()` — pattern matches existing top-events code.

No schema changes. No new migration. No public API change.

## Tests

Add cases to `server/tests/` covering:

1. Project with one active alert, events in both windows → renders sessions + one 🎯 line with delta.
2. Project with active alert but zero events in current window, nonzero prior → `0  ▼ -100%`.
3. Project with active alert, zero in both windows → `0  —`.
4. Project with no alerts → nudge line, no 🎯 lines.
5. Project with `is_active = false` alert only → treated as no alerts.
6. Project with multiple alerts on the same `event_name` (different conditions) → single line.
7. Muted alert (`muted_until` in future) still appears.
8. Ordering: two alerts, current counts 5 and 27 → 27 listed first.
9. Multi-project digest still renders all projects including ones with zero sessions.

Existing digest tests that assert the old "Top:" or total-events format must be updated or removed.

## Out of scope

- Surfacing funnels as core events (could be a future iteration).
- Alert-trigger counts (e.g. "signups crossed threshold 3 times this week"). Counts here are raw event counts, not alert firings.
- Configurable digest period (still 7 days).
- Email / scheduled digest delivery (this is the on-demand `/digest` command only).
