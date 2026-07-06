from typing import Any

import libcst as cst

from skyvern.core.script_generations.generate_script import _build_http_request_statement


def test_http_request_script_statement_includes_secret_response_paths() -> None:
    block: dict[str, Any] = {
        "block_type": "http_request",
        "method": "POST",
        "url": "{{ api_base }}/token",
        "headers": {"Authorization": "Bearer {{ api_key }}"},
        "body": {"grant_type": "client_credentials"},
        "secret_response_paths": ["data.token", "data.refresh_token"],
        "timeout": 30,
        "follow_redirects": True,
        "label": "fetch_token",
    }

    statement = _build_http_request_statement(block)
    rendered = cst.Module(body=[statement]).code
    normalized = rendered.replace(" ", "")

    assert "secret_response_paths=" in normalized
    assert "'data.token'" in rendered or '"data.token"' in rendered
    assert "'data.refresh_token'" in rendered or '"data.refresh_token"' in rendered
