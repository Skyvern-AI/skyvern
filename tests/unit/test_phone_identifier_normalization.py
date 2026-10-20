import pytest

from skyvern.utils.phone_validation import (
    is_e164_phone_number,
    looks_like_phone_identifier,
    normalize_identifier,
    normalize_phone_identifier,
    phone_identifier_candidates,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("4155552671", "4155552671", id="bare-ten-digit"),
        pytest.param("14155552671", "+14155552671", id="leading-one-eleven-digit"),
        pytest.param("+14155552671", "+14155552671", id="already-e164"),
        pytest.param("(415) 555-2671", "(415) 555-2671", id="country-ambiguous"),
    ],
)
def test_normalize_phone_identifier_only_normalizes_explicit_country_codes(value: str, expected: str) -> None:
    assert normalize_phone_identifier(value) == expected


def test_normalize_phone_identifier_does_not_guess_non_nanp_country_code() -> None:
    assert normalize_phone_identifier("44 20 7946 0958") == "44 20 7946 0958"


@pytest.mark.parametrize("value", ["+14155552671", "+442079460958", "+6831234"])
def test_is_e164_phone_number_accepts_normalized_international_numbers(value: str) -> None:
    assert is_e164_phone_number(value)


@pytest.mark.parametrize("value", ["4155552671", "+", "+01234567", "+1415555267100000"])
def test_is_e164_phone_number_rejects_ambiguous_or_invalid_numbers(value: str) -> None:
    assert not is_e164_phone_number(value)


def test_email_is_not_treated_as_phone_identifier() -> None:
    assert not looks_like_phone_identifier("Tenant.User@example.com")
    assert normalize_identifier(" Tenant.User@example.com ") == "tenant.user@example.com"


@pytest.mark.parametrize("value", ["+1-EXT-415-555-2671", "+1--415-555-2671"])
def test_malformed_phone_identifier_is_rejected_without_lossy_normalization(value: str) -> None:
    assert not looks_like_phone_identifier(value)
    assert normalize_phone_identifier(value) == value


def test_phone_identifier_candidates_do_not_guess_legacy_country_code() -> None:
    assert phone_identifier_candidates("+1 (415) 555-2671") == [
        "+1 (415) 555-2671",
        "+14155552671",
        "14155552671",
    ]


def test_phone_identifier_candidates_do_not_add_legacy_candidate_for_non_nanp_number() -> None:
    assert phone_identifier_candidates("+44 20 7946 0958") == [
        "+44 20 7946 0958",
        "+442079460958",
        "442079460958",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("123-456", False, id="six-digits-with-punctuation"),
        pytest.param("123 456", False, id="six-digits-with-whitespace"),
        pytest.param("123-4567", True, id="seven-digits-with-punctuation"),
    ],
)
def test_phone_detection_counts_digits_not_formatted_characters(value: str, expected: bool) -> None:
    assert looks_like_phone_identifier(value) is expected


def test_short_numeric_labels_do_not_generate_punctuation_free_candidates() -> None:
    assert phone_identifier_candidates("123-456") == ["123-456"]
