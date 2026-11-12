import pytest

from skyvern.forge.sdk.core.curl_converter import parse_curl_command


@pytest.mark.parametrize(
    "curl_command",
    [
        "curl 'https://api.example.com/items' -H 'Accept: application/json' --compressed",
        "curl -L https://api.example.com/items",
        "curl -s https://api.example.com/items",
    ],
)
def test_unsupported_option_raises_value_error(curl_command: str) -> None:
    with pytest.raises(ValueError, match="unsupported option"):
        parse_curl_command(curl_command)
