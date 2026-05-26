from __future__ import annotations

from skyvern.forge.sdk.copilot.signature import (
    COMPARE_CAP,
    compute_signature,
    normalize_for_compare,
)


def test_normalize_strips_urls() -> None:
    a = normalize_for_compare("See https://example.com/a/b for details.")
    b = normalize_for_compare("See https://other.example.org/x for details.")
    assert a == b


def test_normalize_strips_skyvern_ids() -> None:
    a = normalize_for_compare("Block `block_1` failed during run wr_abc123.")
    b = normalize_for_compare("Block `block_44` failed during run wr_xyz999.")
    assert a == b


def test_normalize_strips_numbers_and_dates() -> None:
    a = normalize_for_compare("Run took 1234 ms and completed at 2026-05-22T10:00:00Z.")
    b = normalize_for_compare("Run took 9876 ms and completed at 2026-05-21T18:30:00Z.")
    assert a == b


def test_normalize_caps_to_compare_cap() -> None:
    body = "a " * 10_000
    assert len(normalize_for_compare(body)) <= COMPARE_CAP


def test_signature_matches_for_variant_inputs() -> None:
    a = "The file is in the Artifacts section. URL: https://example.com/foo/123"
    b = "The file is in the Artifacts section. URL: https://other.io/baz/9999"
    assert compute_signature(a) == compute_signature(b)


def test_signature_differs_for_distinct_text() -> None:
    a = "The file is in the Artifacts section."
    b = "I have emailed the downloaded file to your account."
    assert compute_signature(a) != compute_signature(b)


def test_signature_is_16_hex_chars() -> None:
    sig = compute_signature("hello")
    assert len(sig) == 16
    assert all(c in "0123456789abcdef" for c in sig)


def test_normalize_redacts_secrets_before_compare() -> None:
    a = normalize_for_compare("Use password: hunter2abc to sign in.")
    b = normalize_for_compare("Use password: secret9999 to sign in.")
    assert a == b
