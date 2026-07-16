"""Tests for the per-project rate-limit override at ingestion time.

Verifies ``app.api.ingestion._resolve_project`` honors
``project.rate_limit_per_second`` when set, and falls back to the
caller-supplied default (``Settings.rate_limit_per_second``) when the
column is ``NULL``. The default-OSS path keeps a single global value;
cloud overlays opt in by setting the per-row column at create time.

These are unit tests with mocked dependencies — the real-DB ingestion
flow is covered by ``test_ingestion_privacy.py`` and ``test_e2e.py``.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


def _project(rate_limit: int | None) -> MagicMock:
    """Build a stand-in Project with the fields ``_resolve_project`` reads."""
    proj = MagicMock()
    proj.id = uuid.uuid4()
    proj.rate_limit_per_second = rate_limit
    proj.domain_allowlist = []
    return proj


@pytest.fixture(autouse=True)
def _clear_rate_state():
    """Reset the in-process rate-limit windows between tests."""
    from app.api import ingestion

    ingestion._rate_windows.clear()
    ingestion._rate_last_access.clear()
    ingestion._rate_call_count = 0
    yield
    ingestion._rate_windows.clear()
    ingestion._rate_last_access.clear()


async def test_resolve_uses_project_override_when_set() -> None:
    """When ``project.rate_limit_per_second`` is set, it overrides the default.

    A project capped at 1 r/s blocks the second call within the same
    second even if the default would allow it (100).
    """
    from app.api import ingestion

    proj = _project(rate_limit=1)
    session = AsyncMock()

    with patch.object(ingestion, "validate_api_key", AsyncMock(return_value=proj)):
        # First call passes…
        result = await ingestion._resolve_project(
            "k", None, session, default_rate_limit=100, client_ip="1.2.3.4"
        )
        assert result is proj
        # …second call within the same second hits the per-project cap.
        with pytest.raises(HTTPException) as exc:
            await ingestion._resolve_project(
                "k", None, session, default_rate_limit=100, client_ip="1.2.3.4"
            )
        assert exc.value.status_code == 429


async def test_resolve_falls_back_to_default_when_override_is_null() -> None:
    """``rate_limit_per_second=None`` defers to the caller-supplied default."""
    from app.api import ingestion

    proj = _project(rate_limit=None)
    session = AsyncMock()

    with patch.object(ingestion, "validate_api_key", AsyncMock(return_value=proj)):
        # default=2 → first two calls pass, third is throttled.
        await ingestion._resolve_project(
            "k", None, session, default_rate_limit=2, client_ip="1.2.3.4"
        )
        await ingestion._resolve_project(
            "k", None, session, default_rate_limit=2, client_ip="1.2.3.4"
        )
        with pytest.raises(HTTPException) as exc:
            await ingestion._resolve_project(
                "k", None, session, default_rate_limit=2, client_ip="1.2.3.4"
            )
        assert exc.value.status_code == 429


async def test_resolve_zero_override_treated_as_unset() -> None:
    """``rate_limit_per_second=0`` is falsy and falls back to the default.

    A 0 override would mean "deny everything" which would be an
    operationally surprising way to disable a project. We don't support
    that semantics here — set the column to a positive integer or leave
    it NULL. Documented at the column definition.
    """
    from app.api import ingestion

    proj = _project(rate_limit=0)
    session = AsyncMock()

    with patch.object(ingestion, "validate_api_key", AsyncMock(return_value=proj)):
        # Default of 1 admits the first call; a 0 override would have rejected it.
        result = await ingestion._resolve_project(
            "k", None, session, default_rate_limit=1, client_ip="1.2.3.4"
        )
        assert result is proj
