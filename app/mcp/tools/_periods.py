"""Period-string-to-datetime translation shared across MCP tools.

Tools that take a ``period`` arg (``"7d"`` / ``"30d"`` / ``"90d"`` / ``"1y"``)
delegate to :func:`period_to_window` so the supported set lives in one
place. Returns timezone-aware UTC datetimes (using
``datetime.now(timezone.utc)`` rather than the deprecated
``datetime.utcnow()``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# One mapping is the source of truth for both the supported set and the
# day-window each label resolves to. ``"1y"`` is approximated as 365 days
# (no business-calendar alignment needed for ad-hoc analytics queries).
_PERIOD_DAYS: dict[str, int] = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "1y": 365,
}


SUPPORTED_PERIODS: tuple[str, ...] = tuple(_PERIOD_DAYS.keys())


class InvalidPeriodError(ValueError):
    """Raised when a tool receives a period outside :data:`SUPPORTED_PERIODS`."""


def period_to_window(period: str) -> tuple[datetime, datetime]:
    """Return ``(start, end)`` datetimes for *period* (UTC).

    ``end`` is "now"; ``start`` is *now - N days*. The half-open
    convention matches the analytics service (``timestamp >= start AND
    timestamp < end``).
    """
    days = _PERIOD_DAYS.get(period)
    if days is None:
        raise InvalidPeriodError(
            f"unsupported period {period!r}; expected one of {sorted(_PERIOD_DAYS)}"
        )
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    return start, end


def previous_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Return the immediately-prior window of equal length.

    Used by ``compare_periods`` so the comparison range is symmetrical
    with the current window.
    """
    width = end - start
    return start - width, start
