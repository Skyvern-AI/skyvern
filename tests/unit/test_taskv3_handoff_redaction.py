"""Unit tests for the Task V3 handoff secret-egress filtering
(skyvern/forge/taskv3/handoff_redaction.py)."""

from __future__ import annotations

import pytest

from skyvern.forge.taskv3.handoff_redaction import (
    MAX_HANDOFF_URL_CHARS,
    mask_signed_urls_in_text,
    sanitize_handoff_url,
)


def test_sanitize_handoff_url_strips_query_and_fragment() -> None:
    result = sanitize_handoff_url("https://example.test/path/to/page?foo=bar#section")
    assert result == "https://example.test/path/to/page"
    assert (
        sanitize_handoff_url("https://admin:s3cr3t@example.com:8443/dashboard?token=abc")
        == "https://example.com:8443/dashboard"
    )


@pytest.mark.parametrize("value", [None, "", "not a url", "just some prose about a page", "https://host:notaport/x"])
def test_sanitize_handoff_url_non_url_returns_none(value: str | None) -> None:
    assert sanitize_handoff_url(value) is None


def test_sanitize_handoff_url_truncates_overlong_path() -> None:
    long_path = "/segment" * ((MAX_HANDOFF_URL_CHARS + 50) // 8)
    result = sanitize_handoff_url(f"https://example.test{long_path}")
    assert result is not None
    assert len(result) <= MAX_HANDOFF_URL_CHARS
    assert result.endswith("…")


def test_sanitize_handoff_url_scrubs_token_shaped_path_segments() -> None:
    # A magic link carries its credential in the path; no secret registry knows it.
    token = "a3f9c2e8d1b4a7f6c9e2d5b8a1f4c7e0"
    result = sanitize_handoff_url(f"https://example.test/reset/{token}/confirm")
    assert result == "https://example.test/reset/***/confirm"
    assert sanitize_handoff_url("https://example.test/jobs/apply") == "https://example.test/jobs/apply"


def test_mask_signed_urls_in_text_masks_only_signing_shaped_urls() -> None:
    signed = "https://example.test/cb?signature=" + "A1b2" * 12
    text = f"stopped at {signed} after visiting https://example.test/plain"
    masked = mask_signed_urls_in_text(text)
    assert "[signed-url]" in masked
    assert "signature=" not in masked
    assert "https://example.test/plain" in masked  # nosemgrep: incomplete-url-substring-sanitization


def test_sanitize_handoff_url_scrubs_percent_encoded_token_segments() -> None:
    encoded = "qh%2Fx" + "A1b2" * 8  # decodes to a high-entropy base64-with-slash token
    result = sanitize_handoff_url(f"https://example.test/reset/{encoded}/done")
    assert result == "https://example.test/reset/***/done"


def test_mask_signed_urls_in_text_survives_trailing_punctuation() -> None:
    signed = "https://example.test/cb?signature=" + "A1b2" * 12
    masked = mask_signed_urls_in_text(f"stopped (see {signed}).")
    assert "signature=" not in masked
    assert masked.endswith("[signed-url]).")
