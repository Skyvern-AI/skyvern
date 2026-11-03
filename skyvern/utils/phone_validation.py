import re

from skyvern.utils.email_validation import SAFE_EMAIL_ADDRESS_PATTERN, normalize_email_address

PHONE_IDENTIFIER_PATTERN = re.compile(r"^\+?[0-9()\-\.\s]+$")
E164_PHONE_NUMBER_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")
TWILIO_PHONE_NUMBER_SID_PATTERN = re.compile(r"PN[0-9a-fA-F]{32}")
REPEATED_PHONE_PUNCTUATION_PATTERN = re.compile(r"[().-]{2,}")
MIN_PHONE_IDENTIFIER_DIGITS = 7
MAX_PHONE_IDENTIFIER_DIGITS = 15


def looks_like_phone_identifier(value: str) -> bool:
    stripped_value = value.strip()
    if SAFE_EMAIL_ADDRESS_PATTERN.fullmatch(stripped_value):
        return False
    digits = re.sub(r"\D", "", stripped_value)
    return (
        PHONE_IDENTIFIER_PATTERN.fullmatch(stripped_value) is not None
        and MIN_PHONE_IDENTIFIER_DIGITS <= len(digits) <= MAX_PHONE_IDENTIFIER_DIGITS
        and REPEATED_PHONE_PUNCTUATION_PATTERN.search(stripped_value) is None
    )


def normalize_phone_identifier(value: str) -> str:
    stripped_value = value.strip()
    if not looks_like_phone_identifier(stripped_value):
        return stripped_value
    digits = re.sub(r"\D", "", stripped_value)
    if stripped_value.startswith("+"):
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return stripped_value


def is_e164_phone_number(value: str) -> bool:
    return E164_PHONE_NUMBER_PATTERN.fullmatch(value) is not None


def is_twilio_phone_number_sid(value: str) -> bool:
    return isinstance(value, str) and TWILIO_PHONE_NUMBER_SID_PATTERN.fullmatch(value) is not None


def phone_identifier_candidates(value: str) -> list[str]:
    stripped_value = value.strip()
    if not looks_like_phone_identifier(stripped_value):
        return [stripped_value]
    digits = re.sub(r"\D", "", stripped_value)
    normalized = normalize_phone_identifier(stripped_value)
    return sorted({stripped_value, normalized, digits})


def normalize_identifier(value: str) -> str:
    stripped_value = value.strip()
    if SAFE_EMAIL_ADDRESS_PATTERN.fullmatch(stripped_value):
        return normalize_email_address(stripped_value)
    if looks_like_phone_identifier(stripped_value):
        return normalize_phone_identifier(stripped_value)
    return stripped_value
