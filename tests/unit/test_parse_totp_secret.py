from skyvern.forge.sdk.services.credentials import parse_totp_secret


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
