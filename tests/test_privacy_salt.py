"""Phase 4.1 verification: daily-salt rotation and SETNX race safety."""

from __future__ import annotations

import asyncio

import pytest

from app.core import privacy, redis_client


@pytest.fixture(autouse=True)
def _reset_salt_state():
    """Ensure each test starts with a clean Redis client + local cache."""
    redis_client._client = None
    privacy._local_salt_cache.clear()
    yield
    redis_client._client = None
    privacy._local_salt_cache.clear()


@pytest.fixture()
def fake_redis():
    """Install a fakeredis async client into the module-level slot."""
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    redis_client._client = client
    yield client
    # Best-effort cleanup; the autouse fixture also resets the slot.


async def test_same_day_idempotent_with_redis(fake_redis):
    """Two sequential calls on the same UTC day yield the same Redis-backed salt."""
    s1 = await privacy.get_today_salt()
    s2 = await privacy.get_today_salt()
    assert s1 == s2
    assert len(s1) == 64  # token_hex(32) → 64 hex chars
    # The key was written with a TTL ≤ 48h.
    ttl = await fake_redis.ttl(f"ip_salt:{privacy._today_key()}")
    assert 0 < ttl <= privacy._SALT_TTL_SECONDS


async def test_self_host_fallback_idempotent():
    """With no Redis configured, the local cache returns the same salt twice."""
    assert redis_client.get_redis() is None
    s1 = await privacy.get_today_salt()
    s2 = await privacy.get_today_salt()
    assert s1 == s2
    assert len(s1) == 64
    assert privacy._local_salt_cache[privacy._today_key()] == s1


async def test_day_rollover_yields_different_salt(monkeypatch):
    """Faking a different UTC day forces a fresh salt (Redis disabled path)."""
    today_real = privacy._today_key()
    s_today = await privacy.get_today_salt()

    # Monkey-patch ``_today_key`` to simulate the next day.
    fake_day = "20991231" if today_real != "20991231" else "20990101"
    monkeypatch.setattr(privacy, "_today_key", lambda: fake_day)

    s_tomorrow = await privacy.get_today_salt()
    assert s_today != s_tomorrow
    assert len(s_tomorrow) == 64


async def test_concurrent_calls_converge_on_one_salt(fake_redis):
    """Two coroutines racing through SETNX must observe the same value."""
    s1, s2 = await asyncio.gather(privacy.get_today_salt(), privacy.get_today_salt())
    assert s1 == s2
