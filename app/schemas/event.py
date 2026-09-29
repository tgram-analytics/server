"""Pydantic schemas for event ingestion requests and responses."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

# Reject timestamps more than 1 day in the future or more than 1 year in the past.
_MAX_FUTURE = timedelta(days=1)
_MAX_PAST = timedelta(days=365)

# Cap number of entries in properties dict to prevent resource exhaustion.
_MAX_PROPERTIES = 100

# Max length of a single scalar string property value (8 KB). Caps memory a
# single event can force the server to materialize from one property.
_MAX_VALUE_LEN = 8192

# A property value is one of these scalars, or a list of them. ``bool`` is a
# subclass of ``int`` in Python — isinstance check just works.
_ScalarTypes = (str, int, float, bool, type(None))


def _validate_timestamp(v: datetime | None) -> datetime | None:
    if v is None:
        return v
    now = datetime.now(UTC)
    # Ensure timezone-aware comparison
    ts = v if v.tzinfo is not None else v.replace(tzinfo=UTC)
    if ts > now + _MAX_FUTURE:
        raise ValueError("timestamp is too far in the future")
    if ts < now - _MAX_PAST:
        raise ValueError("timestamp is too far in the past")
    return v


def _is_scalar(v: Any) -> bool:
    """Return True when *v* is a JSON-safe scalar (str, int, float, bool, None).

    Excludes any other shape — notably ``dict`` and ``list`` — so callers can
    distinguish scalars from container values cheaply.
    """
    return isinstance(v, _ScalarTypes)


def _validate_property_value(key: str, value: Any) -> Any:
    """Return *value* unchanged when it's a scalar; validate-and-sort when
    it's a list of scalars; raise ``ValueError`` otherwise.

    Lists are sorted at write time so that ``["b", "a"]`` and ``["a", "b"]``
    collapse to the same JSONB representation — that makes combo queries
    (``GROUP BY properties->'foo'``) trivial without read-time normalisation,
    and the unnest / per-element query (``jsonb_array_elements_text``) is
    unaffected by element order either way. Heterogeneous lists (e.g.
    mixing ``str`` and ``int``) can't be compared with ``<`` in Python 3,
    so we fall back to insertion order rather than 400 on a payload we can
    technically store.

    Order-sensitive use cases (e.g. a navigation path) should serialize the
    list to a string (``"home,products,checkout"``) or use an object shape
    with positional keys — the analytics column is JSONB, not an ordered
    list type, and the sort-on-write rule applies uniformly.
    """
    if _is_scalar(value):
        if isinstance(value, str) and len(value) > _MAX_VALUE_LEN:
            raise ValueError(f"properties[{key!r}] string value exceeds {_MAX_VALUE_LEN} chars")
        return value

    if isinstance(value, list):
        for i, item in enumerate(value):
            if not _is_scalar(item):
                raise ValueError(
                    f"properties[{key!r}][{i}] must be a scalar "
                    "(str, int, float, bool, null); "
                    f"got {type(item).__name__}. "
                    "Arrays may only contain scalar primitives — "
                    "objects, nested arrays, and undefined are not allowed."
                )
            if isinstance(item, str) and len(item) > _MAX_VALUE_LEN:
                raise ValueError(
                    f"properties[{key!r}][{i}] string value exceeds {_MAX_VALUE_LEN} chars"
                )
        try:
            return sorted(value)
        except TypeError:
            # Heterogeneous element types — leave caller's order intact
            # rather than 400-ing on something we can technically store.
            return value

    raise ValueError(
        f"properties[{key!r}] must be a scalar "
        "(str, int, float, bool, null) or a list of those scalars; "
        f"got {type(value).__name__}."
    )


def _validate_properties(v: dict[str, Any]) -> dict[str, Any]:
    if len(v) > _MAX_PROPERTIES:
        raise ValueError(f"properties must have at most {_MAX_PROPERTIES} entries")
    return {key: _validate_property_value(key, val) for key, val in v.items()}


class TrackEventRequest(BaseModel):
    api_key: str = Field(..., min_length=1, max_length=255)
    event_name: str = Field(..., min_length=1, max_length=255)
    session_id: str = Field(..., min_length=1, max_length=512)
    properties: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime | None = None

    _validate_timestamp = field_validator("timestamp")(_validate_timestamp)
    _validate_properties = field_validator("properties")(_validate_properties)


class PageviewRequest(BaseModel):
    api_key: str = Field(..., min_length=1, max_length=255)
    session_id: str = Field(..., min_length=1, max_length=512)
    url: str = Field(..., min_length=1, max_length=2048)
    referrer: str | None = Field(default=None, max_length=2048)
    timestamp: datetime | None = None
    properties: dict[str, Any] = Field(default_factory=dict)

    _validate_timestamp = field_validator("timestamp")(_validate_timestamp)
    _validate_properties = field_validator("properties")(_validate_properties)


# Max taps in one /taps request. The SDK flushes at this size.
MAX_TAPS_PER_REQUEST = 50

# Max stored length of a tap's element label.
MAX_TAP_LABEL_LEN = 80


class TapPoint(BaseModel):
    """One tap. ``x`` is a fraction of the document width; ``y`` is CSS px
    from the document top (viewport px when the target is fixed or sticky)."""

    x: float = Field(..., ge=0, le=1)
    y: int = Field(..., ge=0, le=100_000)
    # Longer labels are cut to MAX_TAP_LABEL_LEN, not rejected: a long label
    # must not drop the whole batch.
    el: str | None = None

    @field_validator("el")
    @classmethod
    def _truncate_label(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v[:MAX_TAP_LABEL_LEN]
        return v or None


class TapsRequest(BaseModel):
    """Body of ``POST /api/v1/taps``: the taps and scroll depth of one pageview.

    ``session_id`` is accepted for parity with the other ingestion bodies but
    is never stored.
    """

    api_key: str = Field(..., min_length=1, max_length=255)
    session_id: str = Field(..., min_length=1, max_length=512)
    path: str = Field(..., min_length=1, max_length=2048)
    viewport: Literal["mobile", "tablet", "desktop"]
    vw: int = Field(..., ge=100, le=10_000)
    taps: list[TapPoint] = Field(default_factory=list, max_length=MAX_TAPS_PER_REQUEST)
    scroll: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.taps and self.scroll is None:
            raise ValueError("taps is empty and scroll is null; nothing to store")
        return self


class EventResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    event_name: str
    properties: dict[str, Any]
    session_id: str
    url: str | None
    referrer: str | None
    timestamp: datetime
    received_at: datetime

    model_config = {"from_attributes": True}
