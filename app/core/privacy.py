"""Privacy primitives: daily salt rotation, visitor hashing, PII scrubbing.

Phase 4.1 lands the daily-salt helper. Subsequent phases extend this module
with ``hash_visitor``, ``parse_user_agent``, ``scrub_properties`` and the
log-redaction filter.

The salt is the single source of randomness used to bind a visitor identity
to one UTC day. It rotates automatically because the cache key is keyed by
``YYYYMMDD``: yesterday's salt is unreachable from today's hash inputs.
"""

from __future__ import annotations

import collections
import functools
import hashlib
import json
import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from ua_parser import user_agent_parser

from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)

_SALT_KEY_PREFIX = "ip_salt:"
_SALT_TTL_SECONDS = 60 * 60 * 48  # 48h, covers UTC-day boundary slack
_SALT_BYTES = 32  # 64 hex chars
_ONE_DAY = timedelta(days=1)

# Self-host fallback cache, keyed by ``YYYYMMDD``. Populated lazily and
# trimmed to today + yesterday to bound memory.
_local_salt_cache: dict[str, str] = {}


def _today_key() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


def _trim_local_cache(today: str) -> None:
    """Keep only today's and yesterday's entries in the local cache."""
    if len(_local_salt_cache) <= 2:
        return
    today_dt = datetime.strptime(today, "%Y%m%d")
    yesterday = (today_dt - _ONE_DAY).strftime("%Y%m%d")
    keep = {today, yesterday}
    for k in list(_local_salt_cache.keys()):
        if k not in keep:
            _local_salt_cache.pop(k, None)


async def get_today_salt() -> str:
    """Return the salt for the current UTC day, generating it if missing.

    Backed by Redis when configured (so all replicas hash identically); falls
    back to a process-local cache otherwise. The Redis path uses
    ``SET NX EX`` followed by a re-``GET`` so concurrent generators converge
    on a single value.
    """
    today = _today_key()
    key = f"{_SALT_KEY_PREFIX}{today}"
    client = get_redis()

    if client is None:
        # Self-host single-replica fallback.
        cached = _local_salt_cache.get(today)
        if cached is not None:
            return cached
        candidate = secrets.token_hex(_SALT_BYTES)
        # ``setdefault`` makes the in-memory path race-safe under
        # ``asyncio.gather``: only the first coroutine's value sticks.
        salt = _local_salt_cache.setdefault(today, candidate)
        _trim_local_cache(today)
        return salt

    existing = await client.get(key)
    if existing is not None:
        return cast(str, existing)

    candidate = secrets.token_hex(_SALT_BYTES)
    # Atomic insert-if-absent; we don't trust the bool return — we always
    # re-GET so racing callers converge on whichever value won.
    await client.set(key, candidate, ex=_SALT_TTL_SECONDS, nx=True)
    winner = await client.get(key)
    if winner is None:
        # Defensive: the key was evicted between SET and GET. Fall back to
        # our candidate; the next caller will re-populate.
        return candidate
    return cast(str, winner)


# ── Visitor hashing ────────────────────────────────────────────────────────

_VISITOR_HASH_LEN = 16  # 64-bit truncation; collision odds acceptable per project/day


async def hash_visitor(project_id: uuid.UUID, client_ip: str, user_agent: str) -> str:
    """Return a stable, daily-rotating visitor identifier.

    Formula (pinned — do not change without a coordinated migration):

        salt = await get_today_salt()
        raw  = f"{salt}{project_id}{client_ip}{user_agent}".encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    Properties:
    * Idempotent for (project, IP, UA) within one UTC day.
    * Rotates at UTC midnight because ``get_today_salt`` is keyed by date.
    * Bound to ``project_id`` so the same visitor on two different projects
      yields different hashes (no cross-project correlation).
    * Truncated to 16 hex chars (64 bits) — fits the ``events.visitor_hash``
      ``String(16)`` column. Widening would require a migration.
    """
    salt = await get_today_salt()
    raw = f"{salt}{project_id}{client_ip}{user_agent}".encode()
    return hashlib.sha256(raw).hexdigest()[:_VISITOR_HASH_LEN]


