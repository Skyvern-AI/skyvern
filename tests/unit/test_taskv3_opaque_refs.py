"""Unit tests for Task V3 signed-URL masking and model-facing rendering (opaque_refs)."""

from __future__ import annotations

import time
from typing import Any, Callable

import pytest

from skyvern.forge.taskv3.opaque_refs import is_signed_url, mask_opaque_urls

SIGNED = (
    "https://files.example.test/uploads/a1b2c3d4e5f6/resume.pdf"
    "?token=eyJhbGciOiJIUzI1NiJ9.c2lnbmVk.Q29ycmVjdEhvcnNlQmF0dGVyeVN0YXBsZTAxMjM0NTY3ODk"
)
PLAIN = "https://portfolio.example.test/jo"
S3_STYLE_SIGNED = (
    "https://bucket.example.test/uploads/resume.pdf"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
)
HASH_ROUTE = "https://careers.example.test/#/jobs/software-engineer-2026"
ORDER_URL = "https://shop.example.test/orders?orderId=ORD2026AUG24X7Q1"
CAMPAIGN_URL = "https://jobs.example.test/apply?utm_source=newsletter&utm_campaign=q3_2026_apply_now"
AZURE_SAS_SIGNED = (
    "https://account.blob.example.test/container/file.pdf?sv=2024-01-01&sig=U8JZpDE0iGXlD6gNCFbaEPFjbD0kH8Oool8DklZD"
)
GCS_SIGNED = (
    "https://storage.example.test/bucket/obj"
    "?GoogleAccessId=x&Expires=1&Signature=OCj2ISaJiHkTj0rLGlkoMXGjtEkDnNfribxUdl7dXTPy"
)
CLOUDFRONT_SIGNED = (
    "https://cdn.example.test/video.mp4"
    "?Policy=eyJTdGF0ZW1lbnQiOm51bGx9"
    "&Signature=a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    "&Key-Pair-Id=APKAEXAMPLE0123456789"
)
POLICY_NUMBER_URL = "https://ins.example.test/claim?policyNumber=POL2026AUG1234567X"
MONKEYVAL_URL = "https://zoo.example.test/exhibit?monkeyval=abcdef0123456789xyz"

# LIVENESS corpus: signed-URL shapes that must mask but previously slipped through unmasked.
JWT_IN_PATH_URL = (
    "https://files.example.test/download/"
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    "/resume.pdf"
)
HEX_BLOB_IN_PATH_URL = (
    "https://files.example.test/download/9f2c8a1b4d6e0f3a7c5b2d8e1f4a6c9b0d3e7f2a5c8b1d4e6f9a0c3e7b2d5f8a/resume.pdf"
)
OFF_ALLOWLIST_KEY_BASE64_URL = (
    "https://files.example.test/download/resume.pdf?t=QW5vdGhlclJhbmRvbUJhc2U2NFN0cmluZzEyMzQ1Njc4OTA"
)
OFF_ALLOWLIST_KEY_HEX_URL = (
    "https://files.example.test/download/resume.pdf?dl=9f2c8a1b4d6e0f3a7c5b2d8e1f4a6c9b0d3e7f2a5c8b1d4e6f9a0c3e7b2d5f8a"
)
# A high-entropy base64-style blob (the same shape already masked as a non-allowlisted-key QUERY
# value above) must be masked just as reliably when it sits bare in the PATH, with no key at all.
HIGH_ENTROPY_BLOB_IN_PATH_URL = (
    "https://files.example.test/download/QW5vdGhlclJhbmRvbUJhc2U2NFN0cmluZzEyMzQ1Njc4OTA/resume.pdf"
)

