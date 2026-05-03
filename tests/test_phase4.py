"""Phase 4 — Analytics query engine + aggregation/retention cron tests."""

import uuid
from datetime import UTC, datetime, timedelta

# ── Helpers ───────────────────────────────────────────────────────────────


async def _seed_project(db_session) -> uuid.UUID:
    """Insert a Project row (+ default ProjectSettings) and return its id.

    Also seeds a parent ``users`` row because ``projects.owner_user_id`` is
    NOT NULL after migration 0005.
    """
    from app.core.security import generate_api_key, hash_api_key
    from app.models.project import Project
    from app.models.settings import ProjectSettings
    from app.models.user import User

    # Use a unique telegram_user_id per call so multi-project tests don't
    # collide on the ``telegram_user_id`` UNIQUE constraint.
    user = User(telegram_user_id=int(uuid.uuid4().int % 9_000_000_000) + 1_000_000_000)
    db_session.add(user)
    await db_session.flush()

    p = Project(
        name="analytics-test.com",
        api_key_hash=hash_api_key(generate_api_key()),
        admin_chat_id=999,
        owner_user_id=user.id,
    )
    db_session.add(p)
    await db_session.flush()

    s = ProjectSettings(project_id=p.id, retention_days=90)
    db_session.add(s)
    await db_session.flush()

    return p.id


async def _seed_event(
    db_session, project_id: uuid.UUID, event_name: str, ts: datetime, props: dict | None = None
) -> None:
    from app.services.events import insert_event

    await insert_event(
        db_session,
        project_id=project_id,
        event_name=event_name,
        session_id=str(uuid.uuid4()),
        properties=props or {},
        timestamp=ts,
    )


_now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
_yesterday = _now - timedelta(days=1)
_week_ago = _now - timedelta(days=7)


# ── count_events ──────────────────────────────────────────────────────────


async def test_count_events_correct_count(db_session):
    from app.services.analytics import count_events

    pid = await _seed_project(db_session)
    for _ in range(5):
        await _seed_event(db_session, pid, "click", _now - timedelta(hours=1))

    count = await count_events(
        db_session,
        project_id=pid,
        event_name="click",
        start=_week_ago,
        end=_now + timedelta(hours=1),
    )
    assert count == 5


async def test_count_events_empty_range_returns_zero(db_session):
    from app.services.analytics import count_events

    pid = await _seed_project(db_session)
    count = await count_events(
        db_session,
        project_id=pid,
        event_name="purchase",
        start=_week_ago,
        end=_now,
    )
    assert count == 0


async def test_count_events_does_not_count_other_projects(db_session):
    from app.services.analytics import count_events

    pid1 = await _seed_project(db_session)
    pid2 = await _seed_project(db_session)
    await _seed_event(db_session, pid2, "click", _now - timedelta(hours=1))

    count = await count_events(
        db_session,
        project_id=pid1,
        event_name="click",
        start=_week_ago,
        end=_now + timedelta(hours=1),
    )
    assert count == 0


# ── events_over_time ──────────────────────────────────────────────────────


async def test_events_over_time_daily_buckets(db_session):
    from app.services.analytics import events_over_time

    pid = await _seed_project(db_session)
    day1 = datetime(2024, 6, 10, 10, 0, tzinfo=UTC)
    day2 = datetime(2024, 6, 11, 10, 0, tzinfo=UTC)
    day3 = datetime(2024, 6, 12, 10, 0, tzinfo=UTC)

    for _ in range(3):
        await _seed_event(db_session, pid, "view", day1)
    for _ in range(7):
        await _seed_event(db_session, pid, "view", day2)
    await _seed_event(db_session, pid, "view", day3)

    rows = await events_over_time(
        db_session,
        project_id=pid,
        event_name="view",
        start=day1,
        end=day3 + timedelta(hours=1),
        granularity="day",
    )
    assert len(rows) == 3
    counts = [r["count"] for r in rows]
    assert counts == [3, 7, 1]


