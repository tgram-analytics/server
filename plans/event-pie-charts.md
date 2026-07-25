# Plan: Specific Pie Chart + Full Pie Charts for events

**Goal.** In the bot event-detail view, replace the single "🥧 Pie chart" button with two buttons:

1. **🥧 Specific Pie Chart (current)** — preserves today's flow: pick one property, get one pie chart.
2. **🥧📊 Full Pie Charts** — new flow: enumerate every property of the event and send one pie chart photo per property.

All work happens in repo `server/` (`https://github.com/tgram-analytics/server`). The current worktree is the **website** repo and is the wrong place to implement; spin up a new server-repo worktree (see Phase 0).

---

## Phase 0 — Documentation Discovery (consolidated)

**Status:** complete. Findings below are sourced from a Phase 0 sweep over the server codebase. No further discovery work needed before Phase 1.

### Allowed APIs (confirmed to exist)

- `app.services.analytics.list_property_keys(session, *, project_id, event_name, start, end) -> list[str]`
  - source: `server/app/services/analytics.py:195`
  - already used by `_show_pie_property_picker` in `events.py`
- `app.services.analytics.top_properties(session, *, project_id, event_name, property_key, start, end, limit) -> list[{"value": ..., "count": ...}]`
  - source: `server/app/services/analytics.py:122`
  - already used by `_send_event_pie_chart`
- `app.services.charts.generate_pie_chart(pie_data, *, title) -> bytes`
  - source: `server/app/services/charts.py:342`
  - `pie_data` shape: `[{"source": <label>, "count": <int>}, ...]`
  - raises `ChartGenerationError` (`server/app/services/charts.py:22`)
- `telegram.Message.reply_photo(photo=..., caption=..., reply_markup=...)` — already in use throughout events.py.
- `python-telegram-bot==21.5` is installed; `Message.reply_media_group` and `Bot.send_media_group` both exist.

### Anti-patterns to avoid

