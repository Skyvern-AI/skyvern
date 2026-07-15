from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.exceptions import PhoneNumberInputMismatch
from skyvern.webeye.actions.handler import _log_tel_fallback_fill_digit_counts, verify_phone_input_digits


def _make_element(locator: MagicMock) -> MagicMock:
    element = MagicMock()
    element.get_locator.return_value = locator
    element.get_id.return_value = "element-id"
    return element


@pytest.mark.asyncio
async def test_phone_readback_accepts_matching_ten_digits() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="(224) 555-0199")

    await verify_phone_input_digits(tag_name="input", locator=locator, expected_value="224-555-0199")


@pytest.mark.parametrize("actual_value", ["+1 (224) 555-0199", "+11 (224) 555-0199"])
@pytest.mark.asyncio
async def test_phone_readback_accepts_country_code_readback(actual_value: str) -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value=actual_value)

    await verify_phone_input_digits(tag_name="input", locator=locator, expected_value="2245550199")


@pytest.mark.asyncio
async def test_phone_readback_digit_drop_raises() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="(224) 555-019")

    with pytest.raises(PhoneNumberInputMismatch) as exc:
        await verify_phone_input_digits(tag_name="input", locator=locator, expected_value="224-555-0199")

    assert exc.value.expected_digit_count == 10
    assert exc.value.actual_digit_count == 9


@pytest.mark.asyncio
async def test_tel_fallback_digit_count_log_never_raises_on_mismatch() -> None:
    # A 10 -> 9 digit drop on the LLM-fallback fill is observed and logged, never raised.
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="(224) 555-019")

    await _log_tel_fallback_fill_digit_counts(
        skyvern_element=_make_element(locator),
        tag_name="input",
        expected_value="(224) 555-0199",
        task_id="tsk_1",
        step_id="stp_1",
    )


@pytest.mark.asyncio
async def test_tel_fallback_digit_count_log_swallows_read_errors() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=RuntimeError("element detached"))

    await _log_tel_fallback_fill_digit_counts(
        skyvern_element=_make_element(locator),
        tag_name="input",
        expected_value="2245550199",
        task_id="tsk_1",
        step_id="stp_1",
    )
