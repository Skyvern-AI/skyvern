import re
from enum import StrEnum
from urllib.parse import unquote

import pyotp
import structlog

from skyvern.exceptions import NoTOTPSecretFound

LOG = structlog.get_logger()


class OnePasswordConstants(StrEnum):
    """Constants for 1Password integration."""

    TOTP = "OP_TOTP"  # Special value to indicate a TOTP code


def parse_totp_secret(totp_secret: str) -> str:
    if not totp_secret:
        return ""

    totp_secret_no_whitespace = "".join(totp_secret.split())
    if len(totp_secret_no_whitespace) == 32:
        return totp_secret_no_whitespace

    LOG.info("TOTP secret key is not 32 characters, try to parse it from URI format")
    try:
        totp_secret = pyotp.parse_uri(totp_secret_no_whitespace).secret
        totp_secret_no_whitespace = "".join(totp_secret.split())
        return totp_secret_no_whitespace
    except Exception:
        LOG.warning("Failed to parse TOTP secret key from URI format, going to extract secret by regex", exc_info=True)
        m = re.search(r"(?i)(?:^|[?&])secret=([^&#]+)", unquote(totp_secret_no_whitespace))
        if m is None:
            raise NoTOTPSecretFound()
        totp_secret = m.group(1)
        totp_secret_no_whitespace = "".join(totp_secret.split())
        return totp_secret_no_whitespace
