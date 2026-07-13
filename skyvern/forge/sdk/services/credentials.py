import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import SplitResult, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import pyotp
import structlog

LOG = structlog.get_logger()

_SECRET_QUERY_PATTERN = re.compile(r"(?i)(?:^|[?&])secret=([^&#]+)")
_ISSUER_MISMATCH_ERROR = "If issuer is specified in both label and parameters, it should be equal."
_TOTP_PLACEHOLDER_PATTERN = re.compile(r"placeholder_[A-Za-z0-9]+_totp")
_TOTP_MARKER_VALUES = frozenset({"OP_TOTP", "BW_TOTP", "AZ_TOTP"})

# 1Password's Python SDK forwards generic 5xx upstream failures as plain Exceptions
# whose stringified message embeds the HTTP status.
_ONEPASSWORD_UPSTREAM_5XX_PATTERN = re.compile(
    r"(?i)"
    r"(?:\b(?:HTTP|status(?:\s+code)?|code|response)\s*[:=]?\s*(5\d{2})\b)"
    r"|"
    r"(?:\b(5\d{2})\s+(?:service\s+unavailable|bad\s+gateway|gateway\s+timeout|internal\s+server\s+error)\b)"
)
_ONEPASSWORD_CREDENTIAL_ERROR_PATTERN = re.compile(
    r"(?i)"
    r"(?:\b(?:unauthorized|forbidden|not\s+authenticated|authentication\s+failed)\b)"
    r"|"
    r"(?:\b(?:invalid|expired|malformed|parse(?:d)?)\b.{0,48}\b(?:token|credential|service\s+account)\b)"
    r"|"
    r"(?:\b(?:token|credential|service\s+account)\b.{0,48}\b(?:invalid|expired|malformed|not\s+valid|parse(?:d)?)\b)"
)


def extract_onepassword_upstream_5xx_status(message: str) -> int | None:
    """Return the upstream 5xx HTTP status embedded in a 1Password SDK error message, if any.

    A match means the failure originated on 1Password's side.
    """
    match = _ONEPASSWORD_UPSTREAM_5XX_PATTERN.search(message)
    if not match:
        return None
    status_digits = match.group(1) or match.group(2)
    return int(status_digits) if status_digits else None


def is_onepassword_credential_error(message: str) -> bool:
    return bool(_ONEPASSWORD_CREDENTIAL_ERROR_PATTERN.search(message))


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


def _safe_urlsplit(value: str) -> SplitResult | None:
    try:
        return urlsplit(value)
    except ValueError:
        return None


def _is_otpauth_uri(totp_secret: str) -> bool:
    parsed = _safe_urlsplit(totp_secret)
    if parsed is None:
        return False
    return parsed.scheme.lower() == "otpauth" and parsed.netloc.lower() == "totp"


def is_unresolved_totp_placeholder(value: object) -> bool:
    """Return whether a value contains an unexpanded workflow TOTP placeholder."""
    return isinstance(value, str) and bool(_TOTP_PLACEHOLDER_PATTERN.search(value))


def is_unresolved_totp_value(value: object) -> bool:
    """Return whether a value still contains a TOTP placeholder or provider marker."""
    return isinstance(value, str) and (
        is_unresolved_totp_placeholder(value) or any(marker in value for marker in _TOTP_MARKER_VALUES)
    )


def _validate_base32_secret(totp_secret: str) -> str:
    normalized_secret = _compact_secret_value(totp_secret)
    try:
        pyotp.TOTP(normalized_secret).byte_secret()
        return normalized_secret
    except Exception:
        return ""


def _parse_uri_totp(uri: str) -> pyotp.TOTP | None:
    parsed_uri = _safe_urlsplit(uri)
    if parsed_uri is None:
        LOG.warning("TOTP URI is malformed")
        return None
    query_pairs = parse_qsl(parsed_uri.query, keep_blank_values=True)
    issuer_count = sum(key == "issuer" for key, _value in query_pairs)
    if issuer_count > 1:
        LOG.warning("TOTP URI contains duplicate issuer parameters")
        return None

    try:
        parsed_otp = pyotp.parse_uri(uri)
    except ValueError as exc:
        if str(exc) != _ISSUER_MISMATCH_ERROR:
            LOG.warning("Failed to parse TOTP config from URI", exception_type=type(exc).__name__)
            return None

        # Issuer metadata does not affect code generation, but PyOTP rejects conflicts before validating the config.
        if issuer_count != 1:
            LOG.warning("TOTP URI is missing issuer metadata required for mismatch recovery")
            return None
        query_without_issuer = urlencode([(key, value) for key, value in query_pairs if key != "issuer"])
        validation_uri = urlunsplit(parsed_uri._replace(query=query_without_issuer))
        try:
            parsed_otp = pyotp.parse_uri(validation_uri)
        except Exception as fallback_exc:
            LOG.warning(
                "Failed to parse TOTP config from URI after ignoring mismatched issuer metadata",
                exception_type=type(fallback_exc).__name__,
            )
            return None
    except Exception as exc:
        LOG.warning("Failed to parse TOTP config from URI", exception_type=type(exc).__name__)
        return None
    if not isinstance(parsed_otp, pyotp.TOTP):
        LOG.warning("Parsed OTP URI is not a TOTP config")
        return None
    if parsed_otp.interval <= 0:
        LOG.warning("TOTP URI contains a nonpositive period")
        return None
    try:
        parsed_otp.byte_secret()
    except Exception as exc:
        LOG.warning("TOTP URI contains an invalid secret", exception_type=type(exc).__name__)
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
        parsed_candidate = _safe_urlsplit(candidate)
        if parsed_candidate is None:
            return ""
        if not (parsed_candidate.scheme.lower() == "otpauth" and parsed_candidate.netloc.lower() == "totp"):
            if parsed_candidate.scheme.lower() == "otpauth":
                return ""
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
