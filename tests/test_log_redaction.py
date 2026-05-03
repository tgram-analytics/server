"""Tests for the log-redaction filter (Phase 4.4).

We do not rely on pytest's ``caplog`` for the redaction assertions: caplog
attaches its own ``LogCaptureHandler`` and does NOT run filters added to the
root logger (filters there only gate propagation through the standard
handler chain). Instead we synthesise ``logging.LogRecord`` instances and
invoke ``RedactingFilter.filter`` directly — this exercises the real code
path (``getMessage()`` interpolation + ``record.args`` reset) without any
fixture-ordering surprises.
"""

from __future__ import annotations

import logging

import pytest

from app.core.privacy import RedactingFilter


def _make_record(msg: str, args: object = None) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=args,
        exc_info=None,
    )


@pytest.fixture()
def redactor() -> RedactingFilter:
    return RedactingFilter()


def test_redacts_sk_live_token_in_args(redactor: RedactingFilter) -> None:
    """``sk_live_*`` API keys passed through ``%s`` args are redacted."""
    record = _make_record("api key %s leaked", ("sk_live_AAA111",))
    assert redactor.filter(record) is True
    assert record.getMessage() == "api key [REDACTED] leaked"
    # ``args`` must be cleared so a re-format does not double-interpolate.
    assert record.args == ()


def test_redacts_proj_token(redactor: RedactingFilter) -> None:
    """``proj_<64 hex>`` tokens are redacted."""
    proj = "proj_" + ("a" * 64)
    record = _make_record(f"using project {proj} for ingest")
    redactor.filter(record)
    assert "[REDACTED]" in record.getMessage()
    assert proj not in record.getMessage()


def test_redacts_email_kv_pair_stops_at_whitespace(redactor: RedactingFilter) -> None:
    """``email=<value>`` is redacted up to whitespace; trailing fields survive.

    The mandated regex matches the entire ``email=user@x.com`` token (key +
    separator + value) and stops at whitespace, so the trailing
    ``country=IT`` field is preserved verbatim.
    """
    record = _make_record("email=user@x.com country=IT")
    redactor.filter(record)
    out = record.getMessage()
    assert "user@x.com" not in out
    assert "[REDACTED]" in out
    assert out.endswith("country=IT")


def test_redacts_password_quoted_value(redactor: RedactingFilter) -> None:
    """``password: "hunter2"`` form is recognised; ``[REDACTED]`` substring present."""
    record = _make_record('password: "hunter2"')
    redactor.filter(record)
    assert "[REDACTED]" in record.getMessage()
    assert "hunter2" not in record.getMessage()


def test_redacts_sk_test_token() -> None:
    """``sk_test_*`` tokens are redacted just like ``sk_live_*``."""
    redactor = RedactingFilter()
    record = _make_record("got token sk_test_ABCdef123")
    redactor.filter(record)
    assert "sk_test_ABCdef123" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_filter_installed_on_root_logger() -> None:
    """Importing ``app.main`` installs a ``RedactingFilter`` on the root logger."""
    # Importing for side-effects: ``create_app()`` runs at import time.
    import app.main  # noqa: F401

    assert any(type(f).__name__ == "RedactingFilter" for f in logging.getLogger().filters)
