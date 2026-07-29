"""Tests for the log-redaction filter (Phase 4.4).

Two layers are covered here:

* ``RedactingFilter`` — the pattern behaviour, exercised by synthesising
  ``logging.LogRecord`` instances and calling ``filter`` directly. We do not
  use pytest's ``caplog`` for these: it attaches its own ``LogCaptureHandler``
  and would add fixture-ordering surprises to assertions that are really
  about ``getMessage()`` interpolation and the ``record.args`` reset.
* ``install_log_redaction`` — the process-wide install, exercised end to end
  through a real root handler with a record from a *library* logger, which is
  the shape that leaked a bot token in production.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable

import pytest

from app.core.privacy import RedactingFilter, install_log_redaction


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


def test_redacts_telegram_bot_token(redactor: RedactingFilter) -> None:
    """Telegram bot tokens (``<digits>:<30+ chars>``) are redacted.

    The bot token used to sit in the /webhook/{token} URL path and would land
    verbatim in uvicorn access logs; the filter now scrubs it.
    """
    token = "1234567890:test-token-for-testing-only-AAaaBBbb"
    record = _make_record(f"set_webhook url=https://x.com/webhook token={token}")
    redactor.filter(record)
    out = record.getMessage()
    assert token not in out
    assert "[REDACTED]" in out


def test_redacts_mcp_bearer_token(redactor: RedactingFilter) -> None:
    """``mcp_<64 hex>`` bearer tokens are redacted."""
    token = "mcp_" + ("a" * 64)
    record = _make_record(f"authorization: Bearer {token}")
    redactor.filter(record)
    out = record.getMessage()
    assert token not in out
    assert "[REDACTED]" in out


def test_redaction_installed_by_app_import() -> None:
    """Importing ``app.main`` installs process-wide redaction."""
    # Importing for side-effects: ``create_app()`` runs at import time.
    import app.main  # noqa: F401
    from app.core.privacy import redaction_installed

    assert redaction_installed() is True


# ── End-to-end: third-party loggers must be covered too ───────────────────────


def _capture_root_output(log: Callable[[], None]) -> str:
    """Run *log* with a capturing handler on the root logger, return its output."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    try:
        log()
    finally:
        root.handlers, root.level = saved_handlers, saved_level
    return stream.getvalue()


def test_propagated_third_party_record_is_redacted() -> None:
    """A record from a library logger reaches the root handler redacted.

    This is the production shape: ``httpx`` logs the outbound Telegram URL,
    which carries the bot token, and propagates it to the root logger's
    handlers. A filter added to the *root logger* never runs on propagated
    records — only the originating logger's filters do — so redaction has to
    happen where every record passes: at construction.
    """
    install_log_redaction()
    token = "8637377571:AAEA-Ir6Gjn9rErCeSS25ne6leGm_O_6ln0"

    out = _capture_root_output(
        lambda: logging.getLogger("httpx").info(
            'HTTP Request: POST https://api.telegram.org/bot%s/getMe "HTTP/1.1 200 OK"',
            token,
        )
    )

    assert token not in out
    assert "[REDACTED]" in out


def test_install_log_redaction_is_idempotent() -> None:
    """Installing twice does not stack factories or double-redact."""
    install_log_redaction()
    first = logging.getLogRecordFactory()
    install_log_redaction()

    assert logging.getLogRecordFactory() is first

    out = _capture_root_output(lambda: logging.getLogger("httpx").info("plain message"))
    assert out.strip() == "plain message"
