#!/usr/bin/env python3
"""Create (or refresh) a self-contained demo account for trying the MCP server.

Run it once inside the server container, from the repo root::

    python scripts/create_demo_account.py              # create / refresh
    python scripts/create_demo_account.py --new-token  # also mint another token

What it does, using the app's own settings, DB session, models and services:

1. Creates a dedicated demo ``User`` keyed by ``DEMO_TELEGRAM_USER_ID``
   (see below for why that id can never belong to a real Telegram account).
2. Creates the project "Demo Shop" (allowlist ``demo.example.com``) owned by
   that user, with a low ingestion rate limit.
3. Seeds ~30 days of sample events (pageviews, ``signup``, ``add_to_cart``,
   ``purchase`` with an ``amount`` property, ``error``) through the same
   privacy path ingestion uses: ``scrub_properties`` -> ``hash_visitor`` /
   ``parse_user_agent`` -> ``insert_event``. Visitors use documentation-only
   IP ranges (RFC 5737), so no real address is ever hashed.
4. Creates two alerts and a short sample alert history.
5. Mints a static MCP token via ``create_token`` and prints the raw value
   once, together with the MCP URL.

Rerunning is safe: the user and project are reused, events are only added
for the time elapsed since the newest demo event (no duplicates), alerts
are not recreated, and no new token is minted unless ``--new-token`` is
passed. Rerunning shortly before someone uses the account keeps the data
current (``verify_integration`` looks at the last 30 minutes by default).

Telegram messages: the demo project's ``admin_chat_id`` is the fake id.
The demo token can rotate the project API key (``rotate_api_key``) and
create alerts, so alerts can fire from ingestion. Notification code skips
the Telegram call for ids at or above 2**52
(``app.core.telegram_ids.is_unreachable_chat_id``): an alert then records
a ``delivered=false`` history row with error ``no_chat``, and a
project-create request sends no message. The "error" alert is stored
paused to show both alert states. An OAuth token exchange with the demo
token sends the usual "New MCP client authorized" message to the admin
chat (``ADMIN_CHAT_ID``), as for any other token.

Plugins are NOT loaded, so pre-create hooks from extensions do not run
for the demo project.

To remove everything the script created (cascades to project, events,
alerts, history and tokens)::

    DELETE FROM users WHERE telegram_user_id = 9000000000000000000;
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Allow ``python scripts/create_demo_account.py`` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.privacy import hash_visitor, parse_user_agent, scrub_properties  # noqa: E402
from app.models.alert import Alert, AlertCondition  # noqa: E402
from app.models.alert_delivery import AlertDelivery  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.mcp_token import MCPToken  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.alerts import create_alert  # noqa: E402
from app.services.events import insert_event  # noqa: E402
from app.services.mcp_tokens import create_token  # noqa: E402
from app.services.projects import create_project  # noqa: E402

# Telegram documents user ids as having "at most 52 significant bits"
# (https://core.telegram.org/bots/api#user), i.e. every real user id is
# < 2**52 = 4_503_599_627_370_496. Group/channel chat ids are negative.
# 9e18 is positive, above 2**52 and below the BIGINT max (2**63 - 1), so it
# fits ``users.telegram_user_id`` and can never be a real user or chat id.
DEMO_TELEGRAM_USER_ID = 9_000_000_000_000_000_000
DEMO_PROJECT_NAME = "Demo Shop"
DEMO_DOMAIN = "demo.example.com"  # RFC 2606 reserved domain
DEMO_TOKEN_LABEL = "demo-account"
DEMO_RATE_LIMIT_PER_SECOND = 5
SEED_DAYS = 30

# Only top up when the newest demo event is at least this old, so an
# immediate rerun is a no-op.
_TOP_UP_MIN_GAP = timedelta(hours=1)
# Guarantee a few events inside verify_integration's default 30-minute window.
_LIVE_WINDOW = timedelta(minutes=15)

_BASE_URL = f"https://{DEMO_DOMAIN}"
_PAGES: list[tuple[str, int]] = [
    ("/", 30),
    ("/products", 18),
    ("/products/espresso-machine", 12),
    ("/products/coffee-grinder", 9),
    ("/pricing", 10),
    ("/blog/how-to-brew", 7),
    ("/cart", 8),
    ("/checkout", 5),
    ("/about", 3),
]
_REFERRERS: list[tuple[str | None, int]] = [
    (None, 40),
    ("https://www.google.com/", 30),
    ("https://news.ycombinator.com/", 8),
    ("https://twitter.com/", 8),
    ("https://www.reddit.com/", 7),
    ("https://duckduckgo.com/", 7),
]
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]
_PRODUCTS = [("espresso-machine", 249), ("coffee-grinder", 89), ("beans-subscription", 19)]
_PLANS = ["free", "starter", "pro"]
_SIGNUP_SOURCES = ["organic", "newsletter", "referral", "ads"]
_ERROR_TYPES = ["PaymentDeclined", "TypeError", "NetworkError", "ValidationError"]


@dataclass
class DemoResult:
    """What :func:`ensure_demo_account` did."""

    user_id: uuid.UUID
    project_id: uuid.UUID
    created_user: bool
    created_project: bool
    events_added: int
    alerts_created: int
    raw_token: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class _Visitor:
    ip: str
    user_agent: str


def _visitors() -> list[_Visitor]:
    """120 synthetic visitors on RFC 5737 documentation IP ranges."""
    out: list[_Visitor] = []
    for i in range(120):
        ip = f"198.51.100.{i + 1}" if i < 60 else f"203.0.113.{i - 59}"
        out.append(_Visitor(ip=ip, user_agent=_USER_AGENTS[i % len(_USER_AGENTS)]))
    return out


def _weighted(rng: random.Random, items: list[tuple[Any, int]]) -> Any:
    return rng.choices([v for v, _ in items], weights=[w for _, w in items], k=1)[0]


def _sessions_for_hour(rng: random.Random, hour_start: datetime, now: datetime, days: int) -> int:
    """Expected traffic shape: daytime peak, quieter weekends, slow growth."""
    h = hour_start.hour
    daytime = 1.0 if 8 <= h <= 22 else 0.3
    weekend = 0.7 if hour_start.weekday() >= 5 else 1.0
    days_ago = (now - hour_start).total_seconds() / 86400
    growth = 1.0 + 0.4 * max(0.0, (days - days_ago) / days)
    mean = 1.6 * daytime * weekend * growth
    return sum(1 for _ in range(6) if rng.random() < mean / 6)


def _plan_hour(
    hour_start: datetime, now: datetime, visitors: list[_Visitor], days: int
) -> list[tuple[datetime, str, dict[str, Any], _Visitor, str]]:
    """Generate sample events for one hour.

    The RNG is seeded by the hour, but the traffic volume also depends on
    *now* (growth curve), so two runs can plan different events for the
    same hour. Reruns avoid duplicates because the caller keeps only
    events newer than the newest existing demo event.
    """
    rng = random.Random(int(hour_start.timestamp()))
    planned: list[tuple[datetime, str, dict[str, Any], _Visitor, str]] = []
    for s in range(_sessions_for_hour(rng, hour_start, now, days)):
        visitor = rng.choice(visitors)
        session_id = f"demo-{hour_start:%Y%m%d%H}-{s}"
        ts = hour_start + timedelta(seconds=rng.randint(0, 3000))
        referrer = _weighted(rng, _REFERRERS)
        for i in range(rng.randint(1, 4)):
            path = _weighted(rng, _PAGES)
            props: dict[str, Any] = {"url": _BASE_URL + path}
            if i == 0 and referrer:
                props["referrer"] = referrer
            planned.append((ts, "pageview", props, visitor, session_id))
            ts += timedelta(seconds=rng.randint(10, 120))
        if rng.random() < 0.10:
            planned.append(
                (
                    ts,
                    "signup",
                    {"plan": rng.choice(_PLANS), "source": rng.choice(_SIGNUP_SOURCES)},
                    visitor,
                    session_id,
                )
            )
            ts += timedelta(seconds=rng.randint(10, 60))
        if rng.random() < 0.12:
            product, price = rng.choice(_PRODUCTS)
            planned.append(
                (ts, "add_to_cart", {"product": product, "price": price}, visitor, session_id)
            )
            ts += timedelta(seconds=rng.randint(10, 90))
            if rng.random() < 0.5:
                qty = rng.choice([1, 1, 1, 2])
                planned.append(
                    (
                        ts,
                        "purchase",
                        {
                            "product": product,
                            "amount": price * qty,
                            "currency": "EUR",
                            "quantity": qty,
                        },
                        visitor,
                        session_id,
                    )
                )
        if rng.random() < 0.04:
            planned.append(
                (
                    ts,
                    "error",
                    {
                        "type": rng.choice(_ERROR_TYPES),
                        "url": _BASE_URL + rng.choice(["/checkout", "/cart", "/products"]),
                    },
                    visitor,
                    session_id,
                )
            )
    return [p for p in planned if p[0] <= now]


def _live_events(
    now: datetime, visitors: list[_Visitor]
) -> list[tuple[datetime, str, dict[str, Any], _Visitor, str]]:
    """A small burst in the last few minutes so the project looks live."""
    visitor = visitors[0]
    session_id = f"demo-live-{now:%Y%m%d%H%M}"
    return [
        (now - timedelta(minutes=6), "pageview", {"url": _BASE_URL + "/"}, visitor, session_id),
        (
            now - timedelta(minutes=4),
            "pageview",
            {"url": _BASE_URL + "/products"},
            visitor,
            session_id,
        ),
        (
            now - timedelta(minutes=2),
            "pageview",
            {"url": _BASE_URL + "/pricing"},
            visitor,
            session_id,
        ),
    ]


async def _get_or_create_user(session: AsyncSession) -> tuple[User, bool]:
    result = await session.execute(
        select(User).where(User.telegram_user_id == DEMO_TELEGRAM_USER_ID)
    )
    user = result.scalar_one_or_none()
    if user is not None:
        return user, False
    user = User(telegram_user_id=DEMO_TELEGRAM_USER_ID)
    session.add(user)
    await session.flush()
    return user, True


async def _get_or_create_project(session: AsyncSession, user: User) -> tuple[Project, bool]:
    result = await session.execute(
        select(Project)
        .where(Project.owner_user_id == user.id, Project.name == DEMO_PROJECT_NAME)
        .order_by(Project.created_at)
        .limit(1)
    )
    project = result.scalar_one_or_none()
    if project is not None:
        return project, False
    # The plaintext API key is not needed: the script inserts events
    # directly, so it is discarded.
    project, _api_key = await create_project(
        session,
        name=DEMO_PROJECT_NAME,
        admin_chat_id=DEMO_TELEGRAM_USER_ID,
        owner_user_id=user.id,
        domain_allowlist=[DEMO_DOMAIN],
    )
    project.rate_limit_per_second = DEMO_RATE_LIMIT_PER_SECOND
    await session.flush()
    return project, True


async def _seed_events(
    session: AsyncSession, project: Project, now: datetime, days: int
) -> list[tuple[datetime, str]]:
    """Insert events newer than the newest existing demo event.

    Returns ``(timestamp, event_name)`` of every inserted event.
    """
    newest = (
        await session.execute(
            select(func.max(Event.timestamp)).where(
                Event.project_id == project.id,
                # Ignore future-dated rows (client timestamps are free-form),
                # otherwise one far-future event would stop every top-up.
                Event.timestamp <= now,
            )
        )
    ).scalar_one_or_none()

    visitors = _visitors()
    planned: list[tuple[datetime, str, dict[str, Any], _Visitor, str]] = []
    if newest is None or now - newest >= _TOP_UP_MIN_GAP:
        start = now - timedelta(days=days) if newest is None else newest
        hour = start.replace(minute=0, second=0, microsecond=0)
        while hour <= now:
            planned.extend(p for p in _plan_hour(hour, now, visitors, days) if p[0] > start)
            hour += timedelta(hours=1)
    if newest is None or now - newest >= _LIVE_WINDOW:
        planned.extend(_live_events(now, visitors))
    planned.sort(key=lambda p: p[0])

    inserted_ids: list[uuid.UUID] = []
    inserted: list[tuple[datetime, str]] = []
    for ts, name, props, visitor, session_id in planned:
        # Same privacy path as app/api/ingestion.py.
        scrubbed, _dropped, _oversized = scrub_properties(props, project_id=project.id)
        visitor_hash = await hash_visitor(project.id, visitor.ip, visitor.user_agent)
        browser, os_name, device_type = parse_user_agent(visitor.user_agent)
        event = await insert_event(
            session,
            project_id=project.id,
            event_name=name,
            session_id=session_id,
            properties=scrubbed,
            timestamp=ts,
            url=scrubbed.get("url") if name == "pageview" else None,
            referrer=scrubbed.get("referrer") if name == "pageview" else None,
            visitor_hash=visitor_hash,
            browser=browser,
            os=os_name,
            device_type=device_type,
        )
        inserted_ids.append(event.id)
        inserted.append((ts, name))

    # Backfilled rows: make server receive time match the event time, so
    # "recent events" ordering reflects the sample timeline.
    for i in range(0, len(inserted_ids), 1000):
        await session.execute(
            update(Event)
            .where(Event.id.in_(inserted_ids[i : i + 1000]))
            .values(received_at=Event.timestamp)
            .execution_options(synchronize_session=False)
        )
    await session.flush()
    return inserted


async def _ensure_alerts(session: AsyncSession, project: Project) -> tuple[dict[str, Alert], int]:
    result = await session.execute(select(Alert).where(Alert.project_id == project.id))
    by_event = {a.event_name: a for a in result.scalars()}
    created = 0
    if "signup" not in by_event:
        by_event["signup"] = await create_alert(
            session,
            project_id=project.id,
            event_name="signup",
            condition=AlertCondition.every_n,
            threshold_n=10,
        )
        created += 1
    if "error" not in by_event:
        alert = await create_alert(
            session,
            project_id=project.id,
            event_name="error",
            condition=AlertCondition.threshold,
            threshold_n=3,
        )
        alert.is_active = False  # paused: listed, never evaluated
        await session.flush()
        by_event["error"] = alert
        created += 1
    return by_event, created


async def _add_alert_history(
    session: AsyncSession,
    alerts: dict[str, Alert],
    inserted: list[tuple[datetime, str]],
) -> None:
    """Add history rows matching the newly inserted events.

    ``signup`` (every 10th): one delivered row per 10 new signups.
    ``error`` (threshold 3/day): one row per day reaching 3 new errors,
    recorded before the alert was paused.
    """
    signup_alert = alerts["signup"]
    error_alert = alerts["error"]
    signups = [ts for ts, name in inserted if name == "signup"]
    rows: list[AlertDelivery] = []
    for ts in signups[9::10]:
        rows.append(
            AlertDelivery(
                alert_id=signup_alert.id,
                project_id=signup_alert.project_id,
                event_name="signup",
                condition=signup_alert.condition,
                threshold_n=signup_alert.threshold_n,
                fired_at=ts,
                delivered=True,
            )
        )
    errors_per_day: dict[str, int] = {}
    for ts, name in inserted:
        if name != "error":
            continue
        day = ts.strftime("%Y-%m-%d")
        errors_per_day[day] = errors_per_day.get(day, 0) + 1
        if errors_per_day[day] == 3:
            rows.append(
                AlertDelivery(
                    alert_id=error_alert.id,
                    project_id=error_alert.project_id,
                    event_name="error",
                    condition=error_alert.condition,
                    threshold_n=error_alert.threshold_n,
                    fired_at=ts,
                    delivered=True,
                )
            )
    session.add_all(rows)
    await session.flush()


async def ensure_demo_account(
    session: AsyncSession,
    *,
    new_token: bool = False,
    now: datetime | None = None,
    days: int = SEED_DAYS,
) -> DemoResult:
    """Create or refresh the demo user, project, sample data and token.

    *days* is how far back the first run seeds. Flushes but does not
    commit; the caller owns the transaction.
    """
    now = now or datetime.now(UTC)
    user, created_user = await _get_or_create_user(session)
    project, created_project = await _get_or_create_project(session, user)
    inserted = await _seed_events(session, project, now, days)
    alerts, alerts_created = await _ensure_alerts(session, project)
    await _add_alert_history(session, alerts, inserted)

    result = DemoResult(
        user_id=user.id,
        project_id=project.id,
        created_user=created_user,
        created_project=created_project,
        events_added=len(inserted),
        alerts_created=alerts_created,
    )

    active_tokens = (
        await session.execute(
            select(func.count())
            .select_from(MCPToken)
            .where(MCPToken.user_id == user.id, MCPToken.revoked_at.is_(None))
        )
    ).scalar_one()
    if new_token or active_tokens == 0:
        raw, _row = await create_token(session, user_id=user.id, label=DEMO_TOKEN_LABEL)
        result.raw_token = raw
    else:
        result.notes.append(
            f"{active_tokens} active token(s) already exist; pass --new-token to mint another."
        )
    return result


async def _run(new_token: bool) -> DemoResult:
    from app.core.config import get_settings
    from app.core.database import close_db, get_session_factory, init_db

    settings = get_settings()
    init_db(settings.database_url)
    try:
        async with get_session_factory()() as session:
            result = await ensure_demo_account(session, new_token=new_token)
            await session.commit()
    finally:
        await close_db()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create or refresh the demo account for the MCP server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--new-token",
        action="store_true",
        help="Mint an additional MCP token even if one already exists.",
    )
    args = parser.parse_args()

    from app.core.config import get_settings

    result = asyncio.run(_run(args.new_token))
    mcp_url = get_settings().mcp_canonical_resource_uri

    print(f"Demo user:    {result.user_id} (telegram_user_id={DEMO_TELEGRAM_USER_ID})")
    print(f"              {'created' if result.created_user else 'reused'}")
    print(f"Demo project: {result.project_id} ({DEMO_PROJECT_NAME})")
    print(f"              {'created' if result.created_project else 'reused'}")
    print(f"Events added: {result.events_added}")
    print(f"Alerts added: {result.alerts_created}")
    for note in result.notes:
        print(f"Note:         {note}")
    if result.raw_token:
        print()
        print("MCP token (shown ONCE, it is stored hashed and cannot be shown again):")
        print(f"  {result.raw_token}")
        print(f"MCP URL: {mcp_url}")
        print("Use it as 'Authorization: Bearer <token>', or paste it on the OAuth")
        print("authorize page that MCP clients open for this server.")
        print("This token only sees the demo project.")


if __name__ == "__main__":
    main()
