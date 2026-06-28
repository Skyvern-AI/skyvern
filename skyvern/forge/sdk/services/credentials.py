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


def _strip_totp_input_whitespace(totp_secret: str) -> str:
    return "".join(totp_secret.split())


def _is_otpauth_uri(totp_secret: str) -> bool:
    return totp_secret.lower().startswith("otpauth://")


def parse_totp_secret(totp_secret: str) -> str:
    if not totp_secret:
        return ""

    totp_secret_no_dashe = "".join(totp_secret.split("-"))
    totp_secret_no_whitespace = "".join(totp_secret_no_dashe.split())
    try:
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
        if m is not None:
            totp_secret = m.group(1)
            totp_secret_no_whitespace = "".join(totp_secret.split())

    # Final validation: ensure the result is valid base32
    try:
        pyotp.TOTP(totp_secret_no_whitespace).byte_secret()
        return totp_secret_no_whitespace
    except Exception:
        LOG.error(
            "Invalid TOTP secret, not valid base32, discarding",
            totp_secret_preview=totp_secret_no_whitespace[:4] + "...",
            exc_info=True,
        )
        return ""


def parse_totp_config(totp_secret: str) -> pyotp.TOTP | None:
    """Parse a raw Base32 TOTP secret or full otpauth URI into a TOTP config."""
    if not totp_secret:
        return None

    totp_secret_no_whitespace = _strip_totp_input_whitespace(totp_secret)
    if _is_otpauth_uri(totp_secret_no_whitespace):
        try:
            parsed_otp = pyotp.parse_uri(totp_secret_no_whitespace)
        except Exception:
            LOG.warning("Failed to parse TOTP config from URI", exc_info=True)
            return None
        if not isinstance(parsed_otp, pyotp.TOTP):
            LOG.warning("Parsed OTP URI is not a TOTP config")
            return None
        return parsed_otp

    parsed_totp_secret = parse_totp_secret(totp_secret)
    if not parsed_totp_secret:
        return None
    return pyotp.TOTP(parsed_totp_secret)


def normalize_totp_config(totp_secret: str) -> str:
    """Return the validated TOTP config string while preserving otpauth URI parameters."""
    if not totp_secret:
        return ""

    totp_secret_no_whitespace = _strip_totp_input_whitespace(totp_secret)
    if _is_otpauth_uri(totp_secret_no_whitespace):
        return totp_secret_no_whitespace if parse_totp_config(totp_secret_no_whitespace) else ""

    return parse_totp_secret(totp_secret)


def generate_totp_code(totp_secret: str, for_time: int | None = None) -> str:
    """Generate the current code for a raw TOTP secret or full otpauth URI."""
    totp = parse_totp_config(totp_secret)
    if not totp:
        raise ValueError("Invalid TOTP secret or otpauth URI")
    if for_time is None:
        return totp.now()
    return totp.at(for_time)
