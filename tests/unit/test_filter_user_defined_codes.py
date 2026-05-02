"""Tests for filter_to_user_defined_codes whitelist filter.

Reproduces SKY-9425: LLM-based summarization prompts can hallucinate codes
from the failure_categories taxonomy (e.g. LLM_REASONING_ERROR) into the
user-defined errors array. The filter drops anything not in the user's
error_code_mapping so only declared codes leak through to the user.
"""

from skyvern.errors.errors import UserDefinedError, filter_to_user_defined_codes


def _make_error(code: str) -> UserDefinedError:
    return UserDefinedError(error_code=code, reasoning="r", confidence_float=0.8)


def test_filter_keeps_codes_present_in_mapping() -> None:
    errors = [_make_error("DATA_UNAVAILABLE"), _make_error("OTHER_USER_CODE")]
    mapping = {"DATA_UNAVAILABLE": "x", "OTHER_USER_CODE": "y"}

    kept, dropped = filter_to_user_defined_codes(errors, mapping)

    assert [e.error_code for e in kept] == ["DATA_UNAVAILABLE", "OTHER_USER_CODE"]
    assert dropped == []


def test_filter_drops_failure_category_codes_not_in_mapping() -> None:
    errors = [_make_error("LLM_REASONING_ERROR"), _make_error("DATA_UNAVAILABLE")]
    mapping = {"DATA_UNAVAILABLE": "if month/year not in dropdown, terminate"}

    kept, dropped = filter_to_user_defined_codes(errors, mapping)

    assert [e.error_code for e in kept] == ["DATA_UNAVAILABLE"]
    assert dropped == ["LLM_REASONING_ERROR"]


def test_filter_returns_empty_when_mapping_is_none() -> None:
    errors = [_make_error("ANYTHING")]

    kept, dropped = filter_to_user_defined_codes(errors, None)

    assert kept == []
    assert dropped == ["ANYTHING"]


def test_filter_returns_empty_when_mapping_is_empty_dict() -> None:
    errors = [_make_error("ANYTHING")]

    kept, dropped = filter_to_user_defined_codes(errors, {})

    assert kept == []
    assert dropped == ["ANYTHING"]


def test_filter_handles_empty_errors_input() -> None:
    kept, dropped = filter_to_user_defined_codes([], {"FOO": "bar"})

    assert kept == []
    assert dropped == []


def test_filter_is_case_sensitive() -> None:
    """Codes must match exactly — mapping is case-sensitive by design."""
    errors = [_make_error("data_unavailable")]
    mapping = {"DATA_UNAVAILABLE": "x"}

    kept, dropped = filter_to_user_defined_codes(errors, mapping)

    assert kept == []
    assert dropped == ["data_unavailable"]
