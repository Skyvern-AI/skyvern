from typing import Any

import pytest

from skyvern.forge.sdk.core.curl_converter import curl_to_http_request_block_params


@pytest.mark.parametrize(
    ("curl_command", "expected_body"),
    [
        (
            "curl -X POST https://api.example.com/users -H 'Content-Type: application/json' "
            """-d '{"name": "Ada", "role": "admin"}'""",
            {"name": "Ada", "role": "admin"},
        ),
        ("curl https://api.example.com/items --data-raw '[1, 2]'", [1, 2]),
    ],
)
def test_json_body_is_imported_as_the_request_body(curl_command: str, expected_body: Any) -> None:
    params = curl_to_http_request_block_params(curl_command)

    assert params["method"] == "POST"
    assert params["body"] == expected_body


def test_non_json_body_is_wrapped_in_data_key() -> None:
    params = curl_to_http_request_block_params("curl https://api.example.com/form -d 'a=1&b=2'")

    assert params["body"] == {"data": "a=1&b=2"}
