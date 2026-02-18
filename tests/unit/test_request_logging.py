from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from skyvern.forge.request_logging import (
    _BINARY_PLACEHOLDER,
    _MAX_BODY_LENGTH,
    _REDACTED,
    _is_loggable_content_type,
    _is_sensitive_key,
    _redact_sensitive_fields,
    _sanitize_response_body,
)

# ---------------------------------------------------------------------------
# _is_sensitive_key — documents exactly which field names are redacted
# ---------------------------------------------------------------------------


class TestIsSensitiveKey:
    """These tests serve as living documentation of the redaction rules.

    If you need to add or remove a field, update ``_SENSITIVE_FIELDS`` in
    ``request_logging.py`` and add a corresponding test case here.
    """

    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "Password",
            "PASSWORD",
            "secret",
            "token",
            "api_key",
            "apikey",
            "api-key",
            "credential",
            "access_key",
            "private_key",
            "auth",
            "authorization",
            "secret_key",
        ],
    )
    def test_sensitive_keys_are_redacted(self, key: str) -> None:
        assert _is_sensitive_key(key) is True, f"Expected '{key}' to be sensitive"

    @pytest.mark.parametrize(
        "key",
        [
            # Suffixed IDs / metadata — should NOT be redacted
            "credential_id",
            "credential_type",
            "token_type",
            "token_count",
            "access_key_id",
            # Pagination cursors
            "next_token",
            "page_token",
            "cursor_token",
            # Author / authentication metadata
            "author",
            "authenticated",
            "authenticated_at",
            "authorization_url",
            "auth_method",
            # Other safe fields
            "secret_name",
            "password_updated_at",
            "api_key_id",
        ],
    )
    def test_non_sensitive_keys_are_preserved(self, key: str) -> None:
        assert _is_sensitive_key(key) is False, f"Expected '{key}' to NOT be sensitive"


# ---------------------------------------------------------------------------
# _redact_sensitive_fields
# ---------------------------------------------------------------------------


class TestRedactSensitiveFields:
    def test_redacts_password(self) -> None:
        data = {"username": "alice", "password": "secret123"}
        result = _redact_sensitive_fields(data)
        assert result["username"] == "alice"
        assert result["password"] == _REDACTED

    def test_redacts_nested_keys(self) -> None:
        data = {"user": {"api_key": "key123", "name": "bob"}}
        result = _redact_sensitive_fields(data)
        assert result["user"]["api_key"] == _REDACTED
        assert result["user"]["name"] == "bob"

    def test_redacts_in_lists(self) -> None:
        data = [{"token": "abc"}, {"name": "ok"}]
        result = _redact_sensitive_fields(data)
        assert result[0]["token"] == _REDACTED
        assert result[1]["name"] == "ok"

    def test_redacts_various_sensitive_keys(self) -> None:
        data = {
            "access_key": "a",
            "private_key": "b",
            "credential": "c",
            "secret": "d",
            "apikey": "e",
            "api-key": "f",
            "api_key": "g",
            "Authorization": "h",
        }
        result = _redact_sensitive_fields(data)
        for key in data:
            assert result[key] == _REDACTED, f"Expected {key} to be redacted"

    def test_preserves_non_sensitive_suffixed_keys(self) -> None:
        """Fields like credential_id and page_token must NOT be redacted."""
        data = {
            "credential_id": "cred_123",
            "credential_type": "oauth",
            "page_token": "abc",
            "author": "alice",
            "token_count": 42,
        }
        result = _redact_sensitive_fields(data)
        assert result == data

    def test_depth_limit_prevents_crash(self) -> None:
        deep: dict = {}
        current = deep
        for _ in range(30):
            current["nested"] = {}
            current = current["nested"]
        current["password"] = "should_not_crash"

        result = _redact_sensitive_fields(deep)
        assert result is not None  # should not raise RecursionError

    def test_depth_limit_still_redacts_keys_at_boundary(self) -> None:
        """Sensitive keys at the depth boundary must still be redacted."""
        # depth 0: top dict, depth 1: "level" value, depths 2-20: 19 "next" dicts, depth 21: leaf
        deep: dict = {"level": {}}
        current = deep["level"]
        for _ in range(19):
            current["next"] = {}
            current = current["next"]
        current["password"] = "leak_me"
        current["safe"] = "visible"

        result = _redact_sensitive_fields(deep)
        node = result["level"]
        for _ in range(19):
            node = node["next"]
        assert node["password"] == _REDACTED
        assert node["safe"] == "visible"

    def test_preserves_non_sensitive_values(self) -> None:
        data = {"status": "ok", "count": 42, "items": [1, 2, 3]}
        result = _redact_sensitive_fields(data)
        assert result == data

    def test_handles_non_dict_non_list(self) -> None:
        assert _redact_sensitive_fields("hello") == "hello"
        assert _redact_sensitive_fields(42) == 42
        assert _redact_sensitive_fields(None) is None