async def test_events_over_time_weekly_buckets(db_session):
    from app.services.analytics import events_over_time

    pid = await _seed_project(db_session)
    # Two events in different calendar weeks
    week1 = datetime(2024, 6, 10, 10, 0, tzinfo=UTC)  # Monday
    week2 = datetime(2024, 6, 17, 10, 0, tzinfo=UTC)  # next Monday
    await _seed_event(db_session, pid, "signup", week1)
    await _seed_event(db_session, pid, "signup", week1)
    await _seed_event(db_session, pid, "signup", week2)

    rows = await events_over_time(
        db_session,
        project_id=pid,
        event_name="signup",
        start=week1,
        end=week2 + timedelta(hours=1),
        granularity="week",
    )
    assert len(rows) == 2
    assert rows[0]["count"] == 2
    assert rows[1]["count"] == 1


# ── top_properties ────────────────────────────────────────────────────────


async def test_top_properties_sorted_by_count(db_session):
    from app.services.analytics import top_properties

    pid = await _seed_project(db_session)
    ts = _now - timedelta(hours=1)
    # "pro" appears 3x, "free" 1x, "enterprise" 2x
    for plan, n in [("pro", 3), ("free", 1), ("enterprise", 2)]:
        for _ in range(n):
            await _seed_event(db_session, pid, "signup", ts, {"plan": plan})

    rows = await top_properties(
        db_session,
        project_id=pid,
        event_name="signup",
        property_key="plan",
        start=_week_ago,
        end=_now + timedelta(hours=1),
    )
    assert rows[0]["value"] == "pro"
    assert rows[0]["count"] == 3
    assert rows[1]["value"] == "enterprise"
    assert rows[2]["value"] == "free"


async def test_top_properties_only_counts_events_with_key(db_session):
    from app.services.analytics import top_properties

    pid = await _seed_project(db_session)
    ts = _now - timedelta(hours=1)
    await _seed_event(db_session, pid, "click", ts, {"button": "buy"})
    await _seed_event(db_session, pid, "click", ts, {})  # no "button" key

    rows = await top_properties(
        db_session,
        project_id=pid,
        event_name="click",
        property_key="button",
        start=_week_ago,
        end=_now + timedelta(hours=1),
    )
    assert len(rows) == 1
    assert rows[0]["count"] == 1


# ── compare_periods ───────────────────────────────────────────────────────


async def test_compare_periods_delta_pct_double(db_session):
    from app.services.analytics import compare_periods

    pid = await _seed_project(db_session)
    prev_start = datetime(2024, 6, 1, tzinfo=UTC)
    prev_end = datetime(2024, 6, 8, tzinfo=UTC)
    curr_start = datetime(2024, 6, 8, tzinfo=UTC)
    curr_end = datetime(2024, 6, 15, tzinfo=UTC)

    # 2 events in previous, 4 in current → +100%
    for _ in range(2):
        await _seed_event(db_session, pid, "purchase", prev_start + timedelta(hours=1))
    for _ in range(4):
        await _seed_event(db_session, pid, "purchase", curr_start + timedelta(hours=1))

    result = await compare_periods(
        db_session,
        project_id=pid,
        event_name="purchase",
        current_start=curr_start,
        current_end=curr_end,
        previous_start=prev_start,
        previous_end=prev_end,
    )
    assert result["current"] == 4
    assert result["previous"] == 2
    assert result["delta_pct"] == 100.0


async def test_compare_periods_delta_pct_none_when_previous_zero(db_session):
    from app.services.analytics import compare_periods

    pid = await _seed_project(db_session)
    curr_start = datetime(2024, 6, 8, tzinfo=UTC)
    curr_end = datetime(2024, 6, 15, tzinfo=UTC)
    prev_start = datetime(2024, 6, 1, tzinfo=UTC)
    prev_end = datetime(2024, 6, 8, tzinfo=UTC)

    await _seed_event(db_session, pid, "buy", curr_start + timedelta(hours=1))
    result = await compare_periods(
        db_session,
        project_id=pid,
        event_name="buy",
        current_start=curr_start,
        current_end=curr_end,
        previous_start=prev_start,
        previous_end=prev_end,
    )
    assert result["current"] == 1
    assert result["previous"] == 0
    assert result["delta_pct"] is None


