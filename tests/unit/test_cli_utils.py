from __future__ import annotations

import pytest

from skyvern.cli.utils import strip_quotes


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("plain", "plain"),
        ("'value'", "value"),
        ('"value"', "value"),
        ("'\"value\"'", "value"),
        ("\"'value'\"", "value"),
        ("'", "'"),
        ('"', '"'),
        ("\"value'", "\"value'"),
    ],
    ids=[
        "none",
        "empty",
        "plain",
        "single_quoted",
        "double_quoted",
        "nested_double_in_single",
        "nested_single_in_double",
        "lone_single_quote",
        "lone_double_quote",
        "mismatched_pair",
    ],
)
def test_strip_quotes(value: str | None, expected: str) -> None:
    assert strip_quotes(value) == expected
