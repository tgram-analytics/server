"""Stateless CSRF token for the authorize form.

``nonce.expiry.sig`` where ``sig = HMAC-SHA256(secret, nonce|expiry|client_id)``.
Stateless (no DB row, no session): the GET renders the token into a hidden
form field; the POST must return it. Binding client_id into the MAC means a
token minted for one client's page cannot authorize another client. Signed
with ``settings.secret_key`` (already required, 32+ chars).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

_DEFAULT_TTL = 15 * 60  # the admin may take a while to find their token


def _sign(secret: str, nonce: str, expiry: int, client_id: str) -> str:
    msg = f"{nonce}|{expiry}|{client_id}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def issue_csrf(*, secret: str, client_id: str, ttl_seconds: int = _DEFAULT_TTL) -> str:
    nonce = secrets.token_urlsafe(16)
    expiry = int(time.time()) + ttl_seconds
    return f"{nonce}.{expiry}.{_sign(secret, nonce, expiry, client_id)}"


def verify_csrf(token: str, *, secret: str, client_id: str) -> bool:
    try:
        nonce, expiry_s, sig = token.split(".", 2)
        expiry = int(expiry_s)
    except ValueError:
        return False
    if time.time() > expiry:
        return False
    return hmac.compare_digest(sig, _sign(secret, nonce, expiry, client_id))
