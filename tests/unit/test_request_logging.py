from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from skyvern.forge import request_logging
from skyvern.forge.request_logging import (
    _BINARY_PLACEHOLDER,
    _MAX_BODY_LENGTH,
    _REDACTED,
    _client_ip_from_headers,
    _is_loggable_content_type,
    _is_sensitive_key,
    _sanitize_body,
    _sanitize_response_body,
    log_raw_request_middleware,
    redact_sensitive_fields,
)

# ---------------------------------------------------------------------------
# _client_ip_from_headers
# ---------------------------------------------------------------------------


class TestClientIpFromHeaders:
    def test_extracts_first_hop_from_x_forwarded_for(self) -> None:
        headers = {"x-forwarded-for": "203.0.113.10, 10.0.0.1"}
        assert _client_ip_from_headers(headers) == "203.0.113.10"

    def test_extracts_single_ip_without_proxy_chain(self) -> None:
        headers = {"x-forwarded-for": "198.51.100.2"}
        assert _client_ip_from_headers(headers) == "198.51.100.2"

    def test_missing_header_returns_none(self) -> None:
        assert _client_ip_from_headers({}) is None

    def test_empty_header_returns_none(self) -> None:
        assert _client_ip_from_headers({"x-forwarded-for": ""}) is None


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
            "totp",
            "TOTP",
            "otp",
            "one_time_code",
            "one_time_password",
            "mfa_code",
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
# redact_sensitive_fields
# ---------------------------------------------------------------------------


class TestRedactSensitiveFields:
    def test_redacts_password(self) -> None:
        data = {"username": "alice", "password": "secret123"}
        result = redact_sensitive_fields(data)
        assert result["username"] == "alice"
        assert result["password"] == _REDACTED

    def test_redacts_nested_keys(self) -> None:
        data = {"user": {"api_key": "key123", "name": "bob"}}
        result = redact_sensitive_fields(data)
        assert result["user"]["api_key"] == _REDACTED
        assert result["user"]["name"] == "bob"

    def test_redacts_in_lists(self) -> None:
        data = [{"token": "abc"}, {"name": "ok"}]
        result = redact_sensitive_fields(data)
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
        result = redact_sensitive_fields(data)
        for key in data:
            assert result[key] == _REDACTED, f"Expected {key} to be redacted"

    def test_redacts_totp_and_otp_fields(self) -> None:
        data = {
            "totp": "123456",
            "otp": "999999",
            "one_time_code": "abc123",
            "one_time_password": "xyz789",
            "mfa_code": "mfa42",
        }
        result = redact_sensitive_fields(data)
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
        result = redact_sensitive_fields(data)
        assert result == data

    def test_depth_limit_prevents_crash(self) -> None:
        deep: dict = {}
        current = deep
        for _ in range(30):
            current["nested"] = {}
            current = current["nested"]
        current["password"] = "should_not_crash"

        result = redact_sensitive_fields(deep)
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

        result = redact_sensitive_fields(deep)
        node = result["level"]
        for _ in range(19):
            node = node["next"]
        assert node["password"] == _REDACTED
        assert node["safe"] == "visible"

    def test_preserves_non_sensitive_values(self) -> None:
        data = {"status": "ok", "count": 42, "items": [1, 2, 3]}
        result = redact_sensitive_fields(data)
        assert result == data

    def test_handles_non_dict_non_list(self) -> None:
        assert redact_sensitive_fields("hello") == "hello"
        assert redact_sensitive_fields(42) == 42
        assert redact_sensitive_fields(None) is None


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

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/v1/credentials/totp"),
            ("POST", "/v1/credentials/totp/"),
            ("POST", "/api/v1/totp"),
            ("POST", "/api/v1/totp/"),
            ("GET", "/v1/credentials/totp"),
            ("GET", "/v1/credentials/totp/"),
        ],
    )
    def test_totp_endpoints_response_redacted(self, method: str, path: str) -> None:
        request = _make_request(method, path)
        body = json.dumps({"code": "123456", "content": "Your code is 123456"})
        result = _sanitize_response_body(request, body, "application/json")
        assert result == _REDACTED


class TestSanitizeBody:
    def test_sensitive_endpoint_request_fully_redacted(self) -> None:
        request = _make_request("POST", "/v1/credentials")
        result = _sanitize_body(request, b'{"password": "hunter2"}', "application/json")
        assert result == _REDACTED

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/google/oauth/config",
            "/v1/google/oauth/config",
        ],
    )
    def test_google_oauth_config_request_redacted(self, path: str) -> None:
        request = _make_request("PUT", path)
        result = _sanitize_body(request, b'{"client_id": "cid", "client_secret": "secret"}', "application/json")
        assert result == _REDACTED

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/google/oauth/callback",
            "/v1/google/oauth/callback",
        ],
    )
    def test_google_oauth_callback_request_redacted(self, path: str) -> None:
        request = _make_request("POST", path)
        result = _sanitize_body(request, b'{"code": "4/0Adeu...", "state": "nonce"}', "application/json")
        assert result == _REDACTED

    def test_non_sensitive_endpoint_request_preserved(self) -> None:
        request = _make_request("GET", "/v1/tasks")
        result = _sanitize_body(request, b'{"user": "alice"}', "application/json")
        assert result == '{"user": "alice"}'

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/v1/credentials/totp"),
            ("POST", "/v1/credentials/totp/"),
            ("POST", "/api/v1/totp"),
            ("POST", "/api/v1/totp/"),
            ("GET", "/v1/credentials/totp"),
            ("GET", "/v1/credentials/totp/"),
        ],
    )
    def test_totp_endpoints_request_redacted(self, method: str, path: str) -> None:
        request = _make_request(method, path)
        body = b'{"totp_identifier": "x@y.com", "content": "Your code is 123456"}'
        result = _sanitize_body(request, body, "application/json")
        assert result == _REDACTED


