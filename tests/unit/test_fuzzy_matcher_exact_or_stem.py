from __future__ import annotations

import pytest

from skyvern.core.script_generations.fuzzy_matcher import match_option_exact_or_stem


@pytest.mark.parametrize(
    ("candidate", "options", "expected"),
    [
        ("United States", ["Canada", "United States"], 1),
        ("united states", ["Canada", "United States"], 1),
        ("O'Reilly", ["O'Reilly Auto", "OReilly"], 1),
        ("Departments", ["Department", "Role"], 0),
        ("bachelors", ["Bachelor", "Master"], 0),
    ],
)
def test_returns_index_on_exact_or_clean_stem(candidate: str, options: list[str], expected: int) -> None:
    assert match_option_exact_or_stem(candidate, options) == expected


@pytest.mark.parametrize(
    ("candidate", "options"),
    [
        ("", ["United States"]),
        ("United States", []),
        ("United States", ["United States", "United States"]),
        ("Senior", ["Senior Vice President", "Associate"]),
        ("New York", ["New York City", "New Hampshire"]),
        ("us", ["use", "user"]),
        ("offices", ["office", "Office"]),
    ],
)
def test_returns_none_when_not_safely_resolvable(candidate: str, options: list[str]) -> None:
    assert match_option_exact_or_stem(candidate, options) is None
