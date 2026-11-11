import pytest

from skyvern.forge.sdk.core.curl_converter import curl_to_http_request_block_params, parse_curl_command


@pytest.mark.parametrize(
    "curl_command, expected_authorization",
    [
        ("curl -u alice:s3cret https://api.example.com/me", "Basic YWxpY2U6czNjcmV0"),
        ("curl --user alice:s3cret https://api.example.com/me", "Basic YWxpY2U6czNjcmV0"),
        # curl reads only the first colon as the separator, so the password keeps the rest.
        ("curl -u 'alice:p:w@ss' https://api.example.com/me", "Basic YWxpY2U6cDp3QHNz"),
        # No colon means an empty password, which curl still sends as "user:".
        ("curl -u alice https://api.example.com/me", "Basic YWxpY2U6"),
    ],
)
def test_user_flag_becomes_basic_authorization_header(curl_command: str, expected_authorization: str) -> None:
    assert parse_curl_command(curl_command)["headers"]["Authorization"] == expected_authorization


def test_explicit_authorization_header_wins_over_user_flag() -> None:
    headers = parse_curl_command("curl -u alice:s3cret -H 'authorization: Bearer tok' https://api.example.com/me")[
        "headers"
    ]

    assert headers == {"authorization": "Bearer tok"}


def test_no_authorization_header_without_credentials() -> None:
    assert parse_curl_command("curl https://api.example.com/me")["headers"] == {}


def test_basic_authorization_reaches_http_request_block_params() -> None:
    params = curl_to_http_request_block_params(
        "curl -X POST -u alice:s3cret -d '{\"a\": 1}' https://api.example.com/items"
    )

    assert params["method"] == "POST"
    assert params["url"] == "https://api.example.com/items"
    assert params["headers"]["Authorization"] == "Basic YWxpY2U6czNjcmV0"
