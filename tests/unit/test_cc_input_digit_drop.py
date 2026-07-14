from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.actions.handler import (
    _card_readback_is_mismatch,
    _card_readback_matches,
    _fill_card_number_with_readback,
    _has_card_number_token,
    _is_card_number_field,
    _is_probable_card_number,
)
from skyvern.webeye.actions.responses import ActionFailure

# Reserved Luhn-valid test PANs (no real cardholder data).
VISA_16 = "4539578763621486"
AMEX_15 = "378282246310005"


@pytest.mark.parametrize(
    "digits,expected",
    [
        (VISA_16, True),  # 16-digit Visa, Luhn ok
        (AMEX_15, True),  # 15-digit Amex, Luhn ok
        ("4111111111111111", True),  # 16-digit test Visa, Luhn ok
        ("4917484589897107", True),  # 16-digit, Luhn ok
        ("4539578763621487", False),  # 16 digits, Luhn fails (last digit off)
        ("2245550199", False),  # 10-digit phone number, not card-length
        ("123456789012", False),  # 12 digits, below the card floor
        ("12345678901234567890", False),  # 20 digits, above the card ceiling
        ("", False),
    ],
)
def test_is_probable_card_number(digits: str, expected: bool) -> None:
    assert _is_probable_card_number(digits) is expected


@pytest.mark.parametrize(
    "actual_value,is_mismatch",
    [
        ("4539 5787 6362 1486", False),  # correct digits, formatted -> match
        ("4539578763621486", False),  # correct digits, unformatted -> match
        ("4539 5876 6214 6837", True),  # same length, scrambled digits -> mismatch
        ("4539 5787 6362 148", True),  # a digit dropped -> mismatch
        ("•••• •••• •••• 1486", False),  # masked read-back, cannot compare -> not a mismatch
        ("4539 88XX XXXX 1486", False),  # partially masked with letters -> not a mismatch
        ("", False),  # empty read-back -> not a mismatch
        (None, False),  # unreadable -> not a mismatch
        ("4539.5787.6362.1486", False),  # dot-formatted correct -> match
        ("4539.5876.6214.6837", True),  # dot-formatted scramble -> mismatch (separator-class fix)
        ("4539/5876/6214/6837", True),  # slash-formatted scramble -> mismatch
        ("4539\xa05787\xa06362\xa01486", False),  # NBSP-formatted correct -> match
        ("4539.5787.6362.148", True),  # dot-formatted dropped digit -> still mismatch
    ],
)
def test_card_readback_is_mismatch(actual_value: str | None, is_mismatch: bool) -> None:
    assert _card_readback_is_mismatch(VISA_16, actual_value) is is_mismatch


@pytest.mark.parametrize(
    "actual_value,matches",
    [
        ("4539 5787 6362 1486", True),  # correct, space-formatted
        ("4539.5787.6362.1486", True),  # correct, dot-formatted
        ("4539\xa05787\xa06362\xa01486", True),  # correct, NBSP-formatted
        ("4539578763621486", True),  # correct, unformatted
        ("4539 5876 6214 6837", False),  # scrambled -> not a positive match
        ("4539 5787 6362 148", False),  # dropped digit -> not a positive match
        ("•••• •••• •••• 1486", False),  # masked -> not a positive match
        ("", False),  # empty -> not a positive match
        (None, False),  # unreadable -> not a positive match
    ],
)
def test_card_readback_matches(actual_value: str | None, matches: bool) -> None:
    assert _card_readback_matches(VISA_16, actual_value) is matches


def _make_card_element(readbacks: list[str | None]) -> MagicMock:
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=list(readbacks))
    element = MagicMock()
    element.get_locator = MagicMock(return_value=locator)
    element.get_id = MagicMock(return_value="card")
    element.input_sequentially = AsyncMock()
    element.input_clear = AsyncMock()
    element.input_fill = AsyncMock()
    return element


@pytest.mark.asyncio
async def test_fill_card_number_recovers_scramble_with_atomic_fill() -> None:
    # First (character-by-character) fill scrambles the digits; the atomic re-entry renders them
    # correctly, so the helper recovers with no failure.
    element = _make_card_element(["4539 5876 6214 6837", "4539 5787 6362 1486"])

    result = await _fill_card_number_with_readback(
        skyvern_element=element, tag_name="input", text=VISA_16, expected_digits=VISA_16
    )

    assert result is None
    element.input_sequentially.assert_awaited_once_with(text=VISA_16)
    element.input_clear.assert_awaited_once()
    element.input_fill.assert_awaited_once_with(text=VISA_16)


