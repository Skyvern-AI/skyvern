from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.exceptions import PhoneNumberInputMismatch
from skyvern.webeye.actions.handler import verify_phone_input_digits


@pytest.mark.asyncio
async def test_phone_readback_accepts_matching_ten_digits() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="(224) 555-0199")

    # Matching digit counts must not raise.
    await verify_phone_input_digits(tag_name="input", locator=locator, expected_value="224-555-0199")


@pytest.mark.asyncio
async def test_phone_readback_mismatch_raises() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="(224) 555-019")

    with pytest.raises(PhoneNumberInputMismatch) as exc:
        await verify_phone_input_digits(tag_name="input", locator=locator, expected_value="224-555-0199")

    assert exc.value.expected_digit_count == 10
    assert exc.value.actual_digit_count == 9
