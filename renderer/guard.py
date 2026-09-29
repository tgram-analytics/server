"""URL and network checks for the screenshot renderer.

Only the standard library is used here, so the checks can be unit-tested
without Playwright or a browser installed.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

# IPv6 ranges that embed an IPv4 address: NAT64 (well-known and local-use)
# and the deprecated IPv4-compatible form ::a.b.c.d.
_V6_WRAPS_V4 = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
    ipaddress.IPv6Network("::/96"),
)

# Chromium flags used for every launch. The per-request resolver rule is
# added by launch_args().
BASE_LAUNCH_ARGS = (
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-gpu",
    "--block-new-web-contents",
)

# Largest screenshot in device pixels (width * dpr * height * dpr). Bounds
# the bitmap in Chromium and in the API process that decodes it.
MAX_DEVICE_PIXELS = 24_000_000
MAX_DOC_WIDTH = 2000  # CSS px

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
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in _V6_WRAPS_V4):
            # NAT64 and IPv4-compatible forms carry an IPv4 address that the
            # ipaddress module still calls global. Refuse them outright.
            return False
        elif (
            ip.sixtofour is not None
            and not is_public_ip(str(ip.sixtofour))
            or ip.teredo is not None
            and not is_public_ip(str(ip.teredo[1]))
        ):
            return False
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


@dataclass(frozen=True)
class CheckedTarget:
    """A page URL whose host was resolved once and found public."""

    url: str
    host: str
    ip: str  # the checked address the browser must use for ``host``


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def validate_target_url(url: str) -> CheckedTarget:
    """Check the page URL the caller asked for.

    The host is resolved once; every address must be public. The returned
    :class:`CheckedTarget` carries the address to pin in the browser (see
    :func:`launch_args`), so the browser connects to the address that was
    checked. Raises :class:`GuardError` with 400 for a malformed URL or
    scheme, 403 for a non-public host.
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
    try:
        addresses = resolve_host(host, port)
    except (OSError, UnicodeError):
        addresses = []
    if not addresses or not all(is_public_ip(a) for a in addresses):
        raise GuardError(403, "host does not resolve to a public address")
    # Prefer IPv4: containers often have no IPv6 route.
    ip = next((a for a in addresses if ":" not in a), addresses[0])
    return CheckedTarget(url=url, host=host, ip=ip)


def launch_args(target: CheckedTarget) -> list[str]:
    """Chromium arguments for one screenshot of ``target``.

    ``--host-resolver-rules`` maps the page host to the checked address, so
    the browser cannot get a different DNS answer for the top-level page
    than the one the guard checked. Other hosts resolve on their own.
    """
    args = list(BASE_LAUNCH_ARGS)
    if not _is_ip_literal(target.host):
        ip = f"[{target.ip}]" if ":" in target.ip else target.ip
        args.append(f"--host-resolver-rules=MAP {target.host} {ip}, EXCLUDE localhost")
    return args


def clip_height(doc_width: int, doc_height: int, dpr: int, max_height: int) -> int:
    """Height in CSS px to capture, within ``max_height`` and the pixel budget."""
    width_px = max(1, doc_width) * dpr
    budget_rows = MAX_DEVICE_PIXELS // (width_px * dpr)
    return max(1, min(doc_height, max_height, budget_rows))


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
