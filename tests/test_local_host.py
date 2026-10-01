"""is_local_host: which Origin / URL values count as local development."""

import pytest

from app.services.events import is_local_host


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost",
        "http://localhost:5173",
        "https://LOCALHOST:3000",
        "http://app.localhost:8080",
        "http://127.0.0.1:8000",
        "http://127.1.2.3",
        "http://[::1]:3000",
        "http://0.0.0.0:8000",
        "http://localhost:5173/pricing?x=1",
        "localhost:3000",
    ],
)
def test_local_values(value):
    assert is_local_host(value) is True


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "null",
        "/pricing",
        "https://example.com",
        "https://localhost.example.com",
        "https://127.example.com",
        "https://mylocalhost.com",
        "http://192.168.1.10:3000",
        "http://[not-an-ip",
    ],
)
def test_non_local_values(value):
    assert is_local_host(value) is False
