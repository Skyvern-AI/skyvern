from __future__ import annotations

import json
import typing
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from skyvern.forge import api_app, request_logging
from skyvern.forge.log_redaction import (
    REDACTED,
    SENSITIVE_FIELDS,
    SENSITIVE_HEADERS,
    is_sensitive_key,
    redact_sensitive_fields,
)
from skyvern.forge.request_logging import (
    _BINARY_PLACEHOLDER,
    _MAX_BODY_LENGTH,
    RequestLoggingMiddleware,
    _client_ip_from_headers,
    _is_loggable_content_type,
    _sanitize_body,
    _sanitize_headers,
    _sanitize_response_body,
    log_raw_request_exception,
    log_raw_request_middleware,
    set_request_organization,
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
# is_sensitive_key — documents exactly which field names are redacted
# ---------------------------------------------------------------------------


class TestIsSensitiveKey:
    """These tests serve as living documentation of the redaction rules.

    If you need to add or remove a field, update ``SENSITIVE_FIELDS`` in
    ``log_redaction.py`` and add a corresponding test case here.
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
            # Credential headers — also classified by SENSITIVE_HEADERS, so they must
            # be masked when the same dict arrives as a log kwarg instead.
            "cookie",
            "Cookie",
            "x-api-key",
            "X-Api-Key",
            # Whole header dicts, masked wholesale because their inner key names are
            # caller-chosen and therefore unmatchable.
            "extra_http_headers",
            "cdp_connect_headers",
            # One-time codes
            "cached_totp",
        ],
    )
    def test_sensitive_keys_are_redacted(self, key: str) -> None:
        assert is_sensitive_key(key) is True, f"Expected '{key}' to be sensitive"

    @pytest.mark.parametrize("key", [200, None, 3.5, ("a",)])
    def test_non_string_keys_are_not_sensitive(self, key: object) -> None:
        # Reached with arbitrary structlog kwargs; raising here kills the log call.
        assert is_sensitive_key(key) is False

    def test_every_credential_header_is_also_a_sensitive_field(self) -> None:
        assert SENSITIVE_HEADERS <= SENSITIVE_FIELDS

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
        assert is_sensitive_key(key) is False, f"Expected '{key}' to NOT be sensitive"


# ---------------------------------------------------------------------------
# redact_sensitive_fields
# ---------------------------------------------------------------------------


class TestRedactSensitiveFields:
    def test_redacts_password(self) -> None:
        data = {"username": "alice", "password": "secret123"}
        result = redact_sensitive_fields(data)
        assert result["username"] == "alice"
        assert result["password"] == REDACTED

    def test_redacts_nested_keys(self) -> None:
        data = {"user": {"api_key": "key123", "name": "bob"}}
        result = redact_sensitive_fields(data)
        assert result["user"]["api_key"] == REDACTED
        assert result["user"]["name"] == "bob"

    def test_redacts_in_lists(self) -> None:
        data = [{"token": "abc"}, {"name": "ok"}]
        result = redact_sensitive_fields(data)
        assert result[0]["token"] == REDACTED
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
            assert result[key] == REDACTED, f"Expected {key} to be redacted"

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
            assert result[key] == REDACTED, f"Expected {key} to be redacted"

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
        assert node["password"] == REDACTED
        assert node["safe"] == "visible"

    def test_preserves_non_sensitive_values(self) -> None:
        data = {"status": "ok", "count": 42, "items": [1, 2, 3]}
        result = redact_sensitive_fields(data)
        assert result == data

    def test_redacts_sensitive_fields_inside_serialized_json(self) -> None:
        private_value = "serialized-private-value"
        data = {
            "payload": json.dumps(
                {
                    "private_key": private_value,
                    "items": [{"status": "ok"}, {"api_key": "nested-key"}],
                }
            )
        }

        result = redact_sensitive_fields(data)
        decoded = json.loads(result["payload"])

        assert private_value not in result["payload"]
        assert decoded == {
            "private_key": REDACTED,
            "items": [{"status": "ok"}, {"api_key": REDACTED}],
        }

    def test_metadata_preserved_by_default_and_redacted_only_when_requested(self) -> None:
        payload = {"metadata": {"region": "north", "note": "opaque"}}

        assert redact_sensitive_fields(payload) == payload
        assert redact_sensitive_fields(payload, redact_metadata=True) == {"metadata": REDACTED}

    def test_is_sensitive_key_gates_metadata_behind_flag(self) -> None:
        assert is_sensitive_key("metadata") is False
        assert is_sensitive_key("Metadata", redact_metadata=True) is True
        assert is_sensitive_key("password") is True

    def test_redact_metadata_flag_threads_into_nested_and_serialized_values(self) -> None:
        nested = {"outer": {"metadata": {"k": "v"}, "keep": "me"}}
        assert redact_sensitive_fields(nested, redact_metadata=True) == {
            "outer": {"metadata": REDACTED, "keep": "me"},
        }

        serialized = {"payload": json.dumps({"metadata": {"k": "v"}, "ok": 1})}
        result = redact_sensitive_fields(serialized, redact_metadata=True)
        assert json.loads(result["payload"]) == {"metadata": REDACTED, "ok": 1}

    def test_preserves_serialized_json_byte_for_byte_when_nothing_redacted(self) -> None:
        # A non-secret JSON payload must round-trip unchanged: re-serializing would escape non-ASCII and
        # re-space separators, corrupting downstream consumers that compare bytes.
        original = '{"note":"Grüße","items":[1,2,3]}'
        result = redact_sensitive_fields({"payload": original})
        assert result["payload"] == original

    @pytest.mark.parametrize("value", ["ordinary text", "123", "true", '"json string"'])
    def test_preserves_non_object_strings(self, value: str) -> None:
        assert redact_sensitive_fields({"message": value, "status": "ok"}) == {
            "message": value,
            "status": "ok",
        }

    def test_handles_non_dict_non_list(self) -> None:
        assert redact_sensitive_fields("hello") == "hello"
        assert redact_sensitive_fields(42) == 42
        assert redact_sensitive_fields(None) is None

    def test_strips_signed_artifact_url_queries_only(self) -> None:
        data = {
            "artifact_url": (
                "https://api.skyvern.com/v1/artifacts/art_synthetic/content/?expiry=1800000600&kid=k1&sig=signed-secret"
            ),
            "other_url": "https://example.com/report?view=full",
        }

        assert redact_sensitive_fields(data) == {
            "artifact_url": "https://api.skyvern.com/v1/artifacts/art_synthetic/content/",
            "other_url": "https://example.com/report?view=full",
        }

    def test_strips_signed_artifact_url_embedded_mid_string(self) -> None:
        data = {
            "input": (
                "fetched https://api.skyvern.com/v1/artifacts/art_embed/content"
                "?expiry=1800000600&kid=k1&sig=embedded-secret then parsed it"
            ),
        }

        assert redact_sensitive_fields(data) == {
            "input": "fetched https://api.skyvern.com/v1/artifacts/art_embed/content then parsed it",
        }

    def test_strips_multiple_embedded_signed_urls_preserving_other_urls(self) -> None:
        data = {
            "input": (
                "a /v1/artifacts/art_a/content?sig=secret-a and "
                "https://api.skyvern.com/v1/artifacts/art_b/content?sig=secret-b but "
                "https://example.com/report?view=full stays"
            ),
        }

        result = redact_sensitive_fields(data)
        assert "secret-a" not in result["input"]
        assert "secret-b" not in result["input"]
        assert result["input"] == (
            "a /v1/artifacts/art_a/content and "
            "https://api.skyvern.com/v1/artifacts/art_b/content but "
            "https://example.com/report?view=full stays"
        )


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


def test_sanitize_headers_removes_posthog_attribution_but_keeps_ordinary_header() -> None:
    assert _sanitize_headers(
        {
            "X-PostHog-Attribution": "encoded-attribution",
            "X-Request-ID": "request-id",
        }
    ) == {"X-Request-ID": "request-id"}


class TestSanitizeResponseBody:
    def test_sensitive_endpoint_fully_redacted(self) -> None:
        request = _make_request("POST", "/api/v1/credentials")
        result = _sanitize_response_body(request, '{"token": "abc"}', "application/json")
        assert result == REDACTED

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
        assert parsed["password"] == REDACTED
        assert parsed["api_key"] == REDACTED

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

    def test_strips_embedded_signed_artifact_url_from_plain_text(self) -> None:
        request = _make_request()
        body = (
            "download failed for https://api.skyvern.com/v1/artifacts/art_err/content"
            "?expiry=1800000600&kid=k1&sig=plain-secret after 3 retries"
        )
        result = _sanitize_response_body(request, body, "text/plain")
        assert "plain-secret" not in result
        assert result == ("download failed for https://api.skyvern.com/v1/artifacts/art_err/content after 3 retries")

    def test_truncates_long_body(self) -> None:
        request = _make_request()
        long_body = "x" * (_MAX_BODY_LENGTH + 500)
        result = _sanitize_response_body(request, long_body, "text/plain")
        assert result.endswith("...[truncated]")
        assert len(result) == _MAX_BODY_LENGTH + len("...[truncated]")

    def test_sensitive_endpoint_trailing_slash(self) -> None:
        request = _make_request("POST", "/api/v1/credentials/")
        result = _sanitize_response_body(request, '{"data": "value"}', "application/json")
        assert result == REDACTED

    def test_malformed_json_credential_mutation_response_is_redacted(self) -> None:
        request = _make_request("PUT", "/api/v1/credentials/cred_123/rotate")
        result = _sanitize_response_body(request, '{"private_key":"secret"', "application/json")
        assert result == REDACTED

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
        assert result == REDACTED


class TestSanitizeBody:
    def test_sensitive_endpoint_request_fully_redacted(self) -> None:
        request = _make_request("POST", "/v1/credentials")
        result = _sanitize_body(request, b'{"password": "hunter2"}', "application/json")
        assert result == REDACTED

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/workflow/copilot/chat-post",
            "/v1/workflow/copilot/question-response",
            "/v1/workflow/copilot/credential-response",
            "/v1/workflow/copilot/convert-yaml-to-blocks",
        ],
    )
    def test_workflow_copilot_request_is_fully_redacted_before_semantic_screening(self, path: str) -> None:
        request = _make_request("POST", path)
        literal = b'{"mode":"ask","message":"password InlineSecret123!"}'

        result = _sanitize_body(request, literal, "application/json")

        assert result == REDACTED
        assert "InlineSecret123" not in result

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
        assert result == REDACTED

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
        assert result == REDACTED

    def test_non_sensitive_endpoint_request_preserved(self) -> None:
        request = _make_request("GET", "/v1/tasks")
        result = _sanitize_body(request, b'{"user": "alice"}', "application/json")
        assert result == '{"user": "alice"}'

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/v1/credentials/cred_123/rotate"),
            ("PUT", "/api/v1/credentials/cred_123"),
        ],
    )
    def test_malformed_json_credential_mutation_request_is_redacted(self, method: str, path: str) -> None:
        request = _make_request(method, path)
        result = _sanitize_body(request, b'{"private_key":"secret"', "application/json")
        assert result == REDACTED

    def test_malformed_json_unrelated_request_keeps_current_behavior(self) -> None:
        request = _make_request("POST", "/v1/tasks")
        result = _sanitize_body(request, b'{"private_key":"secret"', "application/json")
        assert result == '{"private_key":"secret"'

    def test_get_credential_path_is_logged_normally(self) -> None:
        request = _make_request("GET", "/api/v1/credentials/cred_123")
        result = _sanitize_body(request, b'{"status":"ok"}', "application/json")
        assert result == '{"status":"ok"}'

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
        assert result == REDACTED


# ---------------------------------------------------------------------------
# log_raw_request_middleware — which requests produce an api.raw_request log
# ---------------------------------------------------------------------------


class _ScopeCopyingMiddleware:
    """Models the ASGI scope rewrites used by the cloud MCP routes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["path"].startswith("/mcp"):
            scope = dict(scope)
        await self.app(scope, receive, send)


def _make_app(unhandled_exception_status: int = 500) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
        response = JSONResponse(status_code=unhandled_exception_status, content={"detail": type(exc).__name__})
        log_raw_request_exception(response.status_code)
        return response

    @app.get("/heartbeat")
    async def heartbeat() -> dict:
        return {"ok": True}

    @app.post("/tasks")
    async def create_task() -> dict:
        return {
            "created": True,
            "artifact_url": (
                "https://api.skyvern.com/v1/artifacts/art_response/content?expiry=1800000300&kid=k1&sig=response-secret"
            ),
        }

    @app.post("/v1/browser_sessions/{browser_session_id}/action_logs")
    async def create_action_logs(browser_session_id: str) -> dict:
        return {"accepted": 1, "browser_session_id": browser_session_id}

    @app.get("/missing")
    async def missing() -> dict:
        raise HTTPException(status_code=404, detail="not found")

    @app.get("/protected")
    async def protected() -> dict:
        raise HTTPException(status_code=403, detail="Invalid credentials")

    @app.get("/payment-required")
    async def payment_required() -> dict:
        raise HTTPException(status_code=402, detail="Payment Required")

    @app.post("/post-only")
    async def post_only() -> dict:
        return {"ok": True}

    @app.get("/v1/credentials/totp")
    async def get_totp() -> dict:
        return {"code": "123456", "content": "Your code is 123456"}

    app.state.boom_error = ValueError("kaboom")

    @app.get("/boom")
    async def boom() -> dict:
        raise app.state.boom_error

    @app.get("/mcp/boom")
    async def mcp_boom() -> dict:
        raise app.state.boom_error

    app.state.stream_error = ValueError("stream failed")

    @app.post("/stream-error")
    async def stream_error() -> StreamingResponse:
        async def body() -> typing.AsyncGenerator[bytes]:
            yield b"first event\\n\\n"
            raise app.state.stream_error

        return StreamingResponse(body(), media_type="text/event-stream")

    return app


@pytest.fixture
def log_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr(request_logging, "LOG", mock)
    monkeypatch.setattr(request_logging.settings, "LOG_RAW_API_REQUESTS", True)
    monkeypatch.setattr(request_logging.settings, "LOG_RAW_API_REQUESTS_SUCCESSFUL_READS", False)
    return mock


class TestMiddlewareLogVolume:
    def test_scope_rewrite_keeps_unhandled_exception_logging(self, log_mock: MagicMock) -> None:
        app = _make_app()
        app.add_middleware(_ScopeCopyingMiddleware)

        response = TestClient(app, raise_server_exceptions=False).get("/mcp/boom")

        assert response.status_code == 500
        log_mock.error.assert_called_once()
        assert log_mock.error.call_args.args[0] == "api.raw_request"
        assert log_mock.error.call_args.kwargs["status_code"] == response.status_code

    def test_api_app_logs_unhandled_exception_with_its_500_response(
        self, log_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api_app, "LOG", MagicMock())
        app = api_app.create_api_app()

        @app.get("/_test_request_logging_boom")
        async def boom() -> None:
            raise ValueError("kaboom")

        response = TestClient(app, raise_server_exceptions=False).get("/_test_request_logging_boom")

        assert response.status_code == 500
        log_mock.error.assert_called_once()
        assert log_mock.error.call_args.args[0] == "api.raw_request"
        assert log_mock.error.call_args.kwargs["status_code"] == response.status_code

    def test_api_app_logs_exception_across_outer_base_http_task_boundary(
        self, log_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def outer_base_http_middleware(
            request: Request, call_next: typing.Callable[[Request], typing.Awaitable[Response]]
        ) -> Response:
            return await call_next(request)

        def configure_api_app(app: FastAPI) -> None:
            app.middleware("http")(outer_base_http_middleware)

        monkeypatch.setattr(api_app, "LOG", MagicMock())
        monkeypatch.setattr(
            api_app,
            "start_forge_app",
            lambda: SimpleNamespace(setup_api_app=configure_api_app),
        )
        app = api_app.create_api_app()

        @app.get("/_test_request_logging_outer_base_http_boom")
        async def boom() -> None:
            raise ValueError("kaboom")

        response = TestClient(app, raise_server_exceptions=False).get("/_test_request_logging_outer_base_http_boom")

        assert response.status_code == 500
        log_mock.error.assert_called_once()
        assert log_mock.error.call_args.args[0] == "api.raw_request"
        assert log_mock.error.call_args.kwargs["status_code"] == response.status_code

    def test_authenticated_request_is_attributed_to_its_organization(
        self, log_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-org error-rate monitors need customer identity on every api.raw_request row.

        Auth resolves inside api_app's own BaseHTTPMiddleware layers, whose child tasks do not
        propagate rebound ContextVars back up to the logging middleware that emits the row.
        """
        monkeypatch.setattr(api_app, "LOG", MagicMock())
        app = api_app.create_api_app()

        @app.post("/_test_request_logging_authed")
        async def authed() -> dict:
            set_request_organization("o_385835488455492960", "Acme Corp")
            return {"ok": True}

        response = TestClient(app).post("/_test_request_logging_authed")

        assert response.status_code == 200
        logged = log_mock.info.call_args.kwargs
        assert logged["organization_id"] == "o_385835488455492960"
        assert logged["organization_name"] == "Acme Corp"

    def test_unhandled_exception_is_attributed_to_its_organization(
        self, log_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api_app, "LOG", MagicMock())
        app = api_app.create_api_app()

        @app.post("/_test_request_logging_authed_boom")
        async def boom() -> None:
            set_request_organization("o_385835488455492960", "Acme Corp")
            raise ValueError("kaboom")

        response = TestClient(app, raise_server_exceptions=False).post("/_test_request_logging_authed_boom")

        assert response.status_code == 500
        assert log_mock.error.call_args.kwargs["organization_id"] == "o_385835488455492960"

    def test_organization_does_not_leak_into_a_later_unauthenticated_request(
        self, log_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api_app, "LOG", MagicMock())
        app = api_app.create_api_app()

        @app.post("/_test_request_logging_authed")
        async def authed() -> dict:
            set_request_organization("o_385835488455492960", "Acme Corp")
            return {"ok": True}

        @app.post("/_test_request_logging_anonymous")
        async def anonymous() -> dict:
            return {"ok": True}

        client = TestClient(app)
        client.post("/_test_request_logging_authed")
        response = client.post("/_test_request_logging_anonymous")

        assert response.status_code == 200
        logged = log_mock.info.call_args.kwargs
        assert "organization_id" not in logged
        assert "organization_name" not in logged

    def test_action_log_request_and_response_bodies_are_fully_redacted(self, log_mock: MagicMock) -> None:
        client = TestClient(_make_app())
        secret = "sk-test-action-log-secret"

        response = client.post(
            "/v1/browser_sessions/pbs_test/action_logs",
            json={"events": [{"selector": f'input[value="{secret}"]'}]},
        )

        assert response.status_code == 200
        log_mock.info.assert_called_once()
        assert log_mock.info.call_args.kwargs["body"] == REDACTED
        assert log_mock.info.call_args.kwargs["response_body"] == REDACTED
        assert secret not in str(log_mock.info.call_args)

    def test_successful_sensitive_get_keeps_redacted_audit_line(self, log_mock: MagicMock) -> None:
        """OTP/credential reads must leave an audit trail even though they are successful reads."""
        client = TestClient(_make_app())
        response = client.get("/v1/credentials/totp")
        assert response.status_code == 200
        log_mock.info.assert_called_once()
        assert log_mock.info.call_args.args[0] == "api.raw_request"
        assert log_mock.info.call_args.kwargs["response_body"] == REDACTED

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
        assert log_mock.info.call_count + log_mock.warning.call_count + log_mock.error.call_count == 1

    def test_successful_post_survives_log_failure(self, log_mock: MagicMock) -> None:
        log_mock.info.side_effect = RuntimeError("log sink unavailable")

        response = TestClient(_make_app()).post("/tasks")

        assert response.status_code == 200
        log_mock.info.assert_called_once()

    def test_403_get_keeps_datadog_monitor_contract(self, log_mock: MagicMock) -> None:
        """The 403-spike monitors query api.raw_request status:warn @status_code:403."""
        client = TestClient(_make_app())
        response = client.get("/protected")
        assert response.status_code == 403
        log_mock.warning.assert_called_once()
        assert log_mock.warning.call_args.args[0] == "api.raw_request"
        assert log_mock.warning.call_args.kwargs["status_code"] == 403

    def test_artifact_url_queries_are_redacted_from_logged_bodies(self, log_mock: MagicMock) -> None:
        client = TestClient(_make_app())
        response = client.post(
            "/tasks",
            json={
                "file_url": (
                    "https://api.skyvern.com/v1/artifacts/art_request/content/"
                    "?expiry=1800000300&kid=k1&sig=request-secret"
                )
            },
        )

        assert response.status_code == 200
        logged = log_mock.info.call_args.kwargs
        assert json.loads(logged["body"])["file_url"] == ("https://api.skyvern.com/v1/artifacts/art_request/content/")
        assert json.loads(logged["response_body"])["artifact_url"] == (
            "https://api.skyvern.com/v1/artifacts/art_response/content"
        )
        assert "request-secret" not in repr(logged)
        assert "response-secret" not in repr(logged)

    @pytest.mark.parametrize("unhandled_exception_status", [500, 503])
    def test_exception_path_logs_the_status_received_by_the_client(
        self, log_mock: MagicMock, unhandled_exception_status: int
    ) -> None:
        client = TestClient(_make_app(unhandled_exception_status), raise_server_exceptions=False)
        response = client.get("/boom")
        assert response.status_code == unhandled_exception_status
        log_mock.error.assert_called_once()
        assert log_mock.error.call_args.args[0] == "api.raw_request"
        assert log_mock.error.call_args.kwargs["status_code"] == response.status_code

    def test_exception_path_reraises_the_original_error_if_logging_fails(self, log_mock: MagicMock) -> None:
        log_mock.error.side_effect = RuntimeError("log sink unavailable")
        app = _make_app()
        client = TestClient(app)

        with pytest.raises(ValueError, match="kaboom") as raised:
            client.get("/boom")

        assert raised.value is app.state.boom_error
        log_mock.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_partially_started_mutating_stream_logs_once_with_client_status(self, log_mock: MagicMock) -> None:
        app = _make_app()
        messages: list[dict[str, typing.Any]] = []
        request_sent = False

        async def receive() -> dict[str, typing.Any]:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict[str, typing.Any]) -> None:
            messages.append(message)

        with pytest.raises(ValueError) as raised:
            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/stream-error",
                    "raw_path": b"/stream-error",
                    "query_string": b"",
                    "root_path": "",
                    "headers": [],
                    "client": ("testclient", 50000),
                    "server": ("testserver", 80),
                    "extensions": {},
                },
                receive,
                send,
            )

        assert raised.value is app.state.stream_error
        assert messages[0]["status"] == 200
        log_mock.error.assert_called_once()
        assert log_mock.error.call_args.args[0] == "api.raw_request"
        assert log_mock.error.call_args.kwargs["status_code"] == messages[0]["status"]
        assert log_mock.info.call_count + log_mock.warning.call_count + log_mock.error.call_count == 1

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


class TestRawRequestLogLevel:
    """Only 4xx rows that call for action share the warn tier; 404/405 are routine and log at info."""

    @pytest.mark.parametrize(("path", "status_code"), [("/missing", 404), ("/post-only", 405)])
    def test_not_found_and_method_not_allowed_log_at_info(
        self, log_mock: MagicMock, path: str, status_code: int
    ) -> None:
        response = TestClient(_make_app()).get(path)

        assert response.status_code == status_code
        log_mock.info.assert_called_once()
        assert log_mock.info.call_args.args[0] == "api.raw_request"
        assert log_mock.info.call_args.kwargs["status_code"] == status_code
        log_mock.warning.assert_not_called()

    @pytest.mark.parametrize(("path", "status_code"), [("/payment-required", 402), ("/protected", 403)])
    def test_actionable_client_errors_still_log_at_warning(
        self, log_mock: MagicMock, path: str, status_code: int
    ) -> None:
        response = TestClient(_make_app()).get(path)

        assert response.status_code == status_code
        log_mock.warning.assert_called_once()
        assert log_mock.warning.call_args.args[0] == "api.raw_request"
        assert log_mock.warning.call_args.kwargs["status_code"] == status_code
        log_mock.info.assert_not_called()