# ── User-Agent parsing ─────────────────────────────────────────────────────

_UNKNOWN = "Unknown"
_BOT_DEVICE_FAMILIES = {"Spider"}


def _classify_device_type(device_family: str, os_family: str) -> str:
    """Map ua-parser device + os heuristics to a coarse device-type bucket.

    Returns one of: ``"mobile" | "tablet" | "desktop" | "bot" | "unknown"``.
    """
    if device_family in _BOT_DEVICE_FAMILIES:
        return "bot"
    df = device_family.lower() if device_family else ""
    of = os_family.lower() if os_family else ""

    if "ipad" in df or "tablet" in df or of == "android" and "tablet" in df:
        return "tablet"
    if df == "ipad":
        return "tablet"

    # Mobile OSes signal phones unless the device family says tablet (handled above).
    if of in {"ios", "android", "windows phone", "blackberry os", "kaios"}:
        return "mobile"
    if "iphone" in df or "mobile" in df or "phone" in df:
        return "mobile"

    if df in {"other", ""} and of in {"other", ""}:
        return "unknown"

    # Desktop OSes (Windows, Mac OS X, Linux, ChromeOS, etc.) and "Other"
    # device family with a known OS → desktop.
    return "desktop"


@functools.lru_cache(maxsize=1024)
def parse_user_agent(ua: str) -> tuple[str, str, str]:
    """Parse a User-Agent string into ``(browser, os, device_type)``.

    * ``browser`` — UA family (e.g. ``"Chrome"``, ``"Firefox"``); ``"Unknown"``
      when missing or reported as ``"Other"``.
    * ``os`` — OS family (e.g. ``"Mac OS X"``, ``"iOS"``); ``"Unknown"`` when
      missing or reported as ``"Other"``.
    * ``device_type`` — one of ``"mobile" | "tablet" | "desktop" | "bot" |
      "unknown"`` derived from the parsed device + os families.

    Cached for the last 1024 distinct UA strings via ``functools.lru_cache``
    — UA distributions are heavily skewed, so this dramatically cuts repeated
    parse cost on the hot ingestion path.
    """
    if not ua:
        return (_UNKNOWN, _UNKNOWN, "unknown")

    parsed = user_agent_parser.Parse(ua)  # type: ignore[no-untyped-call]
    ua_part = parsed.get("user_agent") or {}
    os_part = parsed.get("os") or {}
    device_part = parsed.get("device") or {}

    browser_family = (ua_part.get("family") or "").strip()
    os_family = (os_part.get("family") or "").strip()
    device_family = (device_part.get("family") or "").strip()

    browser = browser_family if browser_family and browser_family != "Other" else _UNKNOWN
    os_name = os_family if os_family and os_family != "Other" else _UNKNOWN
    device_type = _classify_device_type(device_family, os_family)

    return (browser, os_name, device_type)


# ── PII tripwire + properties size cap ─────────────────────────────────────

PII_DENYLIST: frozenset[str] = frozenset(
    {
        "email",
        "phone",
        "ssn",
        "password",
        "token",
        "credit_card",
        "card_number",
        "cvv",
        "iban",
        "tax_id",
    }
)

MAX_PROPERTIES_BYTES = 4096

# ── Segment-based key matching ─────────────────────────────────────────────
#
# Property keys are normalized into lowercase segments before matching:
# camelCase and letter<->digit boundaries become separators, then the key is
# split on runs of ASCII non-alphanumeric characters (accented letters act
# as separators too). ``userEmail`` → ("user", "email"), ``email2`` →
# ("email", "2"), ``credit-card_number`` → ("credit", "card", "number").
# A key is PII when any denylist term (itself segmented the same way)
# appears as a contiguous run of segments. Comparison is exact per segment:
# ``tokens`` does not match ``token``, ``emailed`` does not match ``email``.

