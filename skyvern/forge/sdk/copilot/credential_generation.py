import secrets
import string
from typing import Literal

REGISTRATION_PASSWORD_MIN_LENGTH = 24
REGISTRATION_PASSWORD_MAX_LENGTH = 128
SAFE_SYMBOLS = "!#$%*+-=?@^_~"

RegistrationCharset = Literal["alphanumeric", "alphanumeric_symbols"]


def _classes(charset: RegistrationCharset) -> list[str]:
    classes = [string.ascii_lowercase, string.ascii_uppercase, string.digits]
    return classes + [SAFE_SYMBOLS] if charset == "alphanumeric_symbols" else classes


def generate_registration_password(length: int, charset: RegistrationCharset) -> str:
    # 24 characters from the 62-character alphabet already carry 142 bits; redrawing until every class
    # appears costs well under one bit.
    if not REGISTRATION_PASSWORD_MIN_LENGTH <= length <= REGISTRATION_PASSWORD_MAX_LENGTH:
        raise ValueError("password length out of range")
    classes = _classes(charset)
    alphabet = "".join(classes)
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if all(any(char in chars for char in password) for chars in classes):
            return password
