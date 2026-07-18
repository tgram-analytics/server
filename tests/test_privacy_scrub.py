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


def test_segment_match_drops_compound_keys() -> None:
    """Denylist terms match as whole segments inside compound keys."""
    dropped_examples = {
        "email": "x",
        "Email": "x",
        "user_email": "x",
        "userEmail": "x",
        "customer-email": "x",
        "auth_token": "x",
        "creditCard": "x",
        "credit_card_number": "x",
        "card_number": "x",
        "stripe.token": "x",
        "phone_model": "x",  # "phone" is a whole segment — err on dropping
    }
    scrubbed, dropped, oversized = scrub_properties(dict(dropped_examples))
    assert scrubbed == {}
    assert set(dropped) == set(dropped_examples)
    assert oversized is False


def test_segment_match_keeps_non_matching_keys() -> None:
    """Substrings that are not whole segments do not trigger a drop."""
    kept = {
        "tokens_count": 3,  # "tokens" != "token"
        "emailed": True,  # "emailed" != "email"
        "tokenize": "yes",
        "phones": 2,  # plural, single segment
        "plan": "pro",
        "amount": 42,
    }
    scrubbed, dropped, oversized = scrub_properties(dict(kept))
    assert scrubbed == kept
    assert dropped == []
    assert oversized is False


def test_multi_segment_denylist_term_matches_across_separators() -> None:
    """ "credit_card" matches as contiguous segments regardless of separator style."""
    scrubbed, dropped, _ = scrub_properties(
        {
            "creditCardNumber": "4242",
            "credit-card": "4242",
            "user_credit_card_last4": "4242",
            "credit_score": 700,  # "credit" alone is not a denylist term
        }
    )
    assert scrubbed == {"credit_score": 700}
    assert set(dropped) == {"creditCardNumber", "credit-card", "user_credit_card_last4"}


def test_mixed_separators_and_spaces() -> None:
    scrubbed, dropped, _ = scrub_properties(
        {"customer email": "x", "tax.id": "x", "card  number": "x", "safe key": 1}
    )
    assert scrubbed == {"safe key": 1}
    assert set(dropped) == {"customer email", "tax.id", "card  number"}


def test_dropped_keys_preserve_original_casing_compound() -> None:
    _, dropped, _ = scrub_properties({"UserEmail": "x", "AUTH-TOKEN": "x"})
    assert set(dropped) == {"UserEmail", "AUTH-TOKEN"}


def test_non_string_keys_do_not_crash() -> None:
    props = {1: "one", None: "none", 2.5: "float", "email": "x"}
    scrubbed, dropped, oversized = scrub_properties(props)  # type: ignore[arg-type]
    assert dropped == ["email"]
    assert scrubbed == {1: "one", None: "none", 2.5: "float"}
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