_SEGMENT_BOUNDARY = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])"  # camelCase: lower/digit → upper
    r"|(?<=[A-Z])(?=[A-Z][a-z])"  # acronym end: HTTPToken → HTTP_Token
    r"|(?<=[A-Za-z])(?=[0-9])"  # letter → digit: email2 → email_2
    r"|(?<=[0-9])(?=[A-Za-z])"  # digit → letter: 2email → 2_email
)
_NON_ALNUM_RUN = re.compile(r"[^a-z0-9]+")


def _key_segments(key: str) -> tuple[str, ...]:
    """Normalize *key* into lowercase ASCII-alphanumeric segments."""
    separated = _SEGMENT_BOUNDARY.sub("_", key)
    return tuple(seg for seg in _NON_ALNUM_RUN.split(separated.lower()) if seg)


# Precomputed at import: denylist terms as segment tuples (``credit_card`` →
# ("credit", "card")), so the hot path never re-segments the denylist.
# Empty tuples are filtered so the matcher can assume every term is non-empty.
_PII_TERM_SEGMENTS: tuple[tuple[str, ...], ...] = tuple(
    segs for segs in (_key_segments(term) for term in PII_DENYLIST) if segs
)

# Keys longer than this bypass the lru_cache: property names are attacker
# controlled (request bodies up to ~1 MB reach the scrubber before the 4 KB
# properties cap applies), and the cache would otherwise pin arbitrarily
# large distinct strings in memory.
_MAX_CACHED_KEY_LEN = 256


@functools.lru_cache(maxsize=4096)
def _is_pii_key_cached(key: str) -> bool:
    """True when any denylist term is a contiguous segment run inside *key*.

    Cached: property-key strings repeat heavily across events, so the
    segmentation + scan cost is paid once per distinct key.
    """
    segments = _key_segments(key)
    n = len(segments)
    for term in _PII_TERM_SEGMENTS:
        t = len(term)
        if t <= n:
            for i in range(n - t + 1):
                if segments[i : i + t] == term:
                    return True
    return False


def _is_pii_key(key: str) -> bool:
    """Cache-aware wrapper: oversized keys skip the cache entirely."""
    if len(key) > _MAX_CACHED_KEY_LEN:
        return _is_pii_key_cached.__wrapped__(key)
    return _is_pii_key_cached(key)


# Module-level counters for PII / oversized observations. Snapshotted by
# ``get_privacy_counters``; the operator-only HTTP surface arrives in 4.7.
_privacy_counters: collections.Counter[tuple[str, str]] = collections.Counter()


def scrub_properties(
    props: dict[str, Any],
    *,
    project_id: uuid.UUID | None = None,
) -> tuple[dict[str, Any], list[str], bool]:
    """Drop PII-named keys and enforce a 4 KB serialized-size cap.

    Returns ``(scrubbed, dropped_keys, oversized)``:

    * Keys are segment-matched against ``PII_DENYLIST``: each key is split
      into lowercase segments (camelCase boundaries and non-alphanumeric
      separators), and the key is dropped when any denylist term appears as
      a contiguous run of whole segments (``user_email``, ``userEmail`` and
      ``stripe.token`` are dropped; ``tokens_count`` and ``emailed`` are
      kept). The original casing is preserved in ``dropped_keys`` for
      telemetry.
    * Values are never inspected — key-based denylist only.
    * If the surviving dict serializes to more than ``MAX_PROPERTIES_BYTES``
      bytes (compact JSON), all properties are dropped and ``oversized`` is
      ``True``. The event itself is still accepted by the caller.

    Emits structured ``logger.warning`` events for both PII drops and oversize
    truncation, and bumps ``_privacy_counters`` for monitoring snapshots.
    """
    dropped_keys: list[str] = []
    survivors: dict[str, Any] = {}
    for key, value in props.items():
        if isinstance(key, str) and _is_pii_key(key):
            dropped_keys.append(key)
            continue
        survivors[key] = value

    pid_str = str(project_id) if project_id else None

    if dropped_keys:
        logger.warning(
            "pii_dropped",
            extra={"project_id": pid_str, "keys": dropped_keys},
        )
        _privacy_counters[(str(project_id), "pii")] += len(dropped_keys)

    encoded = json.dumps(survivors, separators=(",", ":"), default=str)
    if len(encoded) > MAX_PROPERTIES_BYTES:
        logger.warning(
            "properties_oversized",
            extra={"project_id": pid_str, "bytes": len(encoded)},
        )
        _privacy_counters[(str(project_id), "oversized")] += 1
        return ({}, dropped_keys, True)

    return (survivors, dropped_keys, False)