# ── aggregation cron ──────────────────────────────────────────────────────


async def test_aggregation_cron_upserts_counts(db_session):
    from sqlalchemy import select

    from app.models.aggregation import Aggregation
    from app.services.aggregation import run_aggregation_cron

    pid = await _seed_project(db_session)
    now = datetime.now(UTC)
    for _ in range(4):
        await _seed_event(db_session, pid, "click", now - timedelta(minutes=5))

    upserted = await run_aggregation_cron(db_session)
    assert upserted > 0

    rows = await db_session.execute(select(Aggregation).where(Aggregation.project_id == pid))
    agg_list = list(rows.scalars())
    assert any(a.event_name == "click" and a.count == 4 for a in agg_list)


async def test_aggregation_cron_is_idempotent(db_session):
    """Running the cron twice must not create duplicate rows."""
    from sqlalchemy import func, select

    from app.models.aggregation import Aggregation
    from app.services.aggregation import run_aggregation_cron

    pid = await _seed_project(db_session)
    now = datetime.now(UTC)
    await _seed_event(db_session, pid, "view", now - timedelta(minutes=1))

    await run_aggregation_cron(db_session)
    await run_aggregation_cron(db_session)  # second run — must upsert, not insert

    result = await db_session.execute(
        select(func.count()).select_from(Aggregation).where(Aggregation.project_id == pid)
    )
    # 4 period types × 1 event name = 4 rows maximum, not 8
    assert result.scalar_one() <= 4


# ── retention cron ────────────────────────────────────────────────────────


async def test_retention_cron_deletes_old_events(db_session):
    from sqlalchemy import select

    from app.models.event import Event
    from app.models.settings import ProjectSettings
    from app.services.aggregation import run_retention_cron

    pid = await _seed_project(db_session)

    # Overwrite retention_days to 30
    settings = await db_session.get(ProjectSettings, pid)
    settings.retention_days = 30
    await db_session.flush()

    old_ts = datetime.now(UTC) - timedelta(days=31)
    recent_ts = datetime.now(UTC) - timedelta(days=1)
    sid_old = str(uuid.uuid4())
    sid_recent = str(uuid.uuid4())

    await _seed_event(db_session, pid, "old", old_ts)
    # Manually set received_at on the old event
    result = await db_session.execute(select(Event).where(Event.session_id == sid_old))

    # Insert explicitly so received_at matches old_ts
    from app.models.event import Event as Ev

    old_ev = Ev(
        project_id=pid,
        event_name="old",
        session_id=sid_old,
        properties={},
        received_at=old_ts,
        timestamp=old_ts,
    )
    recent_ev = Ev(
        project_id=pid,
        event_name="recent",
        session_id=sid_recent,
        properties={},
        received_at=recent_ts,
        timestamp=recent_ts,
    )
    db_session.add(old_ev)
    db_session.add(recent_ev)
    await db_session.flush()

    deleted = await run_retention_cron(db_session)
    assert deleted >= 1

    result = await db_session.execute(select(Ev).where(Ev.session_id == sid_recent))
    assert result.scalar_one_or_none() is not None  # recent kept


async def test_retention_cron_zero_retention_keeps_all(db_session):
    """retention_days == 0 means keep forever."""

    from app.models.event import Event
    from app.models.settings import ProjectSettings
    from app.services.aggregation import run_retention_cron

    pid = await _seed_project(db_session)
    settings = await db_session.get(ProjectSettings, pid)
    settings.retention_days = 0
    await db_session.flush()

    very_old = datetime.now(UTC) - timedelta(days=3650)
    ev = Event(
        project_id=pid,
        event_name="ancient",
        session_id=str(uuid.uuid4()),
        properties={},
        received_at=very_old,
        timestamp=very_old,
    )
    db_session.add(ev)
    await db_session.flush()

    deleted = await run_retention_cron(db_session)
    assert deleted == 0
