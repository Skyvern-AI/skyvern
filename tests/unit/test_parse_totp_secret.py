import pyotp

from skyvern.forge.sdk.services.credentials import generate_totp_code, normalize_totp_config, parse_totp_secret


def test_empty_string_returns_empty() -> None:
    assert parse_totp_secret("") == ""


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
