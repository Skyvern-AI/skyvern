"""Secret-field detection for record-browser capture and live drafts.

Keep the token lists identical to `isSecretField` in
`skyvern/forge/sdk/routes/streaming/channels/js/exfiltrate.js`.
"""

from __future__ import annotations

import typing as t

from skyvern.services.browser_recording.types import ExfiltratedConsoleEvent, ExfiltratedEvent

CredentialKind = t.Literal["password", "totp", "credit_card"]

SECRET_INPUT_TYPES = frozenset({"password"})
SECRET_AUTOCOMPLETE_TOKENS = frozenset(
    {
        "current-password",
        "new-password",
        "one-time-code",
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
        "cc-number",
        "cc-csc",
        "cc-exp",
        "cc-exp-month",
        "cc-exp-year",
    }
)


def _autocomplete_tokens(autocomplete: str | None) -> list[str]:
    if not autocomplete:
        return []
    return autocomplete.lower().split()


def is_secret_field(input_type: str | None, autocomplete: str | None) -> bool:
    if (input_type or "").lower() in SECRET_INPUT_TYPES:
        return True
    return any(token in SECRET_AUTOCOMPLETE_TOKENS for token in _autocomplete_tokens(autocomplete))


def credential_kind_for_target(input_type: str | None, autocomplete: str | None) -> CredentialKind | None:
    tokens = _autocomplete_tokens(autocomplete)
    if any(token in CREDIT_CARD_AUTOCOMPLETE_TOKENS for token in tokens):
        return "credit_card"
    if "one-time-code" in tokens:
        return "totp"
    if (input_type or "").lower() == "password" or any(token in PASSWORD_AUTOCOMPLETE_TOKENS for token in tokens):
        return "password"
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
    if not is_secret_field(target.inputType, target.autocomplete):
        return event

    target.value = None
    event.params.inputValue = None
    # Named keys survive: StateMachineInputText emits on Enter, which is the only signal a
    # login submitted with Enter (never blurring before navigation) ever produces.
    if is_character_key(event.params.key):
        event.params.key = None
        event.params.code = None
    return event
