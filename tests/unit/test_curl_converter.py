import pytest

from skyvern.forge.sdk.core.curl_converter import parse_curl_command


@pytest.mark.parametrize("auth_option", ["-u", "--user"])
def test_parse_curl_command_adds_basic_auth_header(auth_option: str) -> None:
    result = parse_curl_command(f"curl {auth_option} user:pass https://example.com")

    assert result["headers"] == {"Authorization": "Basic dXNlcjpwYXNz"}


def test_parse_curl_command_preserves_existing_authorization_header() -> None:
    result = parse_curl_command("curl -u user:pass -H 'Authorization: Bearer token' https://example.com")

    assert result["headers"] == {"Authorization": "Bearer token"}
