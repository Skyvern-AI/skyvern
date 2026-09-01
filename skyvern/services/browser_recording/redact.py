"""Secret-field detection for record-browser capture and live drafts.

Keep the token lists identical to `isSecretField` in
`skyvern/forge/sdk/routes/streaming/channels/js/exfiltrate.js`.
"""

from __future__ import annotations

import re
import typing as t

from skyvern.services.browser_recording.types import CredentialKind, ExfiltratedConsoleEvent, ExfiltratedEvent

SECRET_INPUT_TYPES = frozenset({"password"})
SECRET_AUTOCOMPLETE_TOKENS = frozenset(
    {
        "current-password",
        "new-password",
        "one-time-code",
        "cc-name",
        "cc-number",
        "cc-csc",
        "cc-exp",
        "cc-exp-month",
        "cc-exp-year",
    }
)
PASSWORD_AUTOCOMPLETE_TOKENS = frozenset({"current-password", "new-password"})
CREDIT_CARD_AUTOCOMPLETE_TOKENS = frozenset(
    {
        "cc-name",
        "cc-number",
        "cc-csc",
        "cc-exp",
        "cc-exp-month",
        "cc-exp-year",
    }
)

# Phrase matchers run on a punctuation-normalized haystack of id / name / labels.
CREDIT_CARD_HINT_PHRASES = (
    "card number",
    "credit card",
    "cardholder",
    "cvv",
    "cvc",
)
TOTP_HINT_PHRASES = (
    "otp",
    "totp",
    "2fa",
    "two factor",
    "one time code",
    "verification code",
    "authenticator code",
)
SECRET_HINT_PHRASES = (
    "api key",
    "apikey",
    "access token",
    "client secret",
    "webhook secret",
    "private key",
    "bearer token",
    "secret value",
)
MAGIC_LINK_HINT_PHRASES = (
    "magic link",
    "email me a link",
    "send login link",
    "send sign in link",
    "passwordless",
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

HaystackPart = str | None | list[str]


def _autocomplete_tokens(autocomplete: str | None) -> list[str]:
    if not autocomplete:
        return []
    return autocomplete.lower().split()


# `texts` comes from getElementText, which appends innerText/textContent for any element
# that has children: for a <select> that is the option labels, for a container it is
# arbitrary page copy. <input> is void, so only there is `texts` purely labelling. Anything
# else is classified from its id and accessible name only, which is also what the page
# script's own matcher reads.
TEXT_IS_LABEL_TAGS = frozenset({"input"})


def texts_are_labels(tag_name: str | None) -> bool:
    return (tag_name or "").lower() in TEXT_IS_LABEL_TAGS


def _normalized_haystack(*parts: HaystackPart) -> str:
    chunks: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, list):
            chunks.extend(item for item in part if item)
        elif part:
            chunks.append(part)
    if not chunks:
        return ""
    return " " + _NON_ALNUM_RE.sub(" ", " ".join(chunks).lower()).strip() + " "


def _haystack_has_phrase(haystack: str, phrase: str) -> bool:
    if not haystack:
        return False
    needle = " " + _NON_ALNUM_RE.sub(" ", phrase.lower()).strip() + " "
    return needle in haystack


def _haystack_has_any(haystack: str, phrases: t.Sequence[str]) -> bool:
    return any(_haystack_has_phrase(haystack, phrase) for phrase in phrases)


def is_secret_field(
    input_type: str | None,
    autocomplete: str | None,
    *,
    field_id: str | None = None,
    accessible_name: str | None = None,
    texts: list[str] | None = None,
    tag_name: str | None = None,
) -> bool:
    kind = credential_kind_for_target(
        input_type,
        autocomplete,
        field_id=field_id,
        accessible_name=accessible_name,
        texts=texts,
        tag_name=tag_name,
    )
    return kind in {"password", "totp", "credit_card", "secret"}


def credential_kind_for_target(
    input_type: str | None,
    autocomplete: str | None,
    *,
    field_id: str | None = None,
    accessible_name: str | None = None,
    texts: list[str] | None = None,
    tag_name: str | None = None,
) -> CredentialKind | None:
    tokens = _autocomplete_tokens(autocomplete)
    haystack = _normalized_haystack(field_id, accessible_name, texts if texts_are_labels(tag_name) else None)

    if any(token in CREDIT_CARD_AUTOCOMPLETE_TOKENS for token in tokens) or _haystack_has_any(
        haystack, CREDIT_CARD_HINT_PHRASES
    ):
        return "credit_card"
    if "one-time-code" in tokens or _haystack_has_any(haystack, TOTP_HINT_PHRASES):
        return "totp"
    if _haystack_has_any(haystack, SECRET_HINT_PHRASES):
        return "secret"
    if (input_type or "").lower() == "password" or any(token in PASSWORD_AUTOCOMPLETE_TOKENS for token in tokens):
        return "password"
    if _haystack_has_any(haystack, MAGIC_LINK_HINT_PHRASES):
        return "magic_link"
    return None


def is_character_key(key: str | None) -> bool:
    """Whether a KeyboardEvent.key carries typed content rather than naming a key.

    Printable keys are a single character ("a", "1"); named keys are longer ("Enter", "Tab").
    Only the former can reconstruct a secret, so only the former is dropped.
    """
    return key is not None and len(key) == 1


def redact_console_event(event: ExfiltratedEvent) -> ExfiltratedEvent:
    if not isinstance(event, ExfiltratedConsoleEvent):
        return event

    target = event.params.target
    if not is_secret_field(
        target.inputType,
        target.autocomplete,
        field_id=target.id,
        accessible_name=target.accessibleName,
        texts=target.text,
        tag_name=target.tagName,
    ):
        return event

    target.value = None
    event.params.inputValue = None
    # Named keys survive: StateMachineInputText emits on Enter, which is the only signal a
    # login submitted with Enter (never blurring before navigation) ever produces.
    if is_character_key(event.params.key):
        event.params.key = None
        event.params.code = None
    return event
