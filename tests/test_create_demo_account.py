"""Tests for ``scripts/create_demo_account.py``.

Runs the script's core function against the test DB (rolled back per test)
and checks idempotency, ownership, and that the data backs every
read-only MCP tool with non-empty results.

Requires a live PostgreSQL DB (same convention as test_phase1).
"""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# 8 days covers the 7d window plus the previous 7d that compare_periods reads,
# and keeps the suite fast. The script itself defaults to 30 days.
_DAYS = 8
_SCRIPT = Path(__file__).parent.parent / "scripts" / "create_demo_account.py"


@pytest.fixture(scope="module")
def demo() -> ModuleType:
    spec = importlib.util.spec_from_file_location("create_demo_account", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def _count(session: AsyncSession, model, *where) -> int:  # type: ignore[no-untyped-def]
    stmt = select(func.count()).select_from(model).where(*where)
    return (await session.execute(stmt)).scalar_one()


def test_demo_telegram_id_cannot_be_a_real_account(demo: ModuleType) -> None:
    # Real Telegram user ids have at most 52 significant bits; chat ids of
    # groups/channels are negative. The value must also fit a signed BIGINT.
    assert 2**52 < demo.DEMO_TELEGRAM_USER_ID < 2**63


async def test_demo_account_is_idempotent(demo: ModuleType, db_session: AsyncSession) -> None:
    from app.models import Alert, AlertDelivery, Event, MCPToken, Project, User

    first = await demo.ensure_demo_account(db_session, days=_DAYS)
    assert first.created_user and first.created_project
    assert first.events_added > 100
    assert first.alerts_created == 2
    assert first.raw_token is not None and first.raw_token.startswith("mcp_")

    events_after_first = await _count(db_session, Event, Event.project_id == first.project_id)
    history_after_first = await _count(
        db_session, AlertDelivery, AlertDelivery.project_id == first.project_id
    )
    assert events_after_first == first.events_added

    second = await demo.ensure_demo_account(db_session, days=_DAYS)
    assert (second.user_id, second.project_id) == (first.user_id, first.project_id)
    assert not second.created_user and not second.created_project
    assert second.events_added == 0
    assert second.alerts_created == 0
    assert second.raw_token is None

    tg_id = demo.DEMO_TELEGRAM_USER_ID
    assert await _count(db_session, User, User.telegram_user_id == tg_id) == 1
    assert await _count(db_session, Event, Event.project_id == first.project_id) == (
        events_after_first
    )
    assert await _count(db_session, Alert, Alert.project_id == first.project_id) == 2
    assert (
        await _count(db_session, AlertDelivery, AlertDelivery.project_id == first.project_id)
        == history_after_first
    )

    # The demo user owns exactly the demo project and nothing else.
    owned = (
        (await db_session.execute(select(Project).where(Project.owner_user_id == first.user_id)))
        .scalars()
        .all()
    )
    assert [p.id for p in owned] == [first.project_id]
    assert owned[0].name == demo.DEMO_PROJECT_NAME
    assert owned[0].domain_allowlist == [demo.DEMO_DOMAIN]
    assert owned[0].admin_chat_id == tg_id

    third = await demo.ensure_demo_account(db_session, new_token=True, days=_DAYS)
    assert third.raw_token is not None and third.raw_token != first.raw_token
    assert await _count(db_session, MCPToken, MCPToken.user_id == first.user_id) == 2
    assert await _count(db_session, User, User.telegram_user_id == tg_id) == 1


async def test_rerun_later_tops_up_without_duplicates(
    demo: ModuleType, db_session: AsyncSession
) -> None:
    from app.models import Event

    start = datetime.now(UTC) - timedelta(days=1)
    first = await demo.ensure_demo_account(db_session, now=start, days=_DAYS)
    later = await demo.ensure_demo_account(db_session, now=start + timedelta(hours=6), days=_DAYS)
    assert later.events_added > 0

    # Every event is newer than the first run's newest event: no overlap.
    rows = (
        await db_session.execute(
            select(Event.timestamp).where(Event.project_id == first.project_id)
        )
    ).scalars()
    timestamps = sorted(rows)
    assert len(timestamps) == first.events_added + later.events_added
    assert timestamps[-1] <= start + timedelta(hours=6)


async def test_demo_data_backs_every_read_only_tool(
    demo: ModuleType, db_session: AsyncSession
) -> None:
    from app.mcp.tools._periods import period_to_window, previous_window
    from app.services import analytics
    from app.services.alerts import list_alerts, list_deliveries
    from app.services.projects import get_project, list_projects

    result = await demo.ensure_demo_account(db_session, days=_DAYS)
    pid, uid = result.project_id, result.user_id
    start, end = period_to_window("7d")

    assert [p.id for p in await list_projects(db_session, uid)] == [pid]
    assert await get_project(db_session, pid, uid) is not None

    names = {r["event_name"] for r in await analytics.list_event_names(db_session, project_id=pid)}
    assert {"pageview", "signup", "add_to_cart", "purchase", "error"} <= names

    assert (
        await analytics.count_events(
            db_session, project_id=pid, event_name="pageview", start=start, end=end
        )
        > 0
    )
    prev_start, prev_end = previous_window(start, end)
    compared = await analytics.compare_periods(
        db_session,
        project_id=pid,
        event_name="pageview",
        current_start=start,
        current_end=end,
        previous_start=prev_start,
        previous_end=prev_end,
    )
    assert compared["current"] > 0 and compared["previous"] > 0

    pages = await analytics.top_properties(
        db_session,
        project_id=pid,
        event_name="pageview",
        property_key="url",
        start=start,
        end=end,
        limit=10,
    )
    assert len(pages) >= 3

    month_start, _ = period_to_window("30d")
    amounts = await analytics.top_properties(
        db_session,
        project_id=pid,
        event_name="purchase",
        property_key="amount",
        start=month_start,
        end=end,
        limit=10,
    )
    assert amounts
    keys = await analytics.list_property_keys(
        db_session, project_id=pid, event_name="purchase", start=month_start, end=end
    )
    assert "amount" in keys

    recent = await analytics.list_recent_events(db_session, project_id=pid, limit=1000)
    assert recent
    # verify_integration's default window is the last 30 minutes.
    live_cutoff = datetime.now(UTC) - timedelta(minutes=30)
    assert any(r["timestamp"] >= live_cutoff for r in recent)

    alerts = await list_alerts(db_session, pid)
    assert {a.event_name for a in alerts} == {"signup", "error"}
    assert {a.event_name: a.is_active for a in alerts} == {"signup": True, "error": False}
    assert await list_deliveries(db_session, pid, since=month_start, limit=50)


async def test_future_dated_event_does_not_block_top_up(
    demo: ModuleType, db_session: AsyncSession
) -> None:
    from app.services.events import insert_event

    start = datetime.now(UTC) - timedelta(days=1)
    first = await demo.ensure_demo_account(db_session, now=start, days=_DAYS)
    await insert_event(
        db_session,
        project_id=first.project_id,
        event_name="pageview",
        session_id="future",
        properties={},
        timestamp=start + timedelta(days=365),
    )
    later = await demo.ensure_demo_account(db_session, now=start + timedelta(hours=6), days=_DAYS)
    assert later.events_added > 0
