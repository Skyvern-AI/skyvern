import re

SAFE_EMAIL_ADDRESS_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+$")


def normalize_email_address(value: str) -> str:
    return value.strip().casefold()


def normalize_identifier_if_email(value: str) -> str:
    stripped_identifier = value.strip()
    if SAFE_EMAIL_ADDRESS_PATTERN.fullmatch(stripped_identifier):
        return normalize_email_address(value)
    return value
