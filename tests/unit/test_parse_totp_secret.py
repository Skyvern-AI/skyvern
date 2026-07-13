import hashlib
from urllib.parse import quote

import pyotp
import pytest

from skyvern.forge.sdk.services.credentials import (
    generate_totp_code,
    is_unresolved_totp_value,
    normalize_totp_config,
    parse_totp_config,
    parse_totp_secret,
)


def test_empty_string_returns_empty() -> None:
    assert parse_totp_secret("") == ""


@pytest.mark.parametrize("marker", ["OP_TOTP", "BW_TOTP", "AZ_TOTP"])
def test_unresolved_totp_value_detects_embedded_provider_marker(marker: str) -> None:
    assert is_unresolved_totp_value(f"code={marker} user=resolved-secret")


def test_valid_base32_secret() -> None:
    secret = "JBSWY3DPEHPK3PXP"
    assert parse_totp_secret(secret) == secret


def test_valid_base32_with_dashes() -> None:
    assert parse_totp_secret("JBSW-Y3DP-EHPK-3PXP") == "JBSWY3DPEHPK3PXP"


def test_valid_base32_with_whitespace() -> None:
    assert parse_totp_secret("JBSWY3DP EHPK3PXP") == "JBSWY3DPEHPK3PXP"


def test_valid_otpauth_uri() -> None:
    uri = "otpauth://totp/user@example.com?secret=JBSWY3DPEHPK3PXP&issuer=Example"
    result = parse_totp_secret(uri)
    assert result == "JBSWY3DPEHPK3PXP"


def test_normalize_totp_config_preserves_otpauth_uri_params() -> None:
    uri = (
        "otpauth://totp/Example:user@example.com"
        "?secret=JBSWY3DPEHPK3PXP&issuer=Example&algorithm=SHA256&digits=8&period=60"
    )

    assert normalize_totp_config(uri) == uri


def test_generate_totp_code_uses_otpauth_uri_params() -> None:
    uri = (
        "otpauth://totp/Example:user@example.com"
        "?secret=JBSWY3DPEHPK3PXP&issuer=Example&algorithm=SHA256&digits=8&period=60"
    )

    assert generate_totp_code(uri, for_time=0) == pyotp.parse_uri(uri).at(0)
    assert len(generate_totp_code(uri, for_time=0)) == 8


def test_otpauth_uri_with_mismatched_issuer_preserves_generation_config() -> None:
    uri = (
        "otpauth://totp/LabelIssuer:user@example.test"
        "?secret=JBSWY3DPEHPK3PXP&issuer=QueryIssuer&algorithm=SHA256&digits=8&period=60"
    )
    expected = pyotp.TOTP(
        "JBSWY3DPEHPK3PXP",
        digest=hashlib.sha256,
        digits=8,
        interval=60,
    ).at(0)

    assert parse_totp_secret(uri) == "JBSWY3DPEHPK3PXP"
    assert normalize_totp_config(uri) == uri
    assert generate_totp_code(uri, for_time=0) == expected


def test_url_encoded_otpauth_uri_with_mismatched_issuer_is_accepted() -> None:
    uri = (
        "otpauth://totp/LabelIssuer:user@example.test"
        "?secret=JBSWY3DPEHPK3PXP&issuer=QueryIssuer&algorithm=SHA512&digits=7&period=45"
    )
    expected = pyotp.TOTP(
        "JBSWY3DPEHPK3PXP",
        digest=hashlib.sha512,
        digits=7,
        interval=45,
    ).at(0)

    encoded_uri = quote(uri, safe="")

    assert parse_totp_secret(encoded_uri) == "JBSWY3DPEHPK3PXP"
    assert normalize_totp_config(encoded_uri) == uri
    assert generate_totp_code(encoded_uri, for_time=0) == expected


