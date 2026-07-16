"""Event ingestion endpoints: POST /api/v1/track and POST /api/v1/pageview.

Authentication: ``api_key`` field in the JSON request body.
Rate limiting:  per-project sliding-window. Limit is
                ``project.rate_limit_per_second`` when set, else
                ``Settings.rate_limit_per_second`` (default 100 req/s).
Origin check:   project's domain_allowlist; empty list = allow all.
"""

from __future__ import annotations

import html
import uuid
from collections import defaultdict, deque
from time import monotonic
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models.project import Project

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_session, get_session_factory
from app.core.privacy import hash_visitor, parse_user_agent, scrub_properties
from app.core.security import validate_api_key
from app.schemas.event import PageviewRequest, TrackEventRequest
from app.services.events import evaluate_alerts, insert_event, is_origin_allowed

router = APIRouter(prefix="/api/v1", tags=["ingestion"])

# ── In-memory sliding-window rate limiter (1-second window) ────────────────
#
# NOTE: this limiter is process-local and in-memory. A multi-worker / multi-pod
# deploy runs one copy per worker, so the effective cap is multiplied by the
# worker count. This is a best-effort guard against accidental floods and cheap
# DoS; a hard anti-DoS guarantee needs a shared store (Redis) or enforcement at
# the edge (CDN / API gateway).

_rate_windows: dict[str, deque[float]] = defaultdict(deque)
_rate_last_access: dict[str, float] = {}

# Two-tier per-IP throttle knobs, read from settings at import time. Kept as
# module attributes so tests can monkeypatch them.
_ingest_ip_rate_limit: int = get_settings().ingest_ip_rate_limit_per_second
_invalid_key_rate_limit: int = get_settings().invalid_key_rate_limit_per_second

# Evict idle projects every N calls to prevent unbounded memory growth.
_EVICTION_INTERVAL = 500
_EVICTION_TTL = 60.0  # seconds since last access before eviction
_rate_call_count = 0


def _evict_stale_entries(now: float) -> None:
    """Remove rate-limiter state for keys idle longer than _EVICTION_TTL."""
    stale = [k for k, t in _rate_last_access.items() if now - t > _EVICTION_TTL]
    for k in stale:
        _rate_windows.pop(k, None)
        _rate_last_access.pop(k, None)


def _is_over_limit(key: str | uuid.UUID, limit: int) -> bool:
    """Read-only check: True if *key* is at/over *limit* hits in the last second.

    Prunes the sliding window but does NOT append, so calling this can never
    consume a key's budget. Use it for a pre-flight check that must not itself
    count as a hit (e.g. gating a valid-key request on the invalid-key budget).
    """
    key = str(key)
    now = monotonic()
    dq = _rate_windows.get(key)
    if not dq:
        return False
    cutoff = now - 1.0
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq) >= limit


def _record_hit(key: str | uuid.UUID) -> None:
    """Append one hit for *key* at the current time (and run periodic eviction)."""
    global _rate_call_count
    key = str(key)
    now = monotonic()

    _rate_call_count += 1
    if _rate_call_count >= _EVICTION_INTERVAL:
        _rate_call_count = 0
        _evict_stale_entries(now)

    _rate_last_access[key] = now
    _rate_windows[key].append(now)


def _is_rate_limited(key: str | uuid.UUID, limit: int) -> bool:
    """Check-and-record: True (without recording) if *key* is already over
    *limit*, otherwise record one hit and return False."""
    if _is_over_limit(key, limit):
        return True
    _record_hit(key)
    return False


def _is_ip_rate_limited(client_ip: str, limit: int) -> bool:
    """Tier 1 coarse valve: cap ALL ingestion per source IP. Keyed 'ip:<addr>'
    so it can never collide with project-id keys. Counts every request."""
    return _is_rate_limited(f"ip:{client_ip}", limit)


# ── Background task helper ─────────────────────────────────────────────────


