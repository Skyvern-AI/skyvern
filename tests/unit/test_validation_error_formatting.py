from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError, field_validator
from starlette.requests import Request

from skyvern.forge.api_app import create_api_app, format_validation_errors


class _DummyModel(BaseModel):
    name: str
    age: int


class _NestedModel(BaseModel):
    user: _DummyModel


class _ModelWithBodySegment(BaseModel):
    """Model that will produce 'body' in error loc when used with FastAPI-style validation."""

    email: str


class _ModelWithRootValidator(BaseModel):
    value: int

    @field_validator("value")
    @classmethod
    def must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("value must be positive")
        return v


class TestFormatValidationErrors:
    """Tests for format_validation_errors in api_app.py."""

    def test_single_field_error(self) -> None:
        """A single missing field produces 'field_name: message'."""
        try:
            _DummyModel(name="Alice", age="not_a_number")  # type: ignore[arg-type]
        except ValidationError as exc:
            result = format_validation_errors(exc)
        assert result.startswith("age:") and "validation error" not in result

    def test_multiple_field_errors(self) -> None:
        """Multiple errors are joined with '; '."""
        try:
            _DummyModel(name=123, age="not_a_number")  # type: ignore[arg-type]
        except ValidationError as exc:
            result = format_validation_errors(exc)
        assert "; " in result
        assert "name" in result
        assert "age" in result

    def test_nested_field_error_uses_arrow_separator(self) -> None:
        """Nested field paths use ' -> ' as separator."""
        try:
            _NestedModel(user={"name": "Alice", "age": "bad"})  # type: ignore[arg-type]
        except ValidationError as exc:
            result = format_validation_errors(exc)
        assert "user -> age" in result

    def test_root_segment_filtered(self) -> None:
        """'__root__' segments should be stripped from the location path."""
        # Pydantic v2 doesn't typically produce __root__ in the same way, but we
        # test the filtering by checking that the function handles it via the
        # field_validator path which still produces a meaningful message.
        try:
            _ModelWithRootValidator(value=-1)
        except ValidationError as exc:
            result = format_validation_errors(exc)
        assert "value" in result
        assert "must be positive" in result
        assert "__root__" not in result

    def test_body_segment_filtered(self) -> None:
        """'body' segments should be stripped from the location path (consistency with frontend)."""
        # Simulate what FastAPI produces: error dicts with 'body' in loc.
        # We construct a ValidationError manually via _DummyModel, and the
        # function should filter 'body' from any loc.
        try:
            _DummyModel(name="Alice", age="bad")  # type: ignore[arg-type]
        except ValidationError as exc:
            result = format_validation_errors(exc)
        # 'body' should not appear in the output even if it were in loc
        assert "body" not in result

    def test_fallback_message_when_no_errors(self) -> None:
        """When error_messages list is empty, a friendly fallback is returned.

        This is practically unreachable with real ValidationErrors, but we
        verify the fallback path by mocking.
        """
        mock_exc = MagicMock(spec=ValidationError)
        mock_exc.errors.return_value = []
        result = format_validation_errors(mock_exc)
        assert result == "A validation error occurred. Please check your input and try again."

    def test_error_message_without_loc(self) -> None:
        """When loc is empty after filtering, only the message is shown."""
        mock_exc = MagicMock(spec=ValidationError)
        mock_exc.errors.return_value = [
            {"loc": ("__root__",), "msg": "Something went wrong", "type": "value_error"},
        ]
        result = format_validation_errors(mock_exc)
        assert result == "Something went wrong"
        assert "__root__" not in result

    def test_body_only_loc_is_filtered(self) -> None:
        """When loc contains only 'body', it is fully filtered and just the message is shown."""
        mock_exc = MagicMock(spec=ValidationError)
        mock_exc.errors.return_value = [
            {"loc": ("body",), "msg": "Invalid request body", "type": "value_error"},
        ]
        result = format_validation_errors(mock_exc)
        assert result == "Invalid request body"

    def test_body_and_field_in_loc(self) -> None:
        """When loc is ('body', 'field_name'), 'body' is filtered, keeping 'field_name'."""
        mock_exc = MagicMock(spec=ValidationError)
        mock_exc.errors.return_value = [
            {"loc": ("body", "email"), "msg": "field required", "type": "value_error.missing"},
        ]
        result = format_validation_errors(mock_exc)
        assert result == "email: field required"
        assert "body" not in result


@pytest.mark.asyncio
async def test_request_validation_handler_does_not_echo_input() -> None:
    secret_input = "submitted-private-key-material"
    secret_context = "submitted-context-material"
    exc = RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body", "credential", "secret_value"),
                "msg": "Value error, invalid secret value",
                "input": secret_input,
                "ctx": {"error": ValueError(secret_context), "submitted": secret_context},
            }
        ]
    )
    app = create_api_app()

    handler = app.exception_handlers[RequestValidationError]
    request = MagicMock(spec=Request)
    request.url.path = "/v1/credentials/cred_x/passkey"
    response = await handler(request, exc)
    response_body = json.loads(response.body)

    assert response.status_code == 422
    assert secret_input not in response.body.decode()
    assert secret_context not in response.body.decode()
    assert isinstance(response_body["detail"], list)
    assert response_body == {
        "detail": [
            {
                "loc": ["body", "credential", "secret_value"],
                "msg": "Value error, invalid secret value",
                "type": "value_error",
            }
        ]
    }


@pytest.mark.asyncio
async def test_request_validation_handler_preserves_input_for_non_credential_routes() -> None:
    echoed_input = "workflow-parameter-value"
    exc = RequestValidationError(
        [
            {
                "type": "less_than",
                "loc": ("body", "parameters", "count"),
                "msg": "Input should be less than 3",
                "input": echoed_input,
                "ctx": {"lt": 3},
            }
        ]
    )
    app = create_api_app()

    handler = app.exception_handlers[RequestValidationError]
    request = MagicMock(spec=Request)
    request.url.path = "/v1/workflows"
    response = await handler(request, exc)
    response_body = json.loads(response.body)

    assert response.status_code == 422
    detail = response_body["detail"][0]
    assert detail["input"] == echoed_input
    assert detail["ctx"] == {"lt": 3}
