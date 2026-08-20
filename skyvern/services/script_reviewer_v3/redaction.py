import html
import json
from urllib.parse import quote, quote_plus

from skyvern.forge.sdk.copilot.secret_scrub import MIN_PERSISTED_REDACTION_LENGTH, REDACTED_SECRET_PLACEHOLDER


def redact_sensitive_value(text: str, value: str | None) -> str:
    if not value:
        return text

    variants = {
        value,
        html.escape(value, quote=False),
        html.escape(value, quote=True),
        quote(value, safe=""),
        quote_plus(value, safe=""),
    }
    variants.update(json.dumps(item, ensure_ascii=False)[1:-1] for item in tuple(variants))
    for variant in sorted(variants, key=len, reverse=True):
        text = text.replace(variant, REDACTED_SECRET_PLACEHOLDER)
    return text


def redact_sensitive_content(text: str, value: str | None) -> str:
    """Floored variant, for text that is written to storage.

    Only persistence writes take the floor, because substring-replacing a short value corrupts what
    is stored. Disclosure channels — prompt, tool results, DOM, logs — must use
    ``redact_sensitive_value``: the values guarded here are short (a TOTP digit, a card CVV), and a
    floor on that axis leaks every one of them.
    """
    if not value or len(value) < MIN_PERSISTED_REDACTION_LENGTH:
        return text
    return redact_sensitive_value(text, value)