@pytest.mark.parametrize(
    "query",
    [
        "issuer=QueryIssuer",
        "secret=NOT_VALID!&issuer=QueryIssuer",
        "secret=JBSWY3DPEHPK3PXP&issuer=QueryIssuer&algorithm=NOPE",
        "secret=JBSWY3DPEHPK3PXP&issuer=QueryIssuer&digits=9",
        "secret=JBSWY3DPEHPK3PXP&issuer=QueryIssuer&period=invalid",
        "secret=JBSWY3DPEHPK3PXP&issuer=QueryIssuer&period=0",
        "secret=JBSWY3DPEHPK3PXP&issuer=QueryIssuer&period=-30",
        "secret=JBSWY3DPEHPK3PXP&issuer=QueryIssuer&unknown=value",
        "secret=JBSWY3DPEHPK3PXP&issuer=FirstIssuer&issuer=SecondIssuer",
        "secret=JBSWY3DPEHPK3PXP&issuer=QueryIssuer&issuer=QueryIssuer",
        "secret=JBSWY3DPEHPK3PXP&issuer=QueryIssuer&iss%75er=QueryIssuer",
    ],
)
def test_issuer_mismatch_does_not_bypass_invalid_config(query: str) -> None:
    uri = f"otpauth://totp/LabelIssuer:user@example.test?{query}"

    assert parse_totp_secret(uri) == ""
    assert normalize_totp_config(uri) == ""


@pytest.mark.parametrize("period", ["0", "-30"])
def test_matching_issuer_uri_rejects_nonpositive_period(period: str) -> None:
    uri = f"otpauth://totp/Example:user@example.test?secret=JBSWY3DPEHPK3PXP&issuer=Example&period={period}"

    assert parse_totp_secret(uri) == ""
    assert normalize_totp_config(uri) == ""


@pytest.mark.parametrize("second_key", ["issuer", "iss%75er"])
def test_matching_issuer_uri_rejects_duplicate_issuer(second_key: str) -> None:
    uri = f"otpauth://totp/Example:user@example.test?secret=JBSWY3DPEHPK3PXP&issuer=Example&{second_key}=Example"

    assert parse_totp_secret(uri) == ""
    assert normalize_totp_config(uri) == ""


def test_hotp_uri_is_not_coerced_into_totp() -> None:
    uri = "otpauth://hotp/Example:user@example.test?secret=JBSWY3DPEHPK3PXP&issuer=Example&counter=0"

    assert parse_totp_secret(uri) == ""
    assert normalize_totp_config(uri) == ""


@pytest.mark.parametrize("value", ["otpauth://[", quote("otpauth://[", safe="")])
def test_malformed_otpauth_authority_is_rejected_without_raising(value: str) -> None:
    assert parse_totp_secret(value) == ""
    assert parse_totp_config(value) is None
    assert normalize_totp_config(value) == ""


def test_normalize_totp_config_preserves_url_encoded_otpauth_uri() -> None:
    uri = (
        "otpauth://totp/Example:user@example.com"
        "?secret=JBSWY3DPEHPK3PXP&issuer=Example&algorithm=SHA256&digits=8&period=60"
    )

    assert normalize_totp_config(quote(uri, safe="")) == uri


def test_otpauth_uri_with_unsupported_algorithm_returns_empty() -> None:
    uri = "otpauth://totp/user@example.com?secret=JBSWY3DPEHPK3PXP&algorithm=NOPE&digits=8&period=45"

    assert parse_totp_secret(uri) == ""
    assert normalize_totp_config(uri) == ""


def test_url_encoded_otpauth_uri_with_unsupported_algorithm_returns_empty() -> None:
    uri = "otpauth://totp/user@example.com?secret=JBSWY3DPEHPK3PXP&algorithm=NOPE&digits=8&period=45"

    assert parse_totp_secret(quote(uri, safe="")) == ""
    assert normalize_totp_config(quote(uri, safe="")) == ""


def test_regex_extraction_valid_secret() -> None:
    value = "https://example.com?secret=JBSWY3DPEHPK3PXP&other=stuff"
    result = parse_totp_secret(value)
    assert result == "JBSWY3DPEHPK3PXP"


def test_invalid_base32_returns_empty() -> None:
    assert parse_totp_secret("not-a-valid-base32!!!") == ""


def test_invalid_base32_short_returns_empty() -> None:
    assert parse_totp_secret("invalid!") == ""


def test_regex_extraction_invalid_secret_returns_empty() -> None:
    value = "https://example.com?secret=not_valid!!!"
    assert parse_totp_secret(value) == ""
