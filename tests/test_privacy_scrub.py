"""Phase 4.3: PII tripwire + 4 KB properties size cap unit tests."""

from __future__ import annotations

import logging
import uuid

from app.core.privacy import MAX_PROPERTIES_BYTES, scrub_properties


def test_drops_pii_key_preserves_others() -> None:
    scrubbed, dropped, oversized = scrub_properties({"email": "a@b", "country": "IT"})
    assert scrubbed == {"country": "IT"}
    assert dropped == ["email"]
    assert oversized is False


def test_oversized_payload_zeros_properties() -> None:
    # ~5 KB nested dict: a single key with a long string value comfortably
    # exceeds MAX_PROPERTIES_BYTES once JSON-encoded.
    big = {"blob": "x" * (MAX_PROPERTIES_BYTES + 1024)}
    scrubbed, dropped, oversized = scrub_properties(big)
    assert scrubbed == {}
    assert dropped == []
    assert oversized is True


def test_uppercase_key_dropped_case_insensitive() -> None:
    scrubbed, dropped, oversized = scrub_properties({"EMAIL": "x"})
    assert scrubbed == {}
    assert dropped == ["EMAIL"]  # original casing preserved
    assert oversized is False


def test_empty_input_noop() -> None:
    scrubbed, dropped, oversized = scrub_properties({})
    assert scrubbed == {}
    assert dropped == []
    assert oversized is False


def test_mixed_pii_and_clean_keys() -> None:
    scrubbed, dropped, oversized = scrub_properties(
        {"email": "x", "user_id": 1, "credit_card": "4242"}
    )
    assert scrubbed == {"user_id": 1}
    assert set(dropped) == {"email", "credit_card"}
    assert oversized is False


def test_caplog_emits_structured_warning_on_drop(caplog) -> None:
    project_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    with caplog.at_level(logging.WARNING, logger="app.core.privacy"):
        scrub_properties({"email": "leak@x.com", "plan": "pro"}, project_id=project_id)

    matches = [r for r in caplog.records if r.message == "pii_dropped"]
    assert matches, f"expected pii_dropped warning, got {[r.message for r in caplog.records]}"
    rec = matches[0]
    assert rec.levelno == logging.WARNING
    assert getattr(rec, "project_id", None) == str(project_id)
    assert getattr(rec, "keys", None) == ["email"]