# ---------------------------------------------------------------------------
# _is_loggable_content_type
# ---------------------------------------------------------------------------


class TestIsLoggableContentType:
    def test_json_is_loggable(self) -> None:
        assert _is_loggable_content_type("application/json") is True
        assert _is_loggable_content_type("application/json; charset=utf-8") is True

    def test_text_is_loggable(self) -> None:
        assert _is_loggable_content_type("text/plain") is True
        assert _is_loggable_content_type("text/html") is True

    def test_binary_is_not_loggable(self) -> None:
        assert _is_loggable_content_type("application/octet-stream") is False
        assert _is_loggable_content_type("image/png") is False

    def test_none_defaults_to_loggable(self) -> None:
        assert _is_loggable_content_type(None) is True


# ---------------------------------------------------------------------------
# _sanitize_response_body
# ---------------------------------------------------------------------------


def _make_request(method: str = "GET", path: str = "/api/v1/test") -> MagicMock:
    request = MagicMock()
    request.method = method
    request.url.path = path
    return request


class TestSanitizeResponseBody:
    def test_sensitive_endpoint_fully_redacted(self) -> None:
        request = _make_request("POST", "/api/v1/credentials")
        result = _sanitize_response_body(request, '{"token": "abc"}', "application/json")
        assert result == _REDACTED

    def test_empty_body(self) -> None:
        request = _make_request()
        assert _sanitize_response_body(request, "", "application/json") == ""

    def test_none_body_returns_binary_placeholder(self) -> None:
        request = _make_request()
        assert _sanitize_response_body(request, None, "application/json") == _BINARY_PLACEHOLDER

    def test_binary_content_type_returns_placeholder(self) -> None:
        request = _make_request()
        result = _sanitize_response_body(request, "some bytes", "application/octet-stream")
        assert result == _BINARY_PLACEHOLDER

    def test_json_fields_are_redacted(self) -> None:
        request = _make_request()
        body = json.dumps({"user": "alice", "password": "hunter2", "api_key": "sk-123"})
        result = _sanitize_response_body(request, body, "application/json")
        parsed = json.loads(result)
        assert parsed["user"] == "alice"
        assert parsed["password"] == _REDACTED
        assert parsed["api_key"] == _REDACTED

    def test_json_preserves_non_sensitive_suffixed_keys(self) -> None:
        """credential_id and page_token in responses must remain visible for debugging."""
        request = _make_request()
        body = json.dumps({"credential_id": "cred_123", "page_token": "abc", "author": "bob"})
        result = _sanitize_response_body(request, body, "application/json")
        parsed = json.loads(result)
        assert parsed["credential_id"] == "cred_123"
        assert parsed["page_token"] == "abc"
        assert parsed["author"] == "bob"

    def test_non_json_body_returned_as_is(self) -> None:
        request = _make_request()
        result = _sanitize_response_body(request, "plain text response", "text/plain")
        assert result == "plain text response"

    def test_truncates_long_body(self) -> None:
        request = _make_request()
        long_body = "x" * (_MAX_BODY_LENGTH + 500)
        result = _sanitize_response_body(request, long_body, "text/plain")
        assert result.endswith("...[truncated]")
        assert len(result) == _MAX_BODY_LENGTH + len("...[truncated]")

    def test_sensitive_endpoint_trailing_slash(self) -> None:
        request = _make_request("POST", "/api/v1/credentials/")
        result = _sanitize_response_body(request, '{"data": "value"}', "application/json")
        assert result == _REDACTED
