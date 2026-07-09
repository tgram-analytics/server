"""Fixed-window in-process rate limiter."""

from app.mcp.oauth.rate_limit import RateLimiter


def test_allows_up_to_limit_then_blocks():
    rl = RateLimiter(limit=3, window_seconds=60)
    assert all(rl.allow("1.2.3.4", now=100.0) for _ in range(3))
    assert not rl.allow("1.2.3.4", now=100.0)


def test_window_resets():
    rl = RateLimiter(limit=1, window_seconds=60)
    assert rl.allow("k", now=100.0)
    assert not rl.allow("k", now=130.0)
    assert rl.allow("k", now=161.0)


def test_keys_independent():
    rl = RateLimiter(limit=1, window_seconds=60)
    assert rl.allow("a", now=100.0)
    assert rl.allow("b", now=100.0)
