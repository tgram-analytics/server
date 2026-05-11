"""In-memory TTL cache for hidden event-meta details shown via callback popups.

When alert notifications are sent, properties with auto-collected `$` keys
(`$os`, `$browser`, etc.) are hidden behind a "Show technical details" button.
The button's callback_data carries a short token; this module stores the
plain-text meta block keyed by that token, with a TTL eviction so memory
stays bounded.
"""

from __future__ import annotations

import secrets
from time import monotonic

_TTL_SECONDS = 24 * 3600
_cache: dict[str, tuple[float, str]] = {}


def _evict_stale(now: float) -> None:
    cutoff = now - _TTL_SECONDS
    stale = [k for k, (t, _) in _cache.items() if t < cutoff]
    for k in stale:
        _cache.pop(k, None)


def store(text: str) -> str:
    """Stash *text* and return a short token usable in callback_data."""
    now = monotonic()
    _evict_stale(now)
    token = secrets.token_urlsafe(6)
    _cache[token] = (now, text)
    return token


def get(token: str) -> str | None:
    """Return the stored text for *token*, or None if missing/expired."""
    entry = _cache.get(token)
    if entry is None:
        return None
    t, text = entry
    if monotonic() - t > _TTL_SECONDS:
        _cache.pop(token, None)
        return None
    return text
