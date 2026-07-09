"""PKCE S256 + stateless CSRF token helpers."""

import time

from app.mcp.oauth.csrf import issue_csrf, verify_csrf
from app.mcp.oauth.pkce import s256_challenge, verify_s256

SECRET = "s" * 32


def test_pkce_roundtrip():
    challenge = s256_challenge("some-verifier-string")
    assert verify_s256("some-verifier-string", challenge)
    assert not verify_s256("wrong-verifier", challenge)


def test_csrf_roundtrip_bound_to_client():
    tok = issue_csrf(secret=SECRET, client_id="abc")
    assert verify_csrf(tok, secret=SECRET, client_id="abc")
    assert not verify_csrf(tok, secret=SECRET, client_id="OTHER")


def test_csrf_tamper_rejected():
    tok = issue_csrf(secret=SECRET, client_id="abc")
    assert not verify_csrf(tok + "x", secret=SECRET, client_id="abc")
    assert not verify_csrf("garbage", secret=SECRET, client_id="abc")


def test_csrf_expiry(monkeypatch):
    tok = issue_csrf(secret=SECRET, client_id="abc", ttl_seconds=1)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 5)
    assert not verify_csrf(tok, secret=SECRET, client_id="abc")
