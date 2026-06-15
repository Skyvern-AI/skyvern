from __future__ import annotations

import re

from skyvern.forge.sdk.copilot.request_policy import RAW_SECRET_PATTERNS, contains_email_password_pair

_SECRET_WORDS = r"password|passwd|passcode|token|secret|api[_ -]?key|credential|bearer|authorization|otp|totp|mfa|2fa"
_SECRET_WORD_VALUE_RE = re.compile(rf"^(?:{_SECRET_WORDS})$", re.I)
_SENSITIVE_TARGET_RE = re.compile(rf"(?:{_SECRET_WORDS}|current-password|one-time-code)", re.I)
_DEFAULT_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,63}$")
# Positive allow-list: only obvious reusable lookup/search fields may persist typed text as a default.
_DEFAULT_FIELD_ALLOW_RE = re.compile(r"(?:search|query|sku|entity|product|part|item|model|coupon|promo)", re.I)
_DEFAULT_FIELD_DENY_RE = re.compile(
    r"(?:email|phone|address|dob|birth|ssn|social|account|card|routing|username|password|secret|token|auth)",
    re.I,
)


def typed_text_looks_secret(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    return (
        contains_email_password_pair(text)
        or _SECRET_WORD_VALUE_RE.fullmatch(text) is not None
        or any(pattern.search(text) for pattern in RAW_SECRET_PATTERNS)
    )


def should_reject_type_text_value(*, value: object, selector: str = "", intent: str = "") -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and (typed_text_looks_secret(value) or _SENSITIVE_TARGET_RE.search(f"{selector} {intent}") is not None)
    )


def safe_typed_default_value(
    value: object,
    *,
    selector: str = "",
    role: str = "",
    accessible_name: str = "",
) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    field_context = f"{selector} {role} {accessible_name}"
    if (
        not text
        or typed_text_looks_secret(text)
        or not _DEFAULT_TOKEN_RE.fullmatch(text)
        or _DEFAULT_FIELD_DENY_RE.search(field_context)
        or not _DEFAULT_FIELD_ALLOW_RE.search(field_context)
    ):
        return None
    return text