# FALSE-POSITIVE corpus: normal URLs the model must still see in full, unmasked.
LONG_LOWERCASE_REDIRECT_URL = (
    "https://jobs.example.test/apply?returnTo=careers.example.test/jobs/"
    "senior-software-engineer-remote-2026-full-time-apply-now"
)
LONG_SLUG_PATH_URL = "https://careers.example.test/jobs/senior-software-engineer-remote-2026-full-time-apply-now"
LONG_UPPERCASE_TRACKING_CODE_URL = "https://shop.example.test/orders?trackingCode=ORD2026AUGSHIPMENTBATCH0001234567890X"
# Digits are technically hex characters, but a long padded numeric identifier is not a hex-encoded
# signature: it must not be masked just for being 32+ digits with no hex letter in sight.
LONG_NUMERIC_TRACKING_NUMBER_URL = "https://shop.example.test/orders?trackingNumber=12345678901234567890123456789012"
# Three dot-separated numeric runs are shaped like a JWT's segments but are not one - a real JWT
# header always base64url-decodes to a JSON object, so it always starts "eyJ".
NUMERIC_DOT_TRIPLET_URL = "https://files.example.test/artifacts/1735689600000.1735689660000.1735689720000/build.zip"
# An ordinary timestamped snapshot filename also happens to look like 3 dot-separated 10+-char
# segments, but has no JWT header shape either.
TIMESTAMPED_FILENAME_URL = "https://files.example.test/backups/db-snapshot.2026-08-20T120000.compressed-archive.tar"
# A camelCase business identifier (order ref, campaign code) mixes upper/lower/digit the way base64
# does, but - unlike random byte output - a real identifier concatenates whole fields (a literal
# year, a padded batch number), producing a long run of consecutive digits that base64/hex output
# essentially never does by chance, so it must not be masked just for having all three character
# classes present.
CAMEL_CASE_ORDER_REF_URL = "https://shop.example.test/orders?orderRef=ORD2026AugShipmentBatch0001234567890X"
# A real random signature can still contain an incidental run of same-case letters (this one has
# "GHIJKLmnopQRSTuvwxYZ"'s 6-letter uppercase-then-lowercase-then-uppercase pattern) - a same-case-run
# rejection would silently miss ~40% of real signatures at this length, so that must not be the
# discriminator (this is a regression guard for a same-case-run heuristic that was tried and reverted).
REALISTIC_SIGNATURE_WITH_SAME_CASE_RUN_URL = (
    "https://shop.example.test/download?ref=aBcDefGHIJKLmnopQRSTuvwxYZ12aBcDefGHIJKL"
)
# A legitimate signing blob can itself contain a "/" character (standard base64's own alphabet), which
# must survive percent-encoded (%2F) as ONE path segment. Decoding before splitting on "/" would turn
# it into a literal separator and shred one 41-char blob into two under-32-char fragments that
# individually evade detection.
PERCENT_ENCODED_SLASH_IN_PATH_BLOB_URL = (
    "https://files.example.test/download/k7QWmPzXvL%2FdcRTfBhNjYqAoEuHiKlZsGw1MnO4pC/resume.pdf"
)
# "+" is a legal, unreserved path character per RFC 3986 (unlike in a query string, where it means
# space under form-encoding) - a signing blob containing a literal, un-percent-encoded "+" must still
# be recognized, not corrupted into a space by a decoder meant for query strings.
UNENCODED_PLUS_IN_PATH_BLOB_URL = (
    "https://files.example.test/download/k7QWmPzXvL+dcRTfBhNjYqAoEuHiKlZsGw1MnO4pC/resume.pdf"
)
# A capability-style signed URL can carry its whole opaque token as a bare query "key" with no
# "=value" at all - the token IS the entire query string.
BLOB_AS_BARE_QUERY_KEY_URL = "https://files.example.test/download?QW5vdGhlclJhbmRvbUJhc2U2NFN0cmluZzEyMzQ1Njc4OTA"
# The same blob-as-key shape, but with a trivial "=1" value tacked on - the value alone is too short
# to qualify, so the key itself must still be checked.
BLOB_AS_QUERY_KEY_WITH_TRIVIAL_VALUE_URL = (
    "https://files.example.test/download?QW5vdGhlclJhbmRvbUJhc2U2NFN0cmluZzEyMzQ1Njc4OTA=1"
)
# A JWT can be embedded WITHIN a larger query value (e.g. an echoed "Bearer <jwt>" header) rather
# than being the value's entire content - detection must match the same way it does in the path
# (substring search), not require the JWT to be the whole decoded value.
JWT_EMBEDDED_IN_QUERY_VALUE_URL = (
    "https://files.example.test/download?t=Bearer%20"
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
# The blob can be fused directly into a KEY that also contains a signing word (e.g. "token-<blob>"),
# bare or with a trivial value - the signing-key match must not short-circuit past checking whether
# the key's own text still carries an unrelated, unmasked blob.
SIGNING_WORD_FUSED_WITH_BLOB_KEY_URL = (
    "https://files.example.test/download?token-QW5vdGhlclJhbmRvbUJhc2U2NFN0cmluZzEyMzQ1Njc4OTA"
)
SIGNING_WORD_FUSED_WITH_BLOB_KEY_TRIVIAL_VALUE_URL = (
    "https://files.example.test/download?token-QW5vdGhlclJhbmRvbUJhc2U2NFN0cmluZzEyMzQ1Njc4OTA=1"
)


def _assert_signed_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["resume_url"]
    assert token != SIGNED
    assert token.startswith("opaque_url_")
    assert refs[token] == SIGNED


def _assert_plain_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked["portfolio_url"] == PLAIN
    assert refs == {}


def _assert_nested(masked: Any, refs: dict[str, str]) -> None:
    token, plain = masked["applicant"]["links"]
    assert token != SIGNED
    assert token.startswith("opaque_url_")
    assert plain == PLAIN
    assert refs[token] == SIGNED


def _assert_non_str_and_none_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked == {"count": 3, "active": True, "note": None}
    assert refs == {}


def _assert_none(masked: Any, refs: dict[str, str]) -> None:
    assert masked is None
    assert refs == {}


def _assert_s3_style_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["resume_url"]
    assert token != S3_STYLE_SIGNED
    assert token.startswith("opaque_url_")
    assert refs[token] == S3_STYLE_SIGNED


def _assert_hash_route_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked["job_url"] == HASH_ROUTE
    assert refs == {}


def _assert_order_url_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked["url"] == ORDER_URL
    assert refs == {}


def _assert_campaign_url_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked["url"] == CAMPAIGN_URL
    assert refs == {}


def _assert_azure_sas_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != AZURE_SAS_SIGNED
    assert token.startswith("opaque_url_")
    assert refs[token] == AZURE_SAS_SIGNED


def _assert_gcs_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != GCS_SIGNED
    assert token.startswith("opaque_url_")
    assert refs[token] == GCS_SIGNED


def _assert_cloudfront_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != CLOUDFRONT_SIGNED
    assert token.startswith("opaque_url_")
    assert refs[token] == CLOUDFRONT_SIGNED


def _assert_policy_number_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked["url"] == POLICY_NUMBER_URL
    assert refs == {}


def _assert_monkeyval_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked["url"] == MONKEYVAL_URL
    assert refs == {}


def _assert_jwt_in_path_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != JWT_IN_PATH_URL
    assert token.startswith("opaque_url_")
    assert refs[token] == JWT_IN_PATH_URL


def _assert_hex_blob_in_path_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != HEX_BLOB_IN_PATH_URL
    assert token.startswith("opaque_url_")
    assert refs[token] == HEX_BLOB_IN_PATH_URL


def _assert_off_allowlist_key_base64_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != OFF_ALLOWLIST_KEY_BASE64_URL
    assert token.startswith("opaque_url_")
    assert refs[token] == OFF_ALLOWLIST_KEY_BASE64_URL


def _assert_off_allowlist_key_hex_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != OFF_ALLOWLIST_KEY_HEX_URL
    assert token.startswith("opaque_url_")
    assert refs[token] == OFF_ALLOWLIST_KEY_HEX_URL


def _assert_long_lowercase_redirect_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked["url"] == LONG_LOWERCASE_REDIRECT_URL
    assert refs == {}


def _assert_long_slug_path_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked["url"] == LONG_SLUG_PATH_URL
    assert refs == {}


def _assert_long_uppercase_tracking_code_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked["url"] == LONG_UPPERCASE_TRACKING_CODE_URL
    assert refs == {}


def _assert_long_numeric_tracking_number_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked["url"] == LONG_NUMERIC_TRACKING_NUMBER_URL
    assert refs == {}


def _assert_high_entropy_blob_in_path_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != HIGH_ENTROPY_BLOB_IN_PATH_URL
    assert token.startswith("opaque_url_")
    assert refs[token] == HIGH_ENTROPY_BLOB_IN_PATH_URL


def _assert_numeric_dot_triplet_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked["url"] == NUMERIC_DOT_TRIPLET_URL
    assert refs == {}


def _assert_timestamped_filename_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked["url"] == TIMESTAMPED_FILENAME_URL
    assert refs == {}


def _assert_camel_case_order_ref_untouched(masked: Any, refs: dict[str, str]) -> None:
    assert masked["url"] == CAMEL_CASE_ORDER_REF_URL
    assert refs == {}


def _assert_realistic_signature_with_same_case_run_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != REALISTIC_SIGNATURE_WITH_SAME_CASE_RUN_URL
    assert token.startswith("opaque_url_")
    assert refs[token] == REALISTIC_SIGNATURE_WITH_SAME_CASE_RUN_URL


def _assert_percent_encoded_slash_in_path_blob_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != PERCENT_ENCODED_SLASH_IN_PATH_BLOB_URL
    assert token.startswith("opaque_url_")
    assert refs[token] == PERCENT_ENCODED_SLASH_IN_PATH_BLOB_URL


def _assert_unencoded_plus_in_path_blob_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != UNENCODED_PLUS_IN_PATH_BLOB_URL
    assert token.startswith("opaque_url_")
    assert refs[token] == UNENCODED_PLUS_IN_PATH_BLOB_URL


def _assert_blob_as_bare_query_key_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != BLOB_AS_BARE_QUERY_KEY_URL
    assert token.startswith("opaque_url_")
    assert refs[token] == BLOB_AS_BARE_QUERY_KEY_URL


def _assert_blob_as_query_key_with_trivial_value_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != BLOB_AS_QUERY_KEY_WITH_TRIVIAL_VALUE_URL
    assert token.startswith("opaque_url_")
    assert refs[token] == BLOB_AS_QUERY_KEY_WITH_TRIVIAL_VALUE_URL


def _assert_jwt_embedded_in_query_value_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != JWT_EMBEDDED_IN_QUERY_VALUE_URL
    assert token.startswith("opaque_url_")
    assert refs[token] == JWT_EMBEDDED_IN_QUERY_VALUE_URL


def _assert_signing_word_fused_with_blob_key_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != SIGNING_WORD_FUSED_WITH_BLOB_KEY_URL
    assert token.startswith("opaque_url_")
    assert refs[token] == SIGNING_WORD_FUSED_WITH_BLOB_KEY_URL


def _assert_signing_word_fused_with_blob_key_trivial_value_masked(masked: Any, refs: dict[str, str]) -> None:
    token = masked["url"]
    assert token != SIGNING_WORD_FUSED_WITH_BLOB_KEY_TRIVIAL_VALUE_URL
    assert token.startswith("opaque_url_")
    assert refs[token] == SIGNING_WORD_FUSED_WITH_BLOB_KEY_TRIVIAL_VALUE_URL


@pytest.mark.parametrize(
    "parameters, check",
    [
        ({"resume_url": SIGNED}, _assert_signed_masked),
        ({"portfolio_url": PLAIN}, _assert_plain_untouched),
        ({"applicant": {"links": [SIGNED, PLAIN]}}, _assert_nested),
        ({"count": 3, "active": True, "note": None}, _assert_non_str_and_none_untouched),
        (None, _assert_none),
        ({"resume_url": S3_STYLE_SIGNED}, _assert_s3_style_masked),
        ({"job_url": HASH_ROUTE}, _assert_hash_route_untouched),
        ({"url": ORDER_URL}, _assert_order_url_untouched),
        ({"url": CAMPAIGN_URL}, _assert_campaign_url_untouched),
        ({"url": AZURE_SAS_SIGNED}, _assert_azure_sas_masked),
        ({"url": GCS_SIGNED}, _assert_gcs_masked),
        ({"url": CLOUDFRONT_SIGNED}, _assert_cloudfront_masked),
        ({"url": POLICY_NUMBER_URL}, _assert_policy_number_untouched),
        ({"url": MONKEYVAL_URL}, _assert_monkeyval_untouched),
        ({"url": JWT_IN_PATH_URL}, _assert_jwt_in_path_masked),
        ({"url": HEX_BLOB_IN_PATH_URL}, _assert_hex_blob_in_path_masked),
        ({"url": OFF_ALLOWLIST_KEY_BASE64_URL}, _assert_off_allowlist_key_base64_masked),
        ({"url": OFF_ALLOWLIST_KEY_HEX_URL}, _assert_off_allowlist_key_hex_masked),
        ({"url": LONG_LOWERCASE_REDIRECT_URL}, _assert_long_lowercase_redirect_untouched),
        ({"url": LONG_SLUG_PATH_URL}, _assert_long_slug_path_untouched),
        ({"url": LONG_UPPERCASE_TRACKING_CODE_URL}, _assert_long_uppercase_tracking_code_untouched),
        ({"url": LONG_NUMERIC_TRACKING_NUMBER_URL}, _assert_long_numeric_tracking_number_untouched),
        ({"url": HIGH_ENTROPY_BLOB_IN_PATH_URL}, _assert_high_entropy_blob_in_path_masked),
        ({"url": NUMERIC_DOT_TRIPLET_URL}, _assert_numeric_dot_triplet_untouched),
        ({"url": TIMESTAMPED_FILENAME_URL}, _assert_timestamped_filename_untouched),
        ({"url": CAMEL_CASE_ORDER_REF_URL}, _assert_camel_case_order_ref_untouched),
        (
            {"url": REALISTIC_SIGNATURE_WITH_SAME_CASE_RUN_URL},
            _assert_realistic_signature_with_same_case_run_masked,
        ),
        (
            {"url": PERCENT_ENCODED_SLASH_IN_PATH_BLOB_URL},
            _assert_percent_encoded_slash_in_path_blob_masked,
        ),
        ({"url": UNENCODED_PLUS_IN_PATH_BLOB_URL}, _assert_unencoded_plus_in_path_blob_masked),
        ({"url": BLOB_AS_BARE_QUERY_KEY_URL}, _assert_blob_as_bare_query_key_masked),
        (
            {"url": BLOB_AS_QUERY_KEY_WITH_TRIVIAL_VALUE_URL},
            _assert_blob_as_query_key_with_trivial_value_masked,
        ),
        ({"url": JWT_EMBEDDED_IN_QUERY_VALUE_URL}, _assert_jwt_embedded_in_query_value_masked),
        (
            {"url": SIGNING_WORD_FUSED_WITH_BLOB_KEY_URL},
            _assert_signing_word_fused_with_blob_key_masked,
        ),
        (
            {"url": SIGNING_WORD_FUSED_WITH_BLOB_KEY_TRIVIAL_VALUE_URL},
            _assert_signing_word_fused_with_blob_key_trivial_value_masked,
        ),
    ],
)
def test_mask_opaque_urls(parameters: dict[str, Any] | None, check: Callable[[Any, dict[str, str]], None]) -> None:
    original = dict(parameters) if parameters is not None else None
    refs = mask_opaque_urls(parameters)
    check(refs.masked, refs.refs)
    assert parameters == original  # input is never mutated


def test_chain_calls_inner_on_raw_text_and_resolves_tokens_after() -> None:
    refs = mask_opaque_urls({"resume_url": SIGNED})
    token = next(iter(refs.refs))
    calls: list[str] = []

    def inner(text: str) -> str:
        calls.append(text)
        return text.upper() if text == "plain text" else text

    resolver = refs.chain(inner)

    assert resolver("plain text") == "PLAIN TEXT"
    assert resolver(token) == SIGNED
    assert resolver(f"see {token} for details") == f"see {SIGNED} for details"

    # inner saw every raw call, including the token itself, but never the resolved URL.
    assert calls == ["plain text", token, f"see {token} for details"]
    assert all(SIGNED not in call for call in calls)


def test_chain_resolves_token_even_when_inner_raises() -> None:
    refs = mask_opaque_urls({"resume_url": SIGNED})
    token = next(iter(refs.refs))

    def inner(text: str) -> str:
        raise RuntimeError("credential resolver unavailable")

    resolver = refs.chain(inner)

    assert resolver(token) == SIGNED
    assert resolver("plain text") == "plain text"


def test_signature_without_a_digit_is_still_masked_under_a_signing_key() -> None:
    url = "https://account.blob.example.test/c/f.pdf?sv=2024-01-01&sig=AbCdEfGhIjKlMnOpQrStUvWxYz"
    refs = mask_opaque_urls({"url": url})
    assert refs.masked["url"].startswith("opaque_url_")
    assert refs.refs[refs.masked["url"]] == url


def test_signed_url_embedded_in_prose_is_masked_and_resolves_back() -> None:
    prose = f"Upload the resume from {SIGNED} before submitting, then open {PLAIN}."
    refs = mask_opaque_urls({"task_data": prose})
    token = next(iter(refs.refs))
    assert refs.masked["task_data"] == f"Upload the resume from {token} before submitting, then open {PLAIN}."
    assert refs.resolve(refs.masked["task_data"]) == prose


def test_signed_url_in_prose_keeps_trailing_punctuation_outside_the_token() -> None:
    prose = f"Upload it ({SIGNED}), then submit; see {SIGNED}."
    refs = mask_opaque_urls({"task_data": prose})
    token = next(iter(refs.refs))
    assert refs.masked["task_data"] == f"Upload it ({token}), then submit; see {token}."
    assert refs.resolve(refs.masked["task_data"]) == prose


def test_signed_url_in_markdown_code_span_or_uppercase_scheme_is_masked() -> None:
    upper = "HTTPS://files.example.test/uploads/x.pdf?token=eyJhbGciOiJIUzI1NiJ9c2lnbmVkQ29ycmVjdEhvcnNl"
    prose = f"Use `{SIGNED}` or {upper} here."
    refs = mask_opaque_urls({"task_data": prose})
    assert SIGNED not in refs.masked["task_data"] and upper not in refs.masked["task_data"]
    assert refs.masked["task_data"].startswith("Use `opaque_url_") and "` or opaque_url_" in refs.masked["task_data"]
    assert refs.resolve(refs.masked["task_data"]) == prose


def test_is_signed_url_true_for_jwt_or_hex_blob_in_path() -> None:
    assert is_signed_url(JWT_IN_PATH_URL) is True
    assert is_signed_url(HEX_BLOB_IN_PATH_URL) is True


def test_is_signed_url_true_for_signing_shaped_value_under_non_allowlisted_key() -> None:
    assert is_signed_url(OFF_ALLOWLIST_KEY_BASE64_URL) is True
    assert is_signed_url(OFF_ALLOWLIST_KEY_HEX_URL) is True


def test_is_signed_url_false_for_long_benign_path_and_query_shapes() -> None:
    assert is_signed_url(LONG_LOWERCASE_REDIRECT_URL) is False
    assert is_signed_url(LONG_SLUG_PATH_URL) is False
    assert is_signed_url(LONG_UPPERCASE_TRACKING_CODE_URL) is False
    assert is_signed_url(LONG_NUMERIC_TRACKING_NUMBER_URL) is False


def test_is_signed_url_false_for_long_numeric_id_in_path() -> None:
    url = "https://shop.example.test/orders/12345678901234567890123456789012/receipt"
    assert is_signed_url(url) is False


def test_is_signed_url_true_for_high_entropy_blob_bare_in_path() -> None:
    assert is_signed_url(HIGH_ENTROPY_BLOB_IN_PATH_URL) is True


def test_is_signed_url_false_for_numeric_dot_triplet_or_timestamped_filename() -> None:
    assert is_signed_url(NUMERIC_DOT_TRIPLET_URL) is False
    assert is_signed_url(TIMESTAMPED_FILENAME_URL) is False


def test_is_signed_url_false_for_camel_case_business_identifier() -> None:
    assert is_signed_url(CAMEL_CASE_ORDER_REF_URL) is False


def test_is_signed_url_true_for_realistic_signature_with_incidental_same_case_run() -> None:
    assert is_signed_url(REALISTIC_SIGNATURE_WITH_SAME_CASE_RUN_URL) is True


def test_is_signed_url_true_for_blob_with_percent_encoded_slash_in_path() -> None:
    assert is_signed_url(PERCENT_ENCODED_SLASH_IN_PATH_BLOB_URL) is True


def test_is_signed_url_true_for_blob_with_unencoded_plus_in_path() -> None:
    assert is_signed_url(UNENCODED_PLUS_IN_PATH_BLOB_URL) is True


def test_is_signed_url_true_for_blob_as_bare_or_trivial_valued_query_key() -> None:
    assert is_signed_url(BLOB_AS_BARE_QUERY_KEY_URL) is True
    assert is_signed_url(BLOB_AS_QUERY_KEY_WITH_TRIVIAL_VALUE_URL) is True


def test_is_signed_url_true_for_jwt_embedded_in_query_value() -> None:
    assert is_signed_url(JWT_EMBEDDED_IN_QUERY_VALUE_URL) is True


def test_is_signed_url_true_for_signing_word_fused_with_blob_key() -> None:
    assert is_signed_url(SIGNING_WORD_FUSED_WITH_BLOB_KEY_URL) is True
    assert is_signed_url(SIGNING_WORD_FUSED_WITH_BLOB_KEY_TRIVIAL_VALUE_URL) is True


def test_is_signed_url_does_not_take_quadratic_time_on_repeated_jwt_header_prefix() -> None:
    # A path segment that's nothing but "eyJ" repeated has no literal "." to terminate the header
    # match, forcing the regex to backtrack the header quantifier at every "eyJ" occurrence. An
    # unbounded quantifier makes that backtrack cost scale with segment length, so total cost is
    # quadratic in the segment length - confirmed unfixed: ~0.03s/0.11s/0.44s at 10/20/40KB (~4x per
    # 2x size). A bounded quantifier keeps the backtrack cost constant per occurrence, so total cost
    # stays linear - this should complete in well under a second even at 100KB.
    adversarial_segment = "eyJ" * 33_000  # ~99KB, no dots anywhere
    url = f"https://files.example.test/download/{adversarial_segment}/resume.pdf"
    start = time.perf_counter()
    is_signed_url(url)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"took {elapsed:.2f}s - JWT regex backtracking is not bounded"


def test_malformed_url_like_prose_does_not_raise() -> None:
    prose = "see http://[invalid?token=abcdefghijklmnop for details"
    refs = mask_opaque_urls({"task_data": prose, "u": "http://[invalid?token=abcdefghijklmnop"})
    assert refs.masked == {"task_data": prose, "u": "http://[invalid?token=abcdefghijklmnop"}
    assert refs.refs == {}
