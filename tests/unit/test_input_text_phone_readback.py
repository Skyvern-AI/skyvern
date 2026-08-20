from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Error as PlaywrightError

from skyvern.exceptions import PhoneNumberInputMismatch
from skyvern.webeye.actions.handler import (
    _fill_nanp_tel_with_readback,
    _log_tel_fallback_fill_digit_counts,
    verify_phone_input_digits,
)
from skyvern.webeye.browser_engine import BrowserEngineMetadata, BrowserEngineSelection

NANP_DIGITS = "2245550199"
NANP_E164 = f"+1{NANP_DIGITS}"
_NOT_INPUTELEMENT_ERROR = "Node is not an HTMLInputElement, HTMLTextAreaElement or HTMLSelectElement"


class _EngineError(Exception):
    pass


async def _never_start() -> None:  # pragma: no cover
    raise AssertionError("start_driver must not be called")


def _engine_selection() -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name="engine-a",
        start_driver=_never_start,
        error_type=_EngineError,
        timeout_error_type=_EngineError,
        metadata=BrowserEngineMetadata(name="engine-a", version="0.0.0"),
        selection_reason="test",
    )


def _make_element(locator: MagicMock) -> MagicMock:
    element = MagicMock()
    element.get_locator.return_value = locator
    element.get_id.return_value = "element-id"
    element.input_sequentially = AsyncMock()
    element.input_clear = AsyncMock()
    element.input_fill = AsyncMock()
    return element


def _make_fill_element(readbacks: list[str]) -> MagicMock:
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=readbacks)
    return _make_element(locator)


@pytest.mark.asyncio
async def test_phone_readback_accepts_matching_ten_digits() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="(224) 555-0199")

    await verify_phone_input_digits(tag_name="input", locator=locator, expected_value="224-555-0199")


@pytest.mark.asyncio
async def test_phone_readback_accepts_single_country_code_with_source_evidence() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="+1 (224) 555-0199")

    await verify_phone_input_digits(
        tag_name="input",
        locator=locator,
        expected_value="2245550199",
        allow_nanp_country_prefix=True,
    )


@pytest.mark.asyncio
async def test_phone_readback_rejects_duplicated_country_code() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="+11 (224) 555-0199")

    with pytest.raises(PhoneNumberInputMismatch):
        await verify_phone_input_digits(
            tag_name="input",
            locator=locator,
            expected_value="2245550199",
            allow_nanp_country_prefix=True,
        )


@pytest.mark.asyncio
async def test_phone_readback_accepts_explicit_widget_nanp_rewrite_without_source_evidence() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="+1 (987) 555-0199")

    await verify_phone_input_digits(tag_name="input", locator=locator, expected_value="9875550199")


@pytest.mark.asyncio
async def test_phone_readback_rejects_bare_prepended_one_without_explicit_marker() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="19875550199")

    with pytest.raises(PhoneNumberInputMismatch):
        await verify_phone_input_digits(tag_name="input", locator=locator, expected_value="9875550199")


@pytest.mark.asyncio
async def test_phone_readback_rejects_widget_rewrite_violating_field_constraints() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="+1 (987) 555-0199")

    with pytest.raises(PhoneNumberInputMismatch):
        await verify_phone_input_digits(
            tag_name="input",
            locator=locator,
            expected_value="9875550199",
            pattern="[0-9]{10}",
        )

    with pytest.raises(PhoneNumberInputMismatch):
        await verify_phone_input_digits(
            tag_name="input",
            locator=locator,
            expected_value="9875550199",
            maxlength="10",
        )

    with pytest.raises(PhoneNumberInputMismatch):
        await verify_phone_input_digits(
            tag_name="input",
            locator=locator,
            expected_value="9875550199",
            pattern="",
        )


@pytest.mark.asyncio
async def test_phone_readback_rejects_trunk_one_without_plus_marker() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="1 (987) 555-0199")

    with pytest.raises(PhoneNumberInputMismatch):
        await verify_phone_input_digits(tag_name="input", locator=locator, expected_value="9875550199")


