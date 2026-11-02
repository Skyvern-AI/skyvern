import re

SAFE_EMAIL_ADDRESS_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+$")
# Keep SQL identifier matching aligned with Python's str.strip().
EMAIL_IDENTIFIER_TRIM_CHARS = (
    " \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


def normalize_email_address(value: str) -> str:
    return value.strip(EMAIL_IDENTIFIER_TRIM_CHARS).casefold()


def normalize_identifier_if_email(value: str) -> str:
    stripped_identifier = value.strip(EMAIL_IDENTIFIER_TRIM_CHARS)
    if SAFE_EMAIL_ADDRESS_PATTERN.fullmatch(stripped_identifier):
        return normalize_email_address(value)
    return value
