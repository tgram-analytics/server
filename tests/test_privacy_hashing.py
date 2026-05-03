"""Phase 4.2 verification: visitor hashing + UA parsing.

Pinned-input tests: a regression in the hash formula (input order, encoding,
truncation length) flips the hex literal below and fails this test.
"""

from __future__ import annotations

import uuid

import pytest

from app.core import privacy, redis_client

# ── Pinned inputs / expected output ────────────────────────────────────────

# Inputs are deliberately fixed so the hash digest below is stable.
_PINNED_PROJECT = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PINNED_IP = "1.2.3.4"
_PINNED_UA = "Mozilla/5.0"
_PINNED_SALT = "a" * 64
# Computed once with the formula
#   sha256(f"{salt}{project_id}{client_ip}{user_agent}".encode()).hexdigest()[:16]
# A regression in that formula will break this literal — fix the formula,
# don't update the literal.
_EXPECTED_HASH = "6b4d72969261bf2c"


@pytest.fixture(autouse=True)
def _reset_state():
    """Clear Redis client + local salt cache between tests."""
    redis_client._client = None
    privacy._local_salt_cache.clear()
    yield
    redis_client._client = None
    privacy._local_salt_cache.clear()


# ── hash_visitor ───────────────────────────────────────────────────────────


async def test_hash_visitor_pinned_inputs(monkeypatch):
    """With a fixed salt, hash_visitor produces the pinned 16-char digest."""

    async def _fixed_salt() -> str:
        return _PINNED_SALT

    monkeypatch.setattr(privacy, "get_today_salt", _fixed_salt)

    h = await privacy.hash_visitor(_PINNED_PROJECT, _PINNED_IP, _PINNED_UA)
    assert h == _EXPECTED_HASH
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


async def test_hash_visitor_idempotent_same_day():
    """Same inputs on the same UTC day yield the same hash (local-cache path)."""
    h1 = await privacy.hash_visitor(_PINNED_PROJECT, _PINNED_IP, _PINNED_UA)
    h2 = await privacy.hash_visitor(_PINNED_PROJECT, _PINNED_IP, _PINNED_UA)
    assert h1 == h2


async def test_hash_visitor_day_rollover_changes_hash(monkeypatch):
    """Faking a different UTC day produces a different hash (salt rotated)."""
    today_real = privacy._today_key()
    h_today = await privacy.hash_visitor(_PINNED_PROJECT, _PINNED_IP, _PINNED_UA)

    fake_day = "20991231" if today_real != "20991231" else "20990101"
    monkeypatch.setattr(privacy, "_today_key", lambda: fake_day)

    h_tomorrow = await privacy.hash_visitor(_PINNED_PROJECT, _PINNED_IP, _PINNED_UA)
    assert h_today != h_tomorrow
    assert len(h_tomorrow) == 16


async def test_hash_visitor_project_isolation(monkeypatch):
    """Different project_id (same salt/IP/UA) yields a different hash."""

    async def _fixed_salt() -> str:
        return _PINNED_SALT

    monkeypatch.setattr(privacy, "get_today_salt", _fixed_salt)

    other_project = uuid.UUID("00000000-0000-0000-0000-000000000002")
    h_a = await privacy.hash_visitor(_PINNED_PROJECT, _PINNED_IP, _PINNED_UA)
    h_b = await privacy.hash_visitor(other_project, _PINNED_IP, _PINNED_UA)
    assert h_a != h_b


# ── parse_user_agent ───────────────────────────────────────────────────────


def test_parse_user_agent_chrome_desktop():
    """A standard Chrome on macOS UA returns Chrome / Mac OS X / desktop."""
    # Bypass the lru_cache so prior tests' cached entries don't interfere.
    privacy.parse_user_agent.cache_clear()
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    browser, os_name, device_type = privacy.parse_user_agent(ua)
    assert browser == "Chrome"
    assert os_name == "Mac OS X"
    assert device_type == "desktop"


def test_parse_user_agent_empty_returns_unknown():
    """Empty UA returns ("Unknown", "Unknown", "unknown")."""
    privacy.parse_user_agent.cache_clear()
    assert privacy.parse_user_agent("") == ("Unknown", "Unknown", "unknown")


def test_parse_user_agent_iphone_is_mobile():
    """An iPhone UA classifies as mobile."""
    privacy.parse_user_agent.cache_clear()
    ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X)"
    _, os_name, device_type = privacy.parse_user_agent(ua)
    assert os_name == "iOS"
    assert device_type == "mobile"


def test_parse_user_agent_googlebot_is_bot():
    """Googlebot UA classifies as bot."""
    privacy.parse_user_agent.cache_clear()
    ua = "Googlebot/2.1 (+http://www.google.com/bot.html)"
    _, _, device_type = privacy.parse_user_agent(ua)
    assert device_type == "bot"
