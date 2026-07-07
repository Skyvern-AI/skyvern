import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import unquote, urlsplit

import pyotp
import structlog

LOG = structlog.get_logger()

_SECRET_QUERY_PATTERN = re.compile(r"(?i)(?:^|[?&])secret=([^&#]+)")


class OnePasswordConstants(StrEnum):
    """Constants for 1Password integration."""

    TOTP = "OP_TOTP"  # Special value to indicate a TOTP code


class AzureVaultConstants(StrEnum):
    """Constants for Azure Vault integration."""

    TOTP = "AZ_TOTP"  # Special value to indicate a TOTP code


class AuthenticatorTotpErrorCode(StrEnum):
    """Stable machine-readable errors for authenticator TOTP validation."""

    AUTHENTICATOR_KEY_REQUIRED = "authenticator_key_required"
    INVALID_AUTHENTICATOR_KEY = "invalid_authenticator_key"
    AUTHENTICATOR_NO_CODE_SECRET = "authenticator_no_code_secret"
    AUTHENTICATOR_TOTP_CONFIG_UNSUPPORTED = "authenticator_totp_config_unsupported"
    AUTHENTICATOR_FEATURE_RESTRICTED = "authenticator_feature_restricted"


@dataclass(frozen=True)
class AuthenticatorTotpParseResult:
    secret: str | None = None
    error_code: AuthenticatorTotpErrorCode | None = None
    message: str | None = None
    vendor: str | None = None


def _strip_totp_input_whitespace(totp_secret: str) -> str:
    return "".join(totp_secret.split())


def _compact_secret_value(totp_secret: str) -> str:
    return _strip_totp_input_whitespace(totp_secret).replace("-", "")


def _is_otpauth_uri(totp_secret: str) -> bool:
    parsed = urlsplit(totp_secret)
    return parsed.scheme.lower() == "otpauth" and parsed.netloc.lower() == "totp"


def _validate_base32_secret(totp_secret: str) -> str:
    normalized_secret = _compact_secret_value(totp_secret)
    try:
        pyotp.TOTP(normalized_secret).byte_secret()
        return normalized_secret
    except Exception:
        return ""


def _parse_uri_totp(uri: str) -> pyotp.TOTP | None:
    try:
        parsed_otp = pyotp.parse_uri(uri)
    except Exception:
        LOG.warning("Failed to parse TOTP config from URI", exc_info=True)
        return None
    if not isinstance(parsed_otp, pyotp.TOTP):
        LOG.warning("Parsed OTP URI is not a TOTP config")
        return None
    return parsed_otp


def parse_totp_secret(totp_secret: str) -> str:
    if not totp_secret:
        return ""

    stripped_totp_secret = totp_secret.strip()
    validated_secret = _validate_base32_secret(stripped_totp_secret)
    if validated_secret:
        return validated_secret

    normalized_input = _strip_totp_input_whitespace(stripped_totp_secret)
    unquoted_input = unquote(normalized_input)
    uri_candidates = [normalized_input]
    if unquoted_input != normalized_input:
        uri_candidates.append(unquoted_input)

    for candidate in uri_candidates:
        if not _is_otpauth_uri(candidate):
            continue
        parsed_otp = _parse_uri_totp(candidate)
        if not parsed_otp:
            return ""
        return _validate_base32_secret(parsed_otp.secret)

    match = _SECRET_QUERY_PATTERN.search(unquoted_input)
    if match is not None:
        parsed_from_secret_param = _validate_base32_secret(match.group(1))
        if parsed_from_secret_param:
            return parsed_from_secret_param

    LOG.error(
        "Invalid TOTP secret, discarding",
        totp_secret_preview=_compact_secret_value(stripped_totp_secret)[:4] + "...",
    )
    return ""


def parse_totp_config(totp_secret: str) -> pyotp.TOTP | None:
    """Parse a raw Base32 TOTP secret or full otpauth URI into a TOTP config."""
    if not totp_secret:
        return None

    totp_secret_no_whitespace = _strip_totp_input_whitespace(totp_secret)
    if _is_otpauth_uri(totp_secret_no_whitespace):
        return _parse_uri_totp(totp_secret_no_whitespace)

    unquoted_totp_secret = unquote(totp_secret_no_whitespace)
    if unquoted_totp_secret != totp_secret_no_whitespace and _is_otpauth_uri(unquoted_totp_secret):
        return _parse_uri_totp(unquoted_totp_secret)

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

    unquoted_totp_secret = unquote(totp_secret_no_whitespace)
    if unquoted_totp_secret != totp_secret_no_whitespace and _is_otpauth_uri(unquoted_totp_secret):
        return unquoted_totp_secret if parse_totp_config(unquoted_totp_secret) else ""

    return parse_totp_secret(totp_secret)


def generate_totp_code(totp_secret: str, for_time: int | None = None) -> str:
    """Generate the current code for a raw TOTP secret or full otpauth URI."""
    totp = parse_totp_config(totp_secret)
    if not totp:
        raise ValueError("Invalid TOTP secret or otpauth URI")
    if for_time is None:
        return totp.now()
    return totp.at(for_time)
