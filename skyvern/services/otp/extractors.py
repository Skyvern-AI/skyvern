import re

from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services.otp.models import MFANavigationPayload, OTPValue

_MIN_OTP_DIGITS = 4
_MAX_OTP_DIGITS = 10
_OTP_CONTEXT_TERMS = (
    "verification code",
    "authentication code",
    "security code",
    "otp",
    "mfa",
    "2fa",
    "two-factor",
    "two factor",
    "one-time password",
    "one time password",
    "one-time code",
    "one time code",
)
_OTP_INPUT_ACTION_TERMS = ("input", "enter", "type", "fill", "use", "submit")
_MFA_NAVIGATION_PAYLOAD_KEYS_NORMALIZED = {
    "verificationcode",
    "mfachoice",
    "mfacode",
    "otp",
    "otpcode",
    "twofactorcode",
    "2facode",
    "authenticationcode",
    "authcode",
}
_NON_ALNUM_PATTERN = re.compile(r"[^a-z0-9]")


def _build_regex_alternation(terms: tuple[str, ...]) -> str:
    """Build a safe regex alternation fragment from plain text terms."""
    return "|".join(re.escape(term) for term in terms)


_OTP_TERM_ALTERNATION = _build_regex_alternation(_OTP_CONTEXT_TERMS)
_OTP_ACTION_TERM_ALTERNATION = _build_regex_alternation(_OTP_INPUT_ACTION_TERMS)
_OTP_DIGITS_PATTERN = rf"\d{{{_MIN_OTP_DIGITS},{_MAX_OTP_DIGITS}}}"
_OTP_CODE_PATTERN = re.compile(rf"^{_OTP_DIGITS_PATTERN}$")
_OTP_TEXT_BEFORE_CODE_PATTERN = re.compile(
    rf"\b(?:{_OTP_TERM_ALTERNATION})\b[^\d]{{0,40}}({_OTP_DIGITS_PATTERN})\b",
    re.IGNORECASE,
)
_OTP_CODE_BEFORE_TEXT_PATTERN = re.compile(
    rf"\b({_OTP_DIGITS_PATTERN})\b[^\w]{{0,20}}(?:{_OTP_TERM_ALTERNATION})\b",
    re.IGNORECASE,
)
_OTP_CONTEXT_PATTERN = re.compile(
    rf"\b(?:{_OTP_TERM_ALTERNATION})\b",
    re.IGNORECASE,
)
_OTP_INPUT_ACTION_CODE_PATTERN = re.compile(
    rf"\b(?:{_OTP_ACTION_TERM_ALTERNATION})\b[^\d]{{0,30}}({_OTP_DIGITS_PATTERN})\b",
    re.IGNORECASE,
)


def _iter_mfa_payload_values(payload: MFANavigationPayload) -> list[str]:
    """Collect candidate MFA values while preserving recursive traversal order.

    Traversal is cycle-safe to avoid recursive blowups for malformed payload objects.
    """
    if not isinstance(payload, (dict, list)):
        return []

    values: list[str] = []
    traversal_stack: list[dict | list | str] = [payload]
    visited_container_ids: set[int] = set()

    while traversal_stack:
        current_item = traversal_stack.pop()
        if isinstance(current_item, str):
            values.append(current_item)
            continue

        current_id = id(current_item)
        if current_id in visited_container_ids:
            continue
        visited_container_ids.add(current_id)

        if isinstance(current_item, dict):
            for key, value in reversed(list(current_item.items())):
                if isinstance(value, (dict, list)):
                    traversal_stack.append(value)
                if _normalize_payload_key(key) in _MFA_NAVIGATION_PAYLOAD_KEYS_NORMALIZED:
                    candidate_value = _coerce_candidate_code_source(value)
                    if candidate_value is not None:
                        traversal_stack.append(candidate_value)
        else:
            for item in reversed(current_item):
                if isinstance(item, (dict, list)):
                    traversal_stack.append(item)

    return values


def _normalize_payload_key(key: object) -> str:
    """Normalize payload keys for alias matching across separators and casing."""
    return _NON_ALNUM_PATTERN.sub("", str(key).lower())


def _coerce_candidate_code_source(value: object) -> str | None:
    """Coerce alias values to strings while intentionally rejecting bools."""
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def extract_totp_from_text(text: object, *, assume_otp_context: bool = False) -> OTPValue | None:
    """Extract a numeric OTP from free-form text with optional OTP-context override."""
    if not isinstance(text, str) or not text:
        return None

    stripped_text = text.strip()
    if not stripped_text:
        return None

    for pattern in (_OTP_TEXT_BEFORE_CODE_PATTERN, _OTP_CODE_BEFORE_TEXT_PATTERN):
        match = pattern.search(stripped_text)
        if match:
            return OTPValue(value=match.group(1), type=OTPType.TOTP)

    context_found = assume_otp_context or bool(_OTP_CONTEXT_PATTERN.search(stripped_text))
    if not context_found:
        return None

    input_action_match = _OTP_INPUT_ACTION_CODE_PATTERN.search(stripped_text)
    if input_action_match:
        return OTPValue(value=input_action_match.group(1), type=OTPType.TOTP)

    return None


def extract_totp_from_navigation_payload(payload: MFANavigationPayload) -> OTPValue | None:
    """Extract a TOTP code from navigation payload using explicit MFA aliases.

    The extractor is intentionally strict:
    - only exact alias keys are considered
    - values must be numeric and between 4 and 10 digits
    """
    for value in _iter_mfa_payload_values(payload):
        stripped_value = value.strip()
        if _OTP_CODE_PATTERN.fullmatch(stripped_value):
            return OTPValue(value=stripped_value, type=OTPType.TOTP)
        otp_from_text = extract_totp_from_text(stripped_value, assume_otp_context=True)
        if otp_from_text:
            return otp_from_text

    if isinstance(payload, str):
        return extract_totp_from_text(payload, assume_otp_context=True)

    return None


def extract_totp_from_navigation_inputs(
    navigation_payload: MFANavigationPayload, navigation_goal: object
) -> OTPValue | None:
    """Extract TOTP from runtime navigation inputs with explicit precedence.

    Priority:
    1. `navigation_payload` explicit MFA aliases
    2. `navigation_goal` textual instructions (e.g. "Input 520265")
    """
    otp_value = extract_totp_from_navigation_payload(navigation_payload)
    if otp_value:
        return otp_value
    return extract_totp_from_text(navigation_goal)
