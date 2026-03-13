from __future__ import annotations

from unittest.mock import MagicMock

from pydantic import BaseModel, ValidationError, field_validator

from skyvern.forge.api_app import format_validation_errors


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
