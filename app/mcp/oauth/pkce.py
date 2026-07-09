"""PKCE (RFC 7636) S256 verifier.

OAuth 2.1 forbids ``code_challenge_method=plain`` — we only implement S256.
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def s256_challenge(code_verifier: str) -> str:
    """Compute ``BASE64URL-NOPAD(SHA256(code_verifier))`` per RFC 7636 §4.2."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_s256(code_verifier: str, code_challenge: str) -> bool:
    """Constant-time-compare a presented verifier against a stored challenge."""
    return hmac.compare_digest(s256_challenge(code_verifier), code_challenge)