@pytest.mark.asyncio
async def test_fill_card_number_clean_first_try_skips_retry() -> None:
    element = _make_card_element(["4539 5787 6362 1486"])

    result = await _fill_card_number_with_readback(
        skyvern_element=element, tag_name="input", text=VISA_16, expected_digits=VISA_16
    )

    assert result is None
    element.input_clear.assert_not_awaited()
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_card_number_fails_after_second_mismatch() -> None:
    element = _make_card_element(["4539 5876 6214 6837", "4539 5876 6214 6837"])

    result = await _fill_card_number_with_readback(
        skyvern_element=element, tag_name="input", text=VISA_16, expected_digits=VISA_16
    )

    assert isinstance(result, ActionFailure)
    assert result.success is False
    assert result.exception_type == "CardNumberInputMismatch"
    element.input_fill.assert_awaited_once_with(text=VISA_16)


@pytest.mark.asyncio
async def test_fill_card_number_masked_readback_does_not_retype() -> None:
    # A masked field cannot be read back, so the helper must not clear/retype a possibly-correct value.
    element = _make_card_element(["•••• •••• •••• 1486"])

    result = await _fill_card_number_with_readback(
        skyvern_element=element, tag_name="input", text=VISA_16, expected_digits=VISA_16
    )

    assert result is None
    element.input_clear.assert_not_awaited()
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_card_number_fails_when_retry_readback_empty() -> None:
    # After a confirmed first mismatch we cleared a known-bad value; if the retry read-back is empty
    # (fill rejected/async-cleared) success must NOT be assumed -- fail loudly instead of silently
    # proceeding with an unverified card.
    element = _make_card_element(["4539 5876 6214 6837", ""])

    result = await _fill_card_number_with_readback(
        skyvern_element=element, tag_name="input", text=VISA_16, expected_digits=VISA_16
    )

    assert isinstance(result, ActionFailure)
    assert result.exception_type == "CardNumberInputMismatch"
    element.input_fill.assert_awaited_once_with(text=VISA_16)


@pytest.mark.asyncio
async def test_fill_card_number_fails_when_retry_readback_masked() -> None:
    # Same guarantee when the retry read-back is masked/unreadable: a positive digit match is required.
    element = _make_card_element(["4539 5876 6214 6837", "•••• •••• •••• 1486"])

    result = await _fill_card_number_with_readback(
        skyvern_element=element, tag_name="input", text=VISA_16, expected_digits=VISA_16
    )

    assert isinstance(result, ActionFailure)
    assert result.exception_type == "CardNumberInputMismatch"
    element.input_fill.assert_awaited_once_with(text=VISA_16)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attrs,expected",
    [
        ({"autocomplete": "cc-number"}, True),  # explicit cc-number token
        ({"autocomplete": "section-billing cc-number"}, True),  # compound autocomplete token
        ({"autocomplete": None, "name": "card.number"}, True),  # hosted checkout tel card field
        ({"autocomplete": None, "id": "card-number"}, True),  # card-number id token
        ({"autocomplete": None, "name": "cardNumber"}, True),  # camelCase name (unseparated + mixed case)
        ({"autocomplete": None, "id": "cardnumber"}, True),  # unseparated id token
        ({"autocomplete": None, "name": "cc-number"}, True),  # cc-number as a name/id
        ({"autocomplete": None, "inputmode": "numeric"}, True),  # numeric-only field
        ({"autocomplete": "off", "inputmode": "numeric"}, True),  # numeric wins over autocomplete=off
        ({"autocomplete": None, "name": "phone", "inputmode": "tel"}, False),  # phone field is not a card field
        ({"autocomplete": None, "name": "cardholder"}, False),  # cardholder is not a card-number field
        ({"autocomplete": "email", "inputmode": "text"}, False),  # neither signal
        ({"autocomplete": None, "inputmode": None}, False),  # no signals
    ],
)
async def test_is_card_number_field(attrs: dict[str, str | None], expected: bool) -> None:
    element = MagicMock()
    element.get_attr = AsyncMock(side_effect=lambda name, **kwargs: attrs.get(name))
    assert await _is_card_number_field(element) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("card.number", True),  # dot separator
        ("card-number", True),  # dash separator
        ("card_number", True),  # underscore separator
        ("cardNumber", True),  # camelCase, unseparated
        ("cardnumber", True),  # unseparated lowercase
        ("CardNumber", True),  # unseparated mixed case
        ("cc-number", True),  # cc-number form
        ("ccNumber", True),  # cc camelCase
        ("number", False),  # bare "number" is not enough
        ("phone", False),
        ("cardholder", False),  # card, but not a card *number*
        ("", False),
        (None, False),
    ],
)
def test_has_card_number_token(value: str | None, expected: bool) -> None:
    assert _has_card_number_token(value) is expected