# ── Log redaction filter ──────────────────────────────────────────────────

_redaction_installed = False
_unpatched_handler_format: Any = None

_REDACT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p)
    for p in (
        r"proj_[a-f0-9]{64}",
        r"sk_(?:live|test)_[A-Za-z0-9]+",
        r"mcp_[a-f0-9]{64}",
        r"\d{6,}:[A-Za-z0-9_-]{30,}",
        r"(?i)(?:email|phone|ssn|password|token|credit_card)[\"\']?\s*[:=]\s*[\"\']?[^\"\'\s,}]+",
    )
]


def redact_text(text: str) -> str:
    """Return *text* with every :data:`_REDACT_PATTERNS` match masked."""
    for pat in _REDACT_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs sensitive tokens from formatted log records.

    Collapses the record: interpolates ``msg % args``, masks the result, and
    clears ``args`` so a downstream re-format cannot double-interpolate. That
    collapse is destructive — a formatter that reads ``record.args``
    structurally (``uvicorn.logging.AccessFormatter``) will not survive it —
    so this is for attaching to one specific logger or handler whose
    formatter you control.

    Process-wide redaction goes through :func:`install_log_redaction`
    instead, which leaves records intact.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


def install_log_redaction() -> None:
    """Mask secrets in every line this process logs. Idempotent.

    Installed from ``create_app()``.

    Wraps ``logging.Handler.format`` so redaction runs on the **rendered
    line**, at the last point before a handler writes it. Two constraints
    force that placement, and between them they rule out the two obvious
    alternatives:

    * A filter on the root logger only sees records logged *through* the root
      logger. Records propagated up from a child logger reach the root
      logger's handlers without ever being offered to its filters, so every
      third-party logger went unredacted — ``httpx`` logs the outbound
      Telegram URL, bot token included.
    * Redacting the record itself (via the record factory, or any filter)
      means interpolating ``msg % args`` early and clearing ``args``. That
      breaks formatters that read ``record.args`` structurally:
      ``uvicorn.logging.AccessFormatter`` unpacks five values from it and
      raises ``ValueError`` on every access log line.

    Formatting the record is exactly the step that resolves ``msg % args``
    into text, so redacting its output needs no assumptions about either.
    It also catches secrets that straddle the boundary — ``"bot%s"`` plus a
    token argument is one string by then.

    Coverage note: a handler that overrides ``format`` itself, or renders
    from record attributes without calling it (the Sentry integration builds
    its event payload directly), is outside this. :class:`RedactingFilter`
    remains available to attach to such a handler.
    """
    global _redaction_installed, _unpatched_handler_format
    if _redaction_installed:
        return

    original_format = logging.Handler.format

    def format_redacted(self: logging.Handler, record: logging.LogRecord) -> str:
        return redact_text(original_format(self, record))

    logging.Handler.format = format_redacted  # type: ignore[method-assign]
    _unpatched_handler_format = original_format
    _redaction_installed = True


def redaction_installed() -> bool:
    """True once :func:`install_log_redaction` has run. Introspection for tests."""
    return _redaction_installed


def get_privacy_counters() -> dict[str, int]:
    """Return a snapshot of the privacy counters keyed ``"{project_id}:{kind}"``.

    ``kind`` is one of ``"pii"`` or ``"oversized"``. Intended for the operator
    HTTP endpoint landing in Phase 4.7.
    """
    return {f"{pid}:{kind}": count for (pid, kind), count in _privacy_counters.items()}
