from __future__ import annotations

import re

from email_validator import EmailNotValidError, validate_email

# The `token` keyword is guarded by negative lookbehinds so pagination cursors
# (next_token, page_token, continuation_token, ...), which are not credentials,
# aren't matched as secrets by either detection or redaction.
SECRET_KEYWORD_ASSIGNMENT_PATTERN = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9]))(?:[A-Za-z0-9]+_){0,8}"
    r"(?:password|passcode|api[_ -]?key|secret|bearer|authorization"
    r"|(?<!next_)(?<!prev_)(?<!previous_)(?<!page_)(?<!continuation_)(?<!cursor_)token)"
    # Consume an optional auth scheme word so `Authorization: Bearer <token>`
    # redacts the token, not just the scheme.
    r"\s*[:=]\s*(?:(?:bearer|basic|token|digest)\s+)?\S+",
    re.I,
)
RAW_SECRET_PATTERNS = (
    SECRET_KEYWORD_ASSIGNMENT_PATTERN,
    re.compile(
        r"\b(?:otp|totp|mfa|2fa|verification|auth(?:entication)? code)(?:\s+code)?\s*(?:is|[:=])?\s*\d{6,8}\b",
        re.I,
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_COLON_DELIMITED_SECRET_SEGMENT_SEPARATORS = (",", ";", "|")
_COLON_DELIMITED_SECRET_EDGE_CHARS = "\"'`()[]{}<>"


def _candidate_secret_segments(text: str) -> list[str]:
    segments: list[str] = []
    for raw_token in (text or "").split():
        token_segments = [raw_token]
        for separator in _COLON_DELIMITED_SECRET_SEGMENT_SEPARATORS:
            token_segments = [part for segment in token_segments for part in segment.split(separator)]
        segments.extend(segment.strip(_COLON_DELIMITED_SECRET_EDGE_CHARS) for segment in token_segments)
    return [segment for segment in segments if segment]


def _is_valid_account_row_email(value: str) -> bool:
    if any(char.isspace() for char in value) or "/" in value or ":" in value:
        return False
    try:
        validate_email(value, check_deliverability=False, test_environment=True)
    except EmailNotValidError:
        return False
    return True


def _looks_like_colon_delimited_secret_value(value: str) -> bool:
    if len(value) < 4:
        return False
    if any(char.isspace() for char in value):
        return False
    if any(char in value for char in ("/", "?", "#")):
        return False
    if value.isdigit() and len(value) <= 5:
        return False
    return True


def _email_password_pair_segments(text: str) -> list[str]:
    pairs: list[str] = []
    for segment in _candidate_secret_segments(text):
        email, separator, secret_value = segment.partition(":")
        if not separator:
            continue
        if _is_valid_account_row_email(email) and _looks_like_colon_delimited_secret_value(secret_value):
            pairs.append(segment)
    return pairs


def contains_email_password_pair(text: str) -> bool:
    # Privacy backstop for pasted account dumps. The request-policy classifier
    # owns ambiguous credential semantics; this parser keeps high-confidence raw
    # values out of model prompts and output surfaces without a broad regex rule.
    return bool(_email_password_pair_segments(text))


def redact_raw_secrets_for_prompt(text: str) -> str:
    redacted = text or ""
    for pattern in RAW_SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    for segment in _email_password_pair_segments(redacted):
        redacted = redacted.replace(segment, "[REDACTED_SECRET]")
    return redacted
