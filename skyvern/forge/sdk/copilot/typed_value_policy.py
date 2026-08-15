from __future__ import annotations

import re

from skyvern.forge.sdk.copilot.request_policy import RAW_SECRET_PATTERNS, contains_email_password_pair

_SECRET_WORDS = r"password|passwd|passcode|token|secret|api[_ -]?key|credential|bearer|authorization|otp|totp|mfa|2fa"
_SECRET_WORD_VALUE_RE = re.compile(rf"^(?:{_SECRET_WORDS})$", re.I)


def typed_text_looks_secret(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    return (
        contains_email_password_pair(text)
        or _SECRET_WORD_VALUE_RE.fullmatch(text) is not None
        or any(pattern.search(text) for pattern in RAW_SECRET_PATTERNS)
    )
