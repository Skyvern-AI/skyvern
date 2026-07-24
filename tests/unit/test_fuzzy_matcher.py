import pytest

from skyvern.core.script_generations.fuzzy_matcher import match_option


@pytest.mark.parametrize(
    "candidate, options, expected",
    [
        ("Canada", ["Canada", "United States"], 0),
        ("united states", ["Canada", "United States"], 1),
        ("Bachelor's Degree", ["bachelors degree", "masters"], 0),
    ],
)
def test_match_option_exact(candidate: str, options: list[str], expected: int) -> None:
    """Pass 1: case-insensitive, apostrophe-normalized exact match."""
    assert match_option(candidate, options) == expected


@pytest.mark.parametrize(
    "candidate, options, expected",
    [
        ("United States", ["Canada", "United States of America"], 1),
        ("Computer Science and Engineering", ["Computer Science", "Biology"], 0),
    ],
)
def test_match_option_substring(candidate: str, options: list[str], expected: int) -> None:
    """Pass 2: substring containment in either direction."""
    assert match_option(candidate, options) == expected


@pytest.mark.parametrize(
    "candidate, options, expected",
    [
        ("bachelors", ["bachelor's degree", "masters"], 0),
        ("masters", ["bachelor", "master of science"], 1),
        ("masters", ["Masters Degree"], 0),
    ],
)
def test_match_option_stem(candidate: str, options: list[str], expected: int) -> None:
    """Pass 3: trailing-'s' stem match (e.g. "masters" <-> "master's")."""
    assert match_option(candidate, options) == expected


def test_match_option_word_overlap() -> None:
    """Pass 4: word-overlap scoring above the 50% threshold."""
    assert match_option("Master of Science", ["Bachelor of Arts", "Master of Arts"]) == 1


@pytest.mark.parametrize(
    "candidate, options",
    [
        # Short option codes collapse to a 1-char stem after rstrip("s") and used
        # to substring-match almost any candidate in Pass 3. They must NOT match.
        ("Australia", ["US", "AU"]),
        ("Tunisia", ["IS", "TN"]),
        ("Massachusetts", ["US", "MA"]),
        # An option normalizing to all "s" yields an empty stem, which used to
        # match every candidate ("" in anything is True).
        ("bachelor", ["ss", "Other"]),
    ],
)
def test_match_option_short_option_no_false_stem_match(candidate: str, options: list[str]) -> None:
    """Pass 3 must guard the option stem length, not just the candidate stem.

    Regression: without the guard, ``match_option("Australia", ["US", "AU"])``
    returned 0 (US) because "us".rstrip("s") -> "u" and "u" in "australia".
    Returning None lets the caller fall back to AI matching, which is correct.
    """
    assert match_option(candidate, options) is None


@pytest.mark.parametrize(
    "candidate, options, expected",
    [
        # A short option must still win on an exact match (Pass 1) even though
        # the stem-length guard skips it in Pass 3.
        ("AU", ["US", "AU"], 1),
        ("US", ["US", "Canada"], 0),
    ],
)
def test_match_option_short_option_exact_still_matches(candidate: str, options: list[str], expected: int) -> None:
    """The stem guard must not regress exact matches on short option codes."""
    assert match_option(candidate, options) == expected


@pytest.mark.parametrize(
    "candidate, options",
    [
        ("", ["Canada"]),
        ("Canada", []),
        ("???", ["Canada"]),
    ],
)
def test_match_option_no_match(candidate: str, options: list[str]) -> None:
    """Empty inputs and unmatchable candidates return None."""
    assert match_option(candidate, options) is None
