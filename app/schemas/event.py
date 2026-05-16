"""Pydantic schemas for event ingestion requests and responses."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Reject timestamps more than 1 day in the future or more than 1 year in the past.
_MAX_FUTURE = timedelta(days=1)
_MAX_PAST = timedelta(days=365)

# Cap number of entries in properties dict to prevent resource exhaustion.
_MAX_PROPERTIES = 100

# A property value is one of these scalars, or a list of them. ``bool`` is a
# subclass of ``int`` in Python — isinstance check just works.
_ScalarTypes = (str, int, float, bool, type(None))

# Properties whose key ends in this suffix get sorted alphabetically /
# numerically at write time, so combo queries (GROUP BY properties->'foo')
# collapse equivalent sets without an extra normalisation step.
_SET_SUFFIX = "_set"


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
    """Return *value* unchanged when it's a scalar; validate-and-maybe-sort
    when it's a list of scalars; raise ``ValueError`` otherwise.

    For keys ending in ``_set``, homogeneous lists are sorted in-place so
    that ``["b", "a"]`` and ``["a", "b"]`` collapse to the same JSONB
    representation. Heterogeneous lists (e.g. mixing str and int) are left
    untouched because ``<`` is not defined across those types in Python 3.
    """
    if _is_scalar(value):
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
        if key.endswith(_SET_SUFFIX):
            try:
                return sorted(value)
            except TypeError:
                # Heterogeneous element types — leave caller's order intact
                # rather than 400-ing on something we can technically store.
                return value
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
