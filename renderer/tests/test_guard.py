"""Tests for the renderer URL guard (standard library only, no browser)."""

import socket

import guard
import pytest
from guard import (
    MAX_DEVICE_PIXELS,
    CheckedTarget,
    GuardError,
    clip_height,
    is_login_wall,
    is_public_ip,
    launch_args,
    validate_target_url,
)


def _fake_getaddrinfo(mapping: dict[str, list[str]]):
    def fake(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in mapping[host]]

    return fake


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5:8000/admin",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0/",
    ],
)
def test_rejects_private_ip(url):
    with pytest.raises(GuardError) as exc:
        validate_target_url(url)
    assert exc.value.status == 403


def test_rejects_private_ip_behind_hostname(monkeypatch):
    # A public-looking name that resolves to one public and one private
    # address must be refused: every resolved address has to be public.
    monkeypatch.setattr(
        guard.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"mixed.example": ["93.184.216.34", "10.1.2.3"]}),
    )
    with pytest.raises(GuardError) as exc:
        validate_target_url("https://mixed.example/")
    assert exc.value.status == 403


def test_accepts_public_host(monkeypatch):
    monkeypatch.setattr(
        guard.socket, "getaddrinfo", _fake_getaddrinfo({"site.example": ["93.184.216.34"]})
    )
    target = validate_target_url("https://site.example/a?b=1")
    assert target == CheckedTarget(
        url="https://site.example/a?b=1", host="site.example", ip="93.184.216.34"
    )


def test_launch_args_pin_the_checked_ip(monkeypatch):
    # The browser must use the address the guard checked, not its own lookup.
    monkeypatch.setattr(
        guard.socket,
        "getaddrinfo",
        _fake_getaddrinfo({"site.example": ["2606:2800:220:1::1", "93.184.216.34"]}),
    )
    target = validate_target_url("https://site.example/")
    args = launch_args(target)
    rules = [a for a in args if a.startswith("--host-resolver-rules=")]
    assert rules == ["--host-resolver-rules=MAP site.example 93.184.216.34, EXCLUDE localhost"]
    assert "--block-new-web-contents" in args


def test_launch_args_bracket_ipv6_and_skip_ip_literals():
    v6 = launch_args(CheckedTarget(url="https://v6.example/", host="v6.example", ip="2606:2800::1"))
    assert "--host-resolver-rules=MAP v6.example [2606:2800::1], EXCLUDE localhost" in v6
    literal = launch_args(
        CheckedTarget(url="http://93.184.216.34/", host="93.184.216.34", ip="93.184.216.34")
    )
    assert not any(a.startswith("--host-resolver-rules") for a in literal)


def test_clip_height_keeps_pixel_budget():
    # Ordinary mobile page: only max_height applies.
    assert clip_height(390, 5569, 2, 6000) == 5569
    assert clip_height(390, 9000, 2, 6000) == 6000
    # Wide page at DPR 3: the budget cuts the height.
    h = clip_height(2000, 20000, 3, 20000)
    assert 2000 * 3 * h * 3 <= MAX_DEVICE_PIXELS
    assert 2000 * 3 * (h + 1) * 3 > MAX_DEVICE_PIXELS
    assert clip_height(2000, 1, 3, 6000) == 1


def test_unresolvable_host_is_rejected(monkeypatch):
    monkeypatch.setattr(guard.socket, "getaddrinfo", _fake_getaddrinfo({}))
    with pytest.raises(GuardError) as exc:
        validate_target_url("https://nope.example/")
    assert exc.value.status == 403


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/",
        "javascript:alert(1)",
        "chrome://settings",
        "data:text/html,hi",
        "example.com/no-scheme",
        "",
        "https://user:pw@example.com/",
    ],
)
def test_rejects_non_http_scheme(url):
    with pytest.raises(GuardError) as exc:
        validate_target_url(url)
    assert exc.value.status == 400


def test_is_public_ip():
    assert is_public_ip("93.184.216.34")
    assert is_public_ip("2606:2800:220:1:248:1893:25c8:1946")
    assert not is_public_ip("172.16.0.1")
    assert not is_public_ip("224.0.0.1")
    assert not is_public_ip("not-an-ip")


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
        "64:ff9b::a00:1",  # NAT64 wrapping 10.0.0.1
        "64:ff9b::5db8:d822",  # NAT64 wrapping a public address: refused too
        "64:ff9b:1::a00:1",  # local-use NAT64
        "::7f00:1",  # IPv4-compatible ::127.0.0.1
        "::a00:1",  # IPv4-compatible ::10.0.0.1
        "2002:7f00:1::1",  # 6to4 wrapping 127.0.0.1
    ],
)
def test_rejects_ipv6_forms_wrapping_ipv4(address):
    assert not is_public_ip(address)


def test_login_wall_detection_by_final_path():
    # Redirected from a protected page to a login route.
    assert is_login_wall("https://a.example/dashboard", "https://a.example/login", False)
    assert is_login_wall("https://a.example/app", "https://a.example/auth/sign-in?next=/app", False)
    assert is_login_wall("https://a.example/x", "https://a.example/users/SignIn", False)
    # Same path, no password field: not a login wall.
    assert not is_login_wall("https://a.example/", "https://a.example/", False)
    assert not is_login_wall("https://a.example/browse", "https://a.example/browse/", False)
    # Redirect to a non-login path.
    assert not is_login_wall("https://a.example/old", "https://a.example/new", False)
    # Asking for the login page itself is not a wall unless it has a password field.
    assert not is_login_wall("https://a.example/login", "https://a.example/login", False)


def test_login_wall_detection_by_password_input():
    assert is_login_wall("https://a.example/", "https://a.example/", True)