# ---------------------------------------------------------------------------
# log_raw_request_middleware — which requests produce an api.raw_request log
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(log_raw_request_middleware)

    @app.get("/heartbeat")
    async def heartbeat() -> dict:
        return {"ok": True}

    @app.post("/tasks")
    async def create_task() -> dict:
        return {"created": True}

    @app.get("/missing")
    async def missing() -> dict:
        raise HTTPException(status_code=404, detail="not found")

    @app.get("/protected")
    async def protected() -> dict:
        raise HTTPException(status_code=403, detail="Invalid credentials")

    @app.get("/v1/credentials/totp")
    async def get_totp() -> dict:
        return {"code": "123456", "content": "Your code is 123456"}

    @app.get("/boom")
    async def boom() -> dict:
        raise ValueError("kaboom")

    return app


@pytest.fixture
def log_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr(request_logging, "LOG", mock)
    monkeypatch.setattr(request_logging.settings, "LOG_RAW_API_REQUESTS", True)
    monkeypatch.setattr(request_logging.settings, "LOG_RAW_API_REQUESTS_SUCCESSFUL_READS", False)
    return mock


class TestMiddlewareLogVolume:
    def test_successful_sensitive_get_keeps_redacted_audit_line(self, log_mock: MagicMock) -> None:
        """OTP/credential reads must leave an audit trail even though they are successful reads."""
        client = TestClient(_make_app())
        response = client.get("/v1/credentials/totp")
        assert response.status_code == 200
        log_mock.info.assert_called_once()
        assert log_mock.info.call_args.args[0] == "api.raw_request"
        assert log_mock.info.call_args.kwargs["response_body"] == _REDACTED

    def test_successful_get_is_not_logged(self, log_mock: MagicMock) -> None:
        client = TestClient(_make_app())
        response = client.get("/heartbeat")
        assert response.status_code == 200
        log_mock.info.assert_not_called()
        log_mock.warning.assert_not_called()
        log_mock.error.assert_not_called()

    def test_successful_get_logged_when_reads_enabled(
        self, log_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(request_logging.settings, "LOG_RAW_API_REQUESTS_SUCCESSFUL_READS", True)
        client = TestClient(_make_app())
        response = client.get("/heartbeat")
        assert response.status_code == 200
        log_mock.info.assert_called_once()
        assert log_mock.info.call_args.args[0] == "api.raw_request"

    def test_successful_post_is_logged(self, log_mock: MagicMock) -> None:
        client = TestClient(_make_app())
        response = client.post("/tasks")
        assert response.status_code == 200
        log_mock.info.assert_called_once()
        assert log_mock.info.call_args.args[0] == "api.raw_request"

    def test_failed_get_is_logged_as_warning(self, log_mock: MagicMock) -> None:
        client = TestClient(_make_app())
        response = client.get("/missing")
        assert response.status_code == 404
        log_mock.warning.assert_called_once()
        assert log_mock.warning.call_args.args[0] == "api.raw_request"

    def test_403_get_keeps_datadog_monitor_contract(self, log_mock: MagicMock) -> None:
        """The 403-spike monitors query api.raw_request status:warn @status_code:403."""
        client = TestClient(_make_app())
        response = client.get("/protected")
        assert response.status_code == 403
        log_mock.warning.assert_called_once()
        assert log_mock.warning.call_args.args[0] == "api.raw_request"
        assert log_mock.warning.call_args.kwargs["status_code"] == 403

    def test_exception_path_is_logged_as_error(self, log_mock: MagicMock) -> None:
        client = TestClient(_make_app(), raise_server_exceptions=False)
        response = client.get("/boom")
        assert response.status_code == 500
        log_mock.error.assert_called_once()
        assert log_mock.error.call_args.args[0] == "api.raw_request"

    def test_disabled_middleware_logs_nothing(self, log_mock: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(request_logging.settings, "LOG_RAW_API_REQUESTS", False)
        client = TestClient(_make_app())
        client.post("/tasks")
        log_mock.info.assert_not_called()


class TestClientDisconnectDuringBodyRead:
    """A client closing the connection mid-body must not surface as an unhandled error."""

    @pytest.mark.asyncio
    async def test_disconnect_short_circuits_without_error_log(self, log_mock: MagicMock) -> None:
        request = MagicMock()
        request.method = "POST"
        request.url.path = "/v1/tasks"
        request.body = AsyncMock(side_effect=ClientDisconnect())
        call_next = AsyncMock()

        response = await log_raw_request_middleware(request, call_next)

        assert response.status_code == 499
        call_next.assert_not_awaited()
        log_mock.error.assert_not_called()
        log_mock.exception.assert_not_called()

    @pytest.mark.asyncio
    async def test_disconnect_when_logging_disabled_still_calls_downstream(
        self, log_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(request_logging.settings, "LOG_RAW_API_REQUESTS", False)
        request = MagicMock()
        request.body = AsyncMock(side_effect=ClientDisconnect())
        sentinel = MagicMock()
        call_next = AsyncMock(return_value=sentinel)

        response = await log_raw_request_middleware(request, call_next)

        assert response is sentinel
        request.body.assert_not_awaited()
