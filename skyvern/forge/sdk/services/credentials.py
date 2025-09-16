import re
from enum import StrEnum
from urllib.parse import unquote

import pyotp
import structlog

LOG = structlog.get_logger()


class OnePasswordConstants(StrEnum):
    """Constants for 1Password integration."""

    TOTP = "OP_TOTP"  # Special value to indicate a TOTP code


class AzureVaultConstants(StrEnum):
    """Constants for Azure Vault integration."""

    TOTP = "AZ_TOTP"  # Special value to indicate a TOTP code


def parse_totp_secret(totp_secret: str) -> str:
    if not totp_secret:
        return ""

    totp_secret_no_dashe = "".join(totp_secret.split("-"))
    totp_secret_no_whitespace = "".join(totp_secret_no_dashe.split())
    try:
        # to verify if it's a valid TOTP secret
        pyotp.TOTP(totp_secret_no_whitespace).byte_secret()
        return totp_secret_no_whitespace
    except Exception:
        LOG.warning("It's not a valid TOTP secret, going to parse it from URI format", exc_info=True)

    try:
        totp_secret = pyotp.parse_uri(totp_secret_no_whitespace).secret
        totp_secret_no_whitespace = "".join(totp_secret.split())
        return totp_secret_no_whitespace
    except Exception:
        LOG.warning("Failed to parse TOTP secret key from URI format, going to extract secret by regex", exc_info=True)
        m = re.search(r"(?i)(?:^|[?&])secret=([^&#]+)", unquote(totp_secret_no_whitespace))
        if m is None:
            return totp_secret_no_whitespace
        totp_secret = m.group(1)
        totp_secret_no_whitespace = "".join(totp_secret.split())
        return totp_secret_no_whitespace
