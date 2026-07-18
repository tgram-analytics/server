"""Direct unit tests for the project-create request service (no DB).

Covers the TTL boundary (``is_expired``), the ``PENDING_CAP`` /
``REQUEST_TTL`` regression constants, the pending-cap guard in
``create_request``, and the compare-and-set semantics of
``claim_request``. Rows are ``types.SimpleNamespace`` stand-ins and the
session is mocked — the ``project_create_requests`` table uses a
Postgres ``ARRAY`` column that doesn't compile on SQLite.
"""

from __future__ import annotations

import types
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.project_requests as svc
from app.services.project_requests import (
    PENDING_CAP,
    REQUEST_TTL,
    PendingCapExceededError,
    claim_request,
    create_request,
    is_expired,
)

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


def _row(
    *,
    status: str = "pending",
    created_at: datetime = NOW - timedelta(minutes=1),
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        name="myapp",
        status=status,
        project_id=None,
        created_at=created_at,
        resolved_at=None,
    )


# ── regression constants ────────────────────────────────────────────────────


def test_pending_cap_is_three():
    assert PENDING_CAP == 3


def test_request_ttl_is_five_minutes():
    assert timedelta(minutes=5) == REQUEST_TTL


# ── is_expired ──────────────────────────────────────────────────────────────


def test_is_expired_exactly_at_ttl_is_not_expired():
    """The comparison is strict (>): exactly REQUEST_TTL old is still valid."""
    row = _row(created_at=NOW - REQUEST_TTL)
    assert is_expired(row, now=NOW) is False


def test_is_expired_one_second_past_ttl():
    row = _row(created_at=NOW - REQUEST_TTL - timedelta(seconds=1))
    assert is_expired(row, now=NOW) is True


def test_is_expired_fresh_row():
    row = _row(created_at=NOW - timedelta(seconds=30))
    assert is_expired(row, now=NOW) is False


@pytest.mark.parametrize("status", ["approved", "rejected", "expired"])
def test_is_expired_non_pending_old_row_is_false(status):
    row = _row(status=status, created_at=NOW - timedelta(days=1))
    assert is_expired(row, now=NOW) is False


def test_is_expired_naive_created_at_treated_as_utc():
    """SQLite hands back naive datetimes in tests; they are read as UTC."""
    naive = (NOW - timedelta(minutes=10)).replace(tzinfo=None)
    row = _row(created_at=naive)
    assert is_expired(row, now=NOW) is True

    naive_fresh = (NOW - timedelta(minutes=1)).replace(tzinfo=None)
    assert is_expired(_row(created_at=naive_fresh), now=NOW) is False


# ── create_request (pending cap) ────────────────────────────────────────────


async def test_create_request_cap_exceeded():
    session = MagicMock()
    count_result = MagicMock()
    count_result.scalar_one.return_value = PENDING_CAP
    session.execute = AsyncMock(return_value=count_result)
    session.add = MagicMock()

    with pytest.raises(PendingCapExceededError):
        await create_request(session, owner_user_id=uuid.uuid4(), name="myapp")

    session.add.assert_not_called()


# ── claim_request (compare-and-set) ─────────────────────────────────────────


async def test_claim_request_wins(monkeypatch):
    audit_mock = AsyncMock()
    monkeypatch.setattr(svc, "write_audit", audit_mock)
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    session.flush = AsyncMock()
    row = _row()
    project_id = uuid.uuid4()

    claimed = await claim_request(session, row, status="approved", project_id=project_id)

    assert claimed is True
    # The ORM object is refreshed in place for callers.
    assert row.status == "approved"
    assert row.project_id == project_id
    assert row.resolved_at is not None
    audit_mock.assert_awaited_once()
    assert audit_mock.await_args.kwargs["action"] == "project.create.request.approved"
    assert audit_mock.await_args.kwargs["target_id"] == str(row.id)


async def test_claim_request_loses_race(monkeypatch):
    """rowcount 0 → another transaction already resolved it: no writes."""
    audit_mock = AsyncMock()
    monkeypatch.setattr(svc, "write_audit", audit_mock)
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=0))
    session.flush = AsyncMock()
    row = _row()

    claimed = await claim_request(session, row, status="rejected")

    assert claimed is False
    assert row.status == "pending"
    assert row.resolved_at is None
    audit_mock.assert_not_awaited()
