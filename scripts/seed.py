#!/usr/bin/env python3
"""Seed the analytics server with test data.

Creates a project (or reuses an existing one by name) and fires a stream of
realistic-looking events so you have something to chart and alert on.

Usage:
    python scripts/seed.py --url http://localhost:8000 --key <SECRET_KEY>
    python scripts/seed.py --url http://localhost:8000 --key <SECRET_KEY> --events 200
    python scripts/seed.py --url http://localhost:8000 --key <SECRET_KEY> \\
        --project "my-demo-site" --events 500 --spread-days 30

Options:
    --url           Base URL of the analytics server  [default: http://localhost:8000]
    --key           SECRET_KEY / X-Internal-Key header value  [required]
    --project       Project name to create / reuse          [default: demo-site.com]
    --events        Total number of events to send          [default: 100]
    --spread-days   Spread timestamps over this many past days [default: 7]
    --no-create     Skip project creation (use existing api_key via --api-key)
    --api-key       Existing project API key (skips project creation)
"""

import argparse
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta

try:
    import httpx
except ImportError:
    print("httpx not installed. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)

# ── Realistic fake data ────────────────────────────────────────────────────────

EVENT_NAMES = [
    "page_view",
    "page_view",
    "page_view",  # weighted higher
    "button_click",
    "button_click",
    "form_submit",
    "signup",
    "login",
    "purchase",
    "add_to_cart",
    "search",
    "video_play",
    "download",
]

PAGES = [
    "/",
    "/pricing",
    "/features",
    "/blog",
    "/about",
    "/contact",
    "/docs",
    "/signup",
    "/login",
    "/dashboard",
]

BUTTONS = [
    "cta-hero",
    "pricing-monthly",
    "pricing-annual",
    "try-free",
    "get-started",
    "learn-more",
]

PLANS = ["free", "pro", "enterprise"]

REFERRERS = [
    "https://google.com",
    "https://twitter.com",
    "https://linkedin.com",
    "",
    "",
    "",  # direct weighted higher
]


def _random_properties(event_name: str) -> dict:
    if event_name == "page_view":
        return {"url": random.choice(PAGES), "referrer": random.choice(REFERRERS)}
    if event_name == "button_click":
        return {"button_id": random.choice(BUTTONS), "page": random.choice(PAGES)}
    if event_name == "form_submit":
        return {"form_id": "contact-form", "page": "/contact"}
    if event_name == "signup":
        return {
            "plan": random.choice(PLANS),
            "source": random.choice(["organic", "paid", "referral"]),
        }
    if event_name == "login":
        return {"method": random.choice(["email", "google", "github"])}
    if event_name == "purchase":
        amount = random.choice([9, 29, 99, 299])
        return {"plan": random.choice(PLANS), "amount": amount, "currency": "USD"}
    if event_name == "add_to_cart":
        return {"plan": random.choice(PLANS), "page": "/pricing"}
    if event_name == "search":
        return {"query": random.choice(["analytics", "pricing", "docs", "api", "integrations"])}
    if event_name == "video_play":
        return {"video_id": random.choice(["intro", "demo", "tutorial"])}
    if event_name == "download":
        return {"file": random.choice(["sdk.zip", "report.pdf", "export.csv"])}
    return {}


def _random_timestamp(spread_days: int) -> str:
    """Return an ISO timestamp somewhere in the last *spread_days* days."""
    delta = timedelta(
        days=random.uniform(0, spread_days),
        hours=random.uniform(0, 23),
        minutes=random.uniform(0, 59),
    )
    ts = datetime.now(UTC) - delta
    return ts.isoformat()


# ── API helpers ────────────────────────────────────────────────────────────────


def create_project(base_url: str, secret_key: str, project_name: str) -> str:
    """Create a project and return its plaintext API key."""
    with httpx.Client(base_url=base_url, timeout=15) as client:
        resp = client.post(
            "/api/v1/internal/projects",
            json={"name": project_name, "admin_chat_id": 0},
            headers={"X-Internal-Key": secret_key},
        )
    if resp.status_code != 201:
        print(
            f"[ERROR] Failed to create project: {resp.status_code} {resp.text}",
            file=sys.stderr,
        )
        sys.exit(1)
    data = resp.json()
    print(f"[OK] Created project '{project_name}'  id={data['id']}")
    print(f"[OK] API key (save this!): {data['api_key']}")
    return data["api_key"]


def send_event(
    base_url: str,
    api_key: str,
    event_name: str,
    session_id: str,
    properties: dict,
    timestamp: str | None,
) -> bool:
    payload: dict = {
        "api_key": api_key,
        "event_name": event_name,
        "session_id": session_id,
        "properties": properties,
    }
    if timestamp:
        payload["timestamp"] = timestamp

    with httpx.Client(base_url=base_url, timeout=10) as client:
        resp = client.post("/api/v1/track", json=payload)

    return resp.status_code == 202


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the tgram-analytics server with test events.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--url", default="http://localhost:8000", help="Server base URL")
    parser.add_argument("--key", required=True, help="SECRET_KEY / X-Internal-Key")
    parser.add_argument("--project", default="demo-site.com", help="Project name")
    parser.add_argument("--events", type=int, default=100, help="Number of events to send")
    parser.add_argument(
        "--spread-days", type=int, default=7, help="Spread timestamps over N past days"
    )
    parser.add_argument("--api-key", default="", help="Existing project API key (skips creation)")
    args = parser.parse_args()

    print(f"[>>] Targeting server: {args.url}")

    # ── Step 1: resolve API key ────────────────────────────────────────────────
    if args.api_key:
        api_key = args.api_key
        print(f"[>>] Using provided api_key: {api_key[:12]}…")
    else:
        api_key = create_project(args.url, args.key, args.project)

    # ── Step 2: fire events ────────────────────────────────────────────────────
    print(f"[>>] Sending {args.events} events spread over last {args.spread_days} day(s)…")

    ok = fail = 0
    # Simulate ~20 concurrent "users" with stable session IDs
    sessions = [str(uuid.uuid4()) for _ in range(20)]

    for i in range(args.events):
        event_name = random.choice(EVENT_NAMES)
        session_id = random.choice(sessions)
        props = _random_properties(event_name)
        ts = _random_timestamp(args.spread_days)

        success = send_event(args.url, api_key, event_name, session_id, props, ts)
        if success:
            ok += 1
        else:
            fail += 1

        # Progress indicator every 10 events
        if (i + 1) % 10 == 0:
            print(f"    {i + 1}/{args.events}  ok={ok}  fail={fail}")

    print()
    print("─" * 50)
    print(f"[DONE] Sent {args.events} events:  ✅ {ok} accepted  ❌ {fail} failed")
    print()
    print("Event breakdown (approx):")
    unique_events = list(set(EVENT_NAMES))
    for name in sorted(unique_events):
        weight = EVENT_NAMES.count(name)
        approx = round(args.events * weight / len(EVENT_NAMES))
        print(f"  {name:<20} ~{approx}")

    if fail > 0:
        print(
            f"\n[WARN] {fail} events failed. Check the server URL and api_key.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
