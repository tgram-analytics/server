"""URL and network checks for the screenshot renderer.

Only the standard library is used here, so the checks can be unit-tested
without Playwright or a browser installed.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

# A final path that matches this after a redirect means the page asked for
# a login. Checked on the last path segment-ish text, case-insensitive.
_LOGIN_PATH_RE = re.compile(r"(login|signin|sign-in|auth)", re.IGNORECASE)


class GuardError(Exception):
    """A request the renderer refuses. ``status`` is the HTTP status to send."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def is_public_ip(address: str) -> bool:
    """Return True only for globally routable unicast addresses.

    Rejects loopback, private (RFC 1918 / ULA), link-local (cloud metadata
    at 169.254.169.254), CGNAT (100.64.0.0/10), multicast, reserved and
    unspecified addresses. IPv4-mapped IPv6 addresses are checked as IPv4.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast


def resolve_host(host: str, port: int | None = None) -> list[str]:
    """Return every address ``host`` resolves to (blocking DNS lookup)."""
    infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def host_is_public(host: str, port: int | None = None) -> bool:
    """True when ``host`` resolves and every resolved address is public."""
    if not host:
        return False
    try:
        addresses = resolve_host(host, port)
    except (OSError, UnicodeError):
        return False
    return bool(addresses) and all(is_public_ip(a) for a in addresses)


def validate_target_url(url: str) -> str:
    """Check the page URL the caller asked for.

    Returns the URL unchanged when it is an absolute http(s) URL whose host
    resolves only to public addresses. Raises :class:`GuardError` with 400
    for a malformed URL or scheme, 403 for a non-public host.
    """
    if not url or len(url) > 4096:
        raise GuardError(400, "url is required (max 4096 chars)")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise GuardError(400, "malformed url") from exc
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise GuardError(400, "only http and https urls are allowed")
    host = parts.hostname
    if not host:
        raise GuardError(400, "url has no host")
    if parts.username or parts.password:
        raise GuardError(400, "credentials in url are not allowed")
    if not host_is_public(host, port):
        raise GuardError(403, "host does not resolve to a public address")
    return url


def is_login_wall(requested_url: str, final_url: str, has_password_input: bool) -> bool:
    """Decide whether the rendered page is a login screen.

    True when the page shows a password field, or when navigation ended on a
    different path than requested and that path looks like a login route
    (``/login``, ``/signin``, ``/sign-in``, ``/auth/...``). Some login pages
    render the password field only after a click, so the path check matters.
    """
    if has_password_input:
        return True
    requested_path = urlsplit(requested_url).path or "/"
    final_path = urlsplit(final_url).path or "/"
    if final_path.rstrip("/") == requested_path.rstrip("/"):
        return False
    return bool(_LOGIN_PATH_RE.search(final_path))