@pytest.mark.asyncio
async def test_phone_readback_digit_drop_raises() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="(224) 555-019")

    with pytest.raises(PhoneNumberInputMismatch) as exc:
        await verify_phone_input_digits(tag_name="input", locator=locator, expected_value="224-555-0199")

    assert exc.value.expected_digit_count == 10
    assert exc.value.actual_digit_count == 9


@pytest.mark.asyncio
async def test_phone_readback_threads_selected_engine_incompatible_error() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=_EngineError(_NOT_INPUTELEMENT_ERROR))

    with pytest.raises(PhoneNumberInputMismatch):
        await verify_phone_input_digits(
            tag_name="input",
            locator=locator,
            expected_value=NANP_DIGITS,
            engine_selection=_engine_selection(),
        )


@pytest.mark.asyncio
async def test_phone_readback_propagates_foreign_engine_error() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=PlaywrightError(_NOT_INPUTELEMENT_ERROR))

    with pytest.raises(PlaywrightError):
        await verify_phone_input_digits(
            tag_name="input",
            locator=locator,
            expected_value=NANP_DIGITS,
            engine_selection=_engine_selection(),
        )


@pytest.mark.asyncio
async def test_tel_fill_and_fallback_log_thread_engine_selection() -> None:
    selection = _engine_selection()
    element = _make_element(MagicMock())
    with patch(
        "skyvern.webeye.actions.handler.get_input_value",
        new=AsyncMock(side_effect=[NANP_DIGITS, NANP_DIGITS]),
    ) as get_input_value:
        assert (
            await _fill_nanp_tel_with_readback(
                skyvern_element=element,
                tag_name="input",
                national_digits=NANP_DIGITS,
                e164_fallback=NANP_E164,
                engine_selection=selection,
            )
            is None
        )
        await _log_tel_fallback_fill_digit_counts(
            skyvern_element=element,
            tag_name="input",
            expected_value=NANP_DIGITS,
            task_id="tsk_1",
            step_id="stp_1",
            engine_selection=selection,
        )

    assert [call.kwargs["engine_selection"] for call in get_input_value.await_args_list] == [selection, selection]


@pytest.mark.asyncio
async def test_nanp_fill_clean_first_attempt_skips_fallbacks() -> None:
    element = _make_fill_element(["(224) 555-0199"])

    mismatch = await _fill_nanp_tel_with_readback(
        skyvern_element=element,
        tag_name="input",
        national_digits=NANP_DIGITS,
        e164_fallback=NANP_E164,
    )

    assert mismatch is None
    element.input_sequentially.assert_awaited_once_with(text=NANP_DIGITS)
    element.input_clear.assert_not_awaited()
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_nanp_fill_recovers_same_length_substitution_with_atomic_national() -> None:
    element = _make_fill_element(["2245550198", "2245550199"])

    mismatch = await _fill_nanp_tel_with_readback(
        skyvern_element=element,
        tag_name="input",
        national_digits=NANP_DIGITS,
        e164_fallback=NANP_E164,
    )

    assert mismatch is None
    element.input_clear.assert_awaited_once()
    element.input_fill.assert_awaited_once_with(text=NANP_DIGITS)


@pytest.mark.asyncio
async def test_nanp_fill_returns_final_mismatch_when_all_safe_attempts_fail() -> None:
    element = _make_fill_element(["+44 22 4555 0199", "+44 22 4555 0199", "+44 22 4555 0199"])

    mismatch = await _fill_nanp_tel_with_readback(
        skyvern_element=element,
        tag_name="input",
        national_digits=NANP_DIGITS,
        e164_fallback=NANP_E164,
    )

    assert mismatch is not None
    assert mismatch.expected_digit_count == 10
    assert mismatch.actual_digit_count == 12


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
