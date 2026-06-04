"""Tests for error_type field on UserDefinedError and SkyvernDefinedError (SKY-7619)."""

from skyvern.errors.errors import (
    ErrorType,
    ReachMaxStepsError,
    SkyvernDefinedError,
    UserDefinedError,
)


def test_user_defined_error_has_user_error_type() -> None:
    error = UserDefinedError(error_code="invalid_credentials", reasoning="Bad creds", confidence_float=1.0)
    assert error.error_type == ErrorType.USER_DEFINED_ERROR


def test_user_defined_error_type_serialized_in_model_dump() -> None:
    error = UserDefinedError(error_code="invalid_credentials", reasoning="Bad creds", confidence_float=1.0)
    dumped = error.model_dump()
    assert dumped["error_type"] == "USER_DEFINED_ERROR"


def test_skyvern_defined_error_has_system_error_type() -> None:
    error = SkyvernDefinedError(error_code="REACH_MAX_STEPS", reasoning="Max steps reached.")
    assert error.error_type == ErrorType.SYSTEM_DEFINED_ERROR


def test_skyvern_defined_error_type_serialized_in_model_dump() -> None:
    error = ReachMaxStepsError()
    dumped = error.model_dump()
    assert dumped["error_type"] == "SYSTEM_DEFINED_ERROR"


def test_to_user_defined_error_inherits_user_error_type() -> None:
    skyvern_error = ReachMaxStepsError()
    user_error = skyvern_error.to_user_defined_error()
    assert user_error.error_type == ErrorType.USER_DEFINED_ERROR
    assert user_error.model_dump()["error_type"] == "USER_DEFINED_ERROR"
