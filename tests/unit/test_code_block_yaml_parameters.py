from __future__ import annotations

import pytest
from pydantic import ValidationError

from skyvern.schemas.workflows import CodeBlockYAML


def test_parameters_field_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CodeBlockYAML.model_validate(
            {
                "label": "run_code",
                "code": "print(foo)",
                "parameters": ["foo"],
            }
        )

    assert "parameter_keys" in str(exc_info.value)


def test_parameter_keys_field_is_accepted() -> None:
    block = CodeBlockYAML.model_validate(
        {
            "label": "run_code",
            "code": "print(foo)",
            "parameter_keys": ["foo"],
        }
    )

    assert block.parameter_keys == ["foo"]


def test_no_parameter_field_parses() -> None:
    block = CodeBlockYAML.model_validate(
        {
            "label": "run_code",
            "code": "print('hello')",
        }
    )

    assert block.parameter_keys is None