async def _run_alert_evaluation(
    project_id: uuid.UUID, event_name: str, properties: dict[str, Any] | None = None
) -> None:
    """Background task: evaluate alerts after event insertion.

    Creates its own session so it can run after the HTTP response is sent.
    Sends Telegram notifications for fired alerts.
    """
    import logging

    from sqlalchemy import select

    from app.bot.setup import get_bot
    from app.models.alert import AlertCondition
    from app.models.project import Project

    log = logging.getLogger(__name__)
    try:
        factory = get_session_factory()
        async with factory() as session, session.begin():
            fired = await evaluate_alerts(session, project_id=project_id, event_name=event_name)
            if not fired:
                return

            log.info(
                "alerts fired: project=%s event=%s count=%d",
                project_id,
                event_name,
                len(fired),
            )

            result = await session.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if project is None:
                log.warning("project not found for alert notification: %s", project_id)
                return

            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            bot = get_bot()
            safe_event = html.escape(event_name)
            safe_project = html.escape(project.name)
            for alert in fired:
                if alert.condition == AlertCondition.every:
                    msg = f"🔔 Event <b>{safe_event}</b> received on <b>{safe_project}</b>"
                elif alert.condition == AlertCondition.every_n:
                    msg = (
                        f"🔔 Event <b>{safe_event}</b> received "
                        f"<b>{alert.threshold_n}</b> times on <b>{safe_project}</b>"
                    )
                else:  # threshold
                    msg = (
                        f"🔔 Event <b>{safe_event}</b> exceeded "
                        f"<b>{alert.threshold_n}</b> today on <b>{safe_project}</b>"
                    )

                hidden_lines: list[str] = []
                if properties:
                    visible_lines: list[str] = []
                    for k, v in properties.items():
                        key_str = str(k)
                        is_meta = key_str.startswith("$") and key_str != "$timezone"
                        if is_meta:
                            hidden_lines.append(f"{key_str}: {v}")
                        else:
                            visible_lines.append(
                                f"<b>{html.escape(key_str)}:</b> {html.escape(str(v))}"
                            )
                    if visible_lines:
                        msg += "\n\n" + "\n".join(visible_lines)

                aid = str(alert.id)
                rows = []
                if hidden_lines:
                    from app.bot.event_meta_cache import store as _store_meta

                    token = _store_meta("\n".join(hidden_lines))
                    rows.append(
                        [
                            InlineKeyboardButton(
                                f"🔍 Show technical details ({len(hidden_lines)})",
                                callback_data=f"alert_meta:{token}",
                            )
                        ]
                    )
                # Carry the triggering event's own property keys through a
                # cache token so the chart handler can put their pie charts
                # first. Falls back to plain alert_pie:{id} when there are
                # no custom properties (or the token has expired).
                charts_cb = f"alert_pie:{aid}"
                prop_keys = [str(k) for k in (properties or {}) if not str(k).startswith("$")]
                if prop_keys:
                    from app.bot.event_meta_cache import store as _store_keys

                    charts_cb = f"alert_pie:{aid}:{_store_keys(','.join(prop_keys))}"
                rows.append([InlineKeyboardButton("📊 More charts", callback_data=charts_cb)])
                rows.append(
                    [
                        InlineKeyboardButton("🔕 Silence", callback_data=f"alert_sil:{aid}"),
                        InlineKeyboardButton("🚫 Disable", callback_data=f"alert_dis:{aid}"),
                    ]
                )
                keyboard = InlineKeyboardMarkup(rows)

                try:
                    await bot.send_message(
                        chat_id=project.admin_chat_id,
                        text=msg,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                except Exception:
                    log.exception(
                        "failed to send alert notification: alert=%s project=%s",
                        alert.id,
                        project_id,
                    )
    except Exception:
        log.exception("alert evaluation failed for project=%s event=%s", project_id, event_name)


# ── Shared pre-flight logic ────────────────────────────────────────────────


async def _resolve_project(
    api_key: str,
    origin: str | None,
    session: AsyncSession,
    default_rate_limit: int,
    client_ip: str,
) -> Project:
    """Validate API key, rate limit, and origin. Returns the Project.

    The effective per-second rate limit is ``project.rate_limit_per_second``
    when set, falling back to ``default_rate_limit`` (the global
    ``Settings.rate_limit_per_second``) when the column is NULL. Cloud
    overlays set the per-row override at project-create time so each
    plan tier gets its own cap.

    Two per-IP guards run *before* ``validate_api_key`` so an attacker can't
    force an un-throttled DB SELECT on every request:

    * Tier 1 (coarse valve): a high per-IP ceiling on all ingestion traffic,
      set far above any legitimate single-IP rate. Bounds worst-case DB load
      without throttling valid server-side SDKs or NAT/proxy pools.
    * Tier 2 (invalid-key penalty): a strict per-IP budget that counts only
      failed key lookups. It is *checked* (read-only) before the DB lookup, so
      an IP already over budget is rejected without touching the DB, and a
      failed lookup *records* one hit afterwards. Valid keys never consume it.
    """
    # Tier 1: coarse per-IP valve over all traffic.
    if _is_ip_rate_limited(client_ip, _ingest_ip_rate_limit):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # Tier 2 pre-check: read-only, so a valid-key request never spends the
    # invalid-key budget. If this IP is already over budget, block before the
    # DB lookup runs.
    badkey_key = f"ip:badkey:{client_ip}"
    if _is_over_limit(badkey_key, _invalid_key_rate_limit):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    project = await validate_api_key(api_key, session)
    if project is None:
        # Charge one attempt against this IP's invalid-key budget.
        _record_hit(badkey_key)
        raise HTTPException(status_code=400, detail="Invalid API key")

    effective_limit = project.rate_limit_per_second or default_rate_limit
    if _is_rate_limited(project.id, effective_limit):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    if not is_origin_allowed(project.domain_allowlist, origin):
        raise HTTPException(status_code=403, detail="Origin not in allowlist")

    return project


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("/track", status_code=202)
async def track(
    body: TrackEventRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Ingest a custom event.

    Returns 202 immediately; alert evaluation runs as a background task.
    """
    origin = request.headers.get("origin")
    # Privacy: derive visitor_hash and parsed-UA fields here; raw IP/UA never
    # leave this function. client_ip is also the key for the pre-auth per-IP
    # throttle inside _resolve_project.
    client_ip = request.client.host if request.client else ""
    project = await _resolve_project(
        body.api_key, origin, session, settings.rate_limit_per_second, client_ip
    )

    ua = request.headers.get("user-agent", "")
    visitor_hash = await hash_visitor(project.id, client_ip, ua)
    browser, os_name, device_type = parse_user_agent(ua)

    scrubbed, _dropped, _oversized = scrub_properties(body.properties, project_id=project.id)

    await insert_event(
        session,
        project_id=project.id,
        event_name=body.event_name,
        session_id=body.session_id,
        properties=scrubbed,
        timestamp=body.timestamp,
        visitor_hash=visitor_hash,
        browser=browser,
        os=os_name,
        device_type=device_type,
    )
    await session.commit()

    background_tasks.add_task(_run_alert_evaluation, project.id, body.event_name, body.properties)
    return {"status": "accepted"}


@router.post("/pageview", status_code=202)
async def pageview(
    body: PageviewRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Ingest a pageview event.

    Forces ``event_name = "pageview"`` and stores url/referrer in dedicated
    columns as well as in ``properties`` for easy querying.
    """
    origin = request.headers.get("origin")
    # Privacy: derive visitor_hash and parsed-UA fields here; raw IP/UA never
    # leave this function. client_ip is also the key for the pre-auth per-IP
    # throttle inside _resolve_project.
    client_ip = request.client.host if request.client else ""
    project = await _resolve_project(
        body.api_key, origin, session, settings.rate_limit_per_second, client_ip
    )

    ua = request.headers.get("user-agent", "")
    visitor_hash = await hash_visitor(project.id, client_ip, ua)
    browser, os_name, device_type = parse_user_agent(ua)

    properties = {**body.properties, "url": body.url}
    if body.referrer:
        properties["referrer"] = body.referrer

    scrubbed, _dropped, _oversized = scrub_properties(properties, project_id=project.id)

    await insert_event(
        session,
        project_id=project.id,
        event_name="pageview",
        session_id=body.session_id,
        properties=scrubbed,
        timestamp=body.timestamp,
        url=body.url,
        referrer=body.referrer,
        visitor_hash=visitor_hash,
        browser=browser,
        os=os_name,
        device_type=device_type,
    )
    await session.commit()

    background_tasks.add_task(_run_alert_evaluation, project.id, "pageview", properties)
    return {"status": "accepted"}