- **DO NOT invent** a `top_properties_all(...)` or similar bulk helper — none exists. The plan loops over keys client-side and calls `top_properties` once per key.
- **DO NOT use `reply_media_group`** for the "Full Pie Charts" output:
  - Media groups cap at 10 items per group. Some events may exceed that.
  - Items in a media group cannot have individual inline keyboards (only the first item's caption is shown, no `reply_markup` per item).
  - Loop with `reply_photo` instead, exactly like `_send_event_pie_chart` already does.
- **DO NOT change the `evta:pie` / `evta:pie_k:*` callback prefixes or `_show_pie_property_picker` / `_send_event_pie_chart` bodies.** The "Specific Pie Chart" flow is unchanged — only the button *label* moves.
- **DO NOT** fire all chart generations in parallel (no `asyncio.gather`). Serialize them: this avoids hammering QuickChart and keeps chat order deterministic.
- **DO NOT** depend on `BotStateService.get()` returning a state with `flow=="events"` after the loop — by the end the user may have navigated. Cache `project`, `pid`, `event_name` in locals before the loop.

### File map

| Concern                          | File                                                  |
| -------------------------------- | ----------------------------------------------------- |
| Event detail UI + callbacks      | `server/app/bot/handlers/events.py`                    |
| Pie/line chart generation        | `server/app/services/charts.py` (no edits needed)     |
| Property enumeration + counts    | `server/app/services/analytics.py` (no edits needed)  |
| Bot test fixtures + conftest     | `server/tests/conftest.py`, `server/tests/fixtures/`   |
| Existing chart-photo test style  | `server/tests/test_reports.py`                         |

### Worktree setup (do this before Phase 1)

```bash
cd /Users/leonardo/Progetti/telegram-analytics/server
git fetch origin
git worktree add -b feat/event-full-pie-charts ../server-event-pie-charts origin/main
cd ../server-event-pie-charts
```

All later phases assume cwd is `server-event-pie-charts/`.

---

## Phase 1 — Wire the UI shell (rename current button + add new button + dispatcher case)

**Scope.** Pure-UI shell change. The new button calls a stub that just answers a Telegram alert; the real implementation lands in Phase 2.

### What to implement

1. Locate `_show_event_detail` in `app/bot/handlers/events.py` (declared at line ~348 per Phase 0 grep). Find the keyboard row containing the existing pie button (`callback_data="evta:pie"`).
2. **Rename** its label from whatever it is today (e.g. `"🥧 Pie chart"`) to `"🥧 Specific Pie Chart"`. Keep `callback_data="evta:pie"` **unchanged** — only the visible label moves. (The "(current)" wording in the user's request is for the descriptor in this plan, not the button label; keeping the label short avoids button-text truncation.)
3. **Add** a new button immediately below it:
   ```python
   InlineKeyboardButton("🥧📊 Full Pie Charts", callback_data="evta:pie_all")
   ```
   Put it on its own row, directly under the Specific Pie Chart button, above any `« Back to Events` row.
4. In `events_callback` (the dispatcher block around lines 130–175), add a new branch after the `evta:pie_k:` branch:
   ```python
   elif data == "evta:pie_all":
       await _send_full_pie_charts(query, owner_user_id)
   ```
5. Add a stub function at the bottom of the "── Pie chart ──" section:
   ```python
   async def _send_full_pie_charts(query: CallbackQuery, owner_user_id: uuid.UUID) -> None:
       """Generate one pie chart per property for the current event."""
       await query.answer("Full Pie Charts coming online…", show_alert=False)
   ```
   This stub lets Phase 1 ship/test independently without touching analytics or charts code.

### Documentation references

- Existing button row pattern: `events.py` `_show_pie_property_picker` builds rows the same way.
- Callback prefix convention `evta:*`: existing branches in `events_callback`.

### Verification checklist

```bash
cd /Users/leonardo/Progetti/telegram-analytics/server-event-pie-charts

# 1. Grep — the label change and the new callback case both exist.
grep -n 'Specific Pie Chart' app/bot/handlers/events.py
grep -n 'Full Pie Charts'    app/bot/handlers/events.py
grep -n '"evta:pie_all"'     app/bot/handlers/events.py

# 2. Confirm the original "evta:pie" callback path is untouched.
grep -n '"evta:pie"\|evta:pie_k' app/bot/handlers/events.py

# 3. Existing tests still pass.
pytest tests/test_events*.py tests/test_reports.py -q
```

### Anti-pattern guards

- ❌ Do not rename the **callback_data** of the existing button — only the label.
- ❌ Do not collapse the two buttons into one row; long emoji labels truncate on narrow Telegram clients.
- ❌ Do not implement the body yet — Phase 1 only ships the shell.

---

## Phase 2 — Implement `_send_full_pie_charts`

**Scope.** Replace the Phase 1 stub with the real fan-out: for each property key of the current event, generate and reply with a pie chart photo.

### What to implement

Replace the stub from Phase 1 with a real implementation. Model it directly on `_send_event_pie_chart` (the existing single-property handler) — copy its state-loading prologue verbatim, then loop over `list_property_keys(...)` instead of taking a `property_key` parameter.

Algorithm:

1. `await query.answer()` (silently ack the click — the existing dispatcher already does this at the top, so this call is redundant; **omit it**).
2. Load `BotStateService` state. If state is invalid (`flow != "events"` or `step != "detail"`), edit the message to "❌ Session expired. Use /events to start again." and return — copy this prologue verbatim from `_send_event_pie_chart`.
3. Extract `project_id_str`, `event_name` from `state.payload`. Guard for missing values the same way `_send_event_pie_chart` does.
4. `pid = uuid.UUID(project_id_str)`. Resolve `project = await get_project(session, pid, owner_user_id)`. Bail with "❌ Project not found." if `None`.
5. `now = datetime.now(UTC)`; `start = now - timedelta(days=30)`; `end = now`. (Same 30-day window the existing pie flow uses.)
6. `keys = await list_property_keys(session, project_id=pid, event_name=event_name, start=start, end=end)`.
7. If `not keys`: `await query.answer(f"No properties found for {event_name}.", show_alert=True)` and return.
8. Edit the original message to a status line and pin a Back keyboard, e.g.:
   ```python
   await query.edit_message_text(
       f"🥧📊 <b>{html.escape(event_name)}</b> · sending {len(keys)} chart(s)…",
       parse_mode="HTML",
       reply_markup=InlineKeyboardMarkup(
           [[InlineKeyboardButton("« Back to Events", callback_data="back:events")]]
       ),
   )
   ```
9. Close the session **before** the loop (so the loop body doesn't hold an open DB session while talking to QuickChart over HTTP). Re-open a short session per `top_properties` call inside the loop. *Rationale:* charts take seconds; holding a transaction open across them is wasteful.
10. For each `key` in `keys`:
    - `rows = await top_properties(session, project_id=pid, event_name=event_name, property_key=key, start=start, end=end, limit=10)` inside a new session.
    - If `not rows`: skip (don't post an empty chart, don't post a "no data" notice — just move on; the next chart is more useful than 10 "no data" lines).
    - `pie_data = [{"source": r["value"], "count": r["count"]} for r in rows]` (same reshape the existing handler does).
    - `try: png = await generate_pie_chart(pie_data, title=f"{event_name} · {key}")` / `except ChartGenerationError: continue` (skip this key, don't abort the whole batch).
    - `await query.message.reply_photo(photo=png, caption=f"🥧 {project.name} · {event_name} · {key}")`.
    - **Do not** attach a `reply_markup` to every photo — the status message above already has the Back button. Only attach a `reply_markup` to the *last* successfully-sent photo so the user has a Back affordance below the final chart.
11. After the loop, if zero photos were sent (every key had empty data or chart-service errors), edit the status message to `"⚠️ No charts could be generated for {event_name}."` so the user isn't left staring at "sending… "

### Documentation references

- Copy state-loading prologue from `events.py` `_send_event_pie_chart` (lines ~ in the "── Pie chart ──" section).
- Copy data reshape (`{"value": ...} → {"source": ...}`) from same function.
- Copy back-keyboard shape from `_send_event_pie_chart`.

### Verification checklist

```bash
# 1. Function exists and is non-stub.
grep -n 'async def _send_full_pie_charts' app/bot/handlers/events.py
grep -c 'list_property_keys\|top_properties\|generate_pie_chart' app/bot/handlers/events.py
# Expect the count to go up by exactly the new references in _send_full_pie_charts
# (1 list_property_keys, 1 top_properties, 1 generate_pie_chart inside the new fn).

# 2. No asyncio.gather over chart generation (anti-pattern guard).
! grep -n 'asyncio\.gather' app/bot/handlers/events.py

# 3. Existing tests still pass.
pytest tests/test_events*.py tests/test_reports.py -q

# 4. Type-check (project uses ruff; if mypy is configured, run it).
ruff check app/bot/handlers/events.py
```

### Anti-pattern guards

- ❌ Do not call `generate_pie_chart` in parallel with `asyncio.gather` — serialize.
- ❌ Do not raise on `ChartGenerationError` for a single key; that aborts the whole batch. `continue` past it.
- ❌ Do not invent helper signatures — only the three documented analytics/charts APIs above are allowed.
- ❌ Do not hold a single `AsyncSession` open across all chart generations — open a fresh session per analytics call inside the loop (or, simpler, do all DB reads up front, close the session, then iterate over chart generation + photo replies).

---

## Phase 3 — Tests

**Scope.** Cover the new dispatcher branch and the fan-out behavior. The repo has no `test_events.py` today — create a new `tests/test_events_pie.py` (verified via `find tests -name 'test_event*'` returning nothing in Phase 0).

### What to implement

Three tests, all using the existing bot-handler test patterns in `tests/test_reports.py` and `tests/test_phaseN.py`:

1. **`test_full_pie_charts_button_present_in_event_detail`** — render the event-detail keyboard via a fixture project + event, assert both labels `"🥧 Specific Pie Chart"` and `"🥧📊 Full Pie Charts"` appear, and that the new button's `callback_data == "evta:pie_all"`.

2. **`test_full_pie_charts_sends_one_photo_per_property`** — seed two events `purchase` (with properties `plan`, `currency`) via `insert_event` (existing helper). Mock or use real `quickchart` if the test suite already does. Fire the `evta:pie_all` callback through `events_callback` with a mocked `query` whose `message.reply_photo` is an `AsyncMock`. Assert `reply_photo.call_count == 2`. Assert each `caption` contains one of `"plan"`, `"currency"`.

3. **`test_full_pie_charts_skips_keys_with_no_data_and_chart_failures`** — seed an event with one property whose values are all `None` (so `top_properties` returns nothing). Patch `generate_pie_chart` to raise `ChartGenerationError` for one specific key. Assert no exception bubbles, and only the surviving key produces a `reply_photo` call.

Look at `tests/test_reports.py` for the canonical chart-handler test pattern (it already mocks `generate_line_chart` and asserts `reply_photo` calls); the new file should mirror it line-for-line.

### Verification checklist

```bash
pytest tests/test_events_pie.py -v
pytest tests/ -q   # full suite, ensure no regressions
```

### Anti-pattern guards

- ❌ Do not skip the "skips on failure" test — silent failure under partial-data is the riskiest behavior; it must be locked in.
- ❌ Do not hit the real QuickChart service from tests. Patch `app.bot.handlers.events.generate_pie_chart` (the module-local import), exactly like `test_reports.py` patches its line-chart import.

---

## Phase 4 — Final verification

```bash
cd /Users/leonardo/Progetti/telegram-analytics/server-event-pie-charts

# Full test suite green.
pytest -q

# Lint + format clean.
ruff check .
ruff format --check .

# Manual smoke (optional but recommended):
# 1. boot the bot locally against a dev DB with seeded events
# 2. /events → tap a project → tap an event
# 3. confirm the two new buttons appear with correct labels
# 4. tap "Specific Pie Chart" → confirm property picker still works (unchanged flow)
# 5. tap "Full Pie Charts" → confirm one photo arrives per property

# Commit + push + open PR.
git add -A
git commit -m "feat(bot): add Full Pie Charts button to event detail"
git push -u origin feat/event-full-pie-charts
gh pr create --title "feat(bot): Full Pie Charts (one chart per property)" \
  --body "Adds 🥧📊 Full Pie Charts alongside renamed 🥧 Specific Pie Chart on the event detail screen. Fans out one pie chart photo per property of the selected event. Skips empty/erroring keys gracefully."
```

### Acceptance criteria

- [ ] Event detail shows **🥧 Specific Pie Chart** and **🥧📊 Full Pie Charts** as two separate buttons.
- [ ] Tapping **🥧 Specific Pie Chart** behaves *identically* to today's pie flow (property picker → one chart).
- [ ] Tapping **🥧📊 Full Pie Charts** sends one photo per property of the event, in series.
- [ ] Properties with no data are silently skipped (no spam).
- [ ] A single failed chart does not abort the whole batch.
- [ ] All three new tests pass; full pytest suite is green; ruff is clean.
