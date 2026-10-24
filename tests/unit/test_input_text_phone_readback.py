from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Error as PlaywrightError

from skyvern.exceptions import (
    InvalidElementForTextInput,
    PhoneNumberInputBrowserInteractionFailed,
    PhoneNumberInputBrowserValidityMismatch,
    PhoneNumberInputCountryCodeMismatch,
    PhoneNumberInputMismatch,
)
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import InputTextAction, TelInputOutcome, TelInputStrategy
from skyvern.webeye.actions.handler import (
    _fill_nanp_tel_with_readback,
    _input_target_log_fields,
    _read_back_tel_fallback_fill,
    _recommendation_drops_dial_code,
    _tel_fill_recovers_with_source_number,
    check_phone_number_format,
    verify_phone_input_digits,
)
from skyvern.webeye.browser_engine import BrowserEngineMetadata, BrowserEngineSelection
from tests.unit.helpers import make_organization, make_step, make_task

NANP_DIGITS = "2245550199"
NANP_E164 = f"+1{NANP_DIGITS}"
_NOT_INPUTELEMENT_ERROR = "Node is not an HTMLInputElement, HTMLTextAreaElement or HTMLSelectElement"
_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(
    _NOW,
    _ORG,
    navigation_payload={"phone": NANP_E164},
    navigation_goal="Fill out the job application form",
)
_STEP = make_step(_NOW, _TASK, step_id="stp-1", status=StepStatus.created, order=0, output=None)


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


def test_tel_target_log_fields_never_include_phone_value() -> None:
    assert _input_target_log_fields(is_tel=True, text=NANP_DIGITS) == {"target_digit_count": 10}
    assert _input_target_log_fields(is_tel=False, text="query") == {"target_value": "query"}


@pytest.mark.asyncio
async def test_nanp_fill_sanitizes_browser_interaction_failure() -> None:
    element = _make_fill_element([])
    element.input_sequentially = AsyncMock(side_effect=RuntimeError(NANP_DIGITS))

    with patch("skyvern.webeye.actions.handler.LOG.warning") as warning_log:
        failure = await _fill_nanp_tel_with_readback(
            skyvern_element=element,
            tag_name="input",
            national_digits=NANP_DIGITS,
            e164_fallback=None,
        )

    assert isinstance(failure, PhoneNumberInputBrowserInteractionFailed)
    assert warning_log.call_args.kwargs == {"error_type": "RuntimeError"}
    assert NANP_DIGITS not in str(warning_log.call_args)


@pytest.mark.asyncio
async def test_nanp_fill_preserves_invalid_element_failure() -> None:
    element = _make_fill_element(["2245550198"])
    element.input_clear = AsyncMock(side_effect=InvalidElementForTextInput(element_id="element-id", tag_name="input"))

    failure = await _fill_nanp_tel_with_readback(
        skyvern_element=element,
        tag_name="input",
        national_digits=NANP_DIGITS,
        e164_fallback=None,
    )

    assert isinstance(failure, InvalidElementForTextInput)


@pytest.mark.asyncio
async def test_tel_fallback_readback_sanitizes_exception() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=RuntimeError(NANP_DIGITS))

    with patch("skyvern.webeye.actions.handler.LOG.warning") as warning_log:
        readback = await _read_back_tel_fallback_fill(
            skyvern_element=_make_element(locator),
            tag_name="input",
            expected_value=NANP_DIGITS,
            task_id="tsk_1",
            step_id="stp_1",
        )

    assert readback == (10, None, None, False)
    assert warning_log.call_args.kwargs == {
        "task_id": "tsk_1",
        "step_id": "stp_1",
        "error_type": "RuntimeError",
    }
    assert NANP_DIGITS not in str(warning_log.call_args)


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
        await _read_back_tel_fallback_fill(
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
async def test_nanp_fill_fails_when_browser_validity_stays_false_across_ladder() -> None:
    element = _make_fill_element([NANP_DIGITS, NANP_DIGITS, NANP_DIGITS])
    element.get_locator.return_value.evaluate = AsyncMock(side_effect=[False, False, False])
    outcome = TelInputOutcome(
        flag_enabled=True,
        final_element_id="element-id",
        strategy=TelInputStrategy.legacy_sequential,
        expected_digit_count=10,
        attempt_count=0,
        retargeted=False,
    )

    with patch("skyvern.webeye.actions.handler.LOG.info") as log_info:
        failure = await _fill_nanp_tel_with_readback(
            skyvern_element=element,
            tag_name="input",
            national_digits=NANP_DIGITS,
            e164_fallback=NANP_E164,
            outcome=outcome,
            enforce_browser_validity=True,
        )

    assert isinstance(failure, PhoneNumberInputBrowserValidityMismatch)
    assert outcome.attempt_count == 3
    assert outcome.strategy is TelInputStrategy.atomic_e164
    assert outcome.actual_digit_count == 10
    assert outcome.browser_valid is False
    assert element.input_sequentially.await_count == 1
    assert element.input_clear.await_count == 2
    assert [call.kwargs["text"] for call in element.input_fill.await_args_list] == [NANP_DIGITS, NANP_E164]
    assert all(
        NANP_DIGITS not in str(log_call) and NANP_E164 not in str(log_call) for log_call in log_info.call_args_list
    )


@pytest.mark.asyncio
async def test_nanp_fill_unknown_browser_validity_preserves_success() -> None:
    element = _make_fill_element([NANP_DIGITS])
    element.get_locator.return_value.evaluate = AsyncMock(return_value=None)
    outcome = TelInputOutcome(
        flag_enabled=True,
        final_element_id="element-id",
        strategy=TelInputStrategy.legacy_sequential,
        expected_digit_count=10,
        attempt_count=0,
        retargeted=False,
    )

    failure = await _fill_nanp_tel_with_readback(
        skyvern_element=element,
        tag_name="input",
        national_digits=NANP_DIGITS,
        e164_fallback=NANP_E164,
        outcome=outcome,
        enforce_browser_validity=True,
    )

    assert failure is None
    assert outcome.attempt_count == 1
    assert outcome.strategy is TelInputStrategy.sequential_national
    assert outcome.actual_digit_count == 10
    assert outcome.browser_valid is None


@pytest.mark.asyncio
async def test_tel_fallback_digit_count_log_never_raises_on_mismatch() -> None:
    # A 10 -> 9 digit drop on the LLM-fallback fill is observed and logged, never raised.
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value="(224) 555-019")

    readback = await _read_back_tel_fallback_fill(
        skyvern_element=_make_element(locator),
        tag_name="input",
        expected_value="(224) 555-0199",
        task_id="tsk_1",
        step_id="stp_1",
    )

    assert readback.country_code_mismatch is None


@pytest.mark.asyncio
async def test_tel_fallback_digit_count_log_swallows_read_errors() -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=RuntimeError("element detached"))

    await _read_back_tel_fallback_fill(
        skyvern_element=_make_element(locator),
        tag_name="input",
        expected_value="2245550199",
        task_id="tsk_1",
        step_id="stp_1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected_value", "readback", "country_code_mismatch"),
    [
        # SKY-18890: the dial code came off the front, leaving the national digits behind the "+".
        (f"+1 {NANP_DIGITS}", f"+{NANP_DIGITS}", True),
        # The widget treated a UK national trunk prefix as the calling code.
        ("+44 7911 123456", "+07911 123456", True),
        # A "+" in front of the digits given, with nothing saying they are national: a number given
        # as an international one without its "+" reads exactly the same way.
        (NANP_DIGITS, f"+{NANP_DIGITS}", False),
        ("447911123456", "+447911123456", False),
        # The widget moved the dial code into its picker and kept the national digits.
        ("+1 (224) 555-0199", "(224) 555-0199", False),
        # The field regrouped the whole international number it was given.
        ("+1 (224) 555-0199", "+1 224-555-0199", False),
        # The field resolved the national digits to an international number of its own.
        (NANP_DIGITS, NANP_E164, False),
        # Shortenings that keep the number: a normalized trunk zero and a trimmed extension.
        ("+44 (0) 7911 123456", "+447911123456", False),
        (f"{NANP_E164} x12", NANP_E164, False),
        # Too few digits to be read as a country calling code plus a number.
        ("5550199", "+5550199", False),
        # An empty field is not a country the number landed under.
        (NANP_E164, "+", False),
    ],
)
async def test_tel_fallback_readback_reports_a_wrong_international_reading(
    expected_value: str, readback: str, country_code_mismatch: bool
) -> None:
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value=readback)
    element = _make_element(locator)
    element.get_attr = AsyncMock(return_value=None)

    result = await _read_back_tel_fallback_fill(
        skyvern_element=element,
        tag_name="input",
        expected_value=expected_value,
        task_id="tsk_1",
        step_id="stp_1",
    )

    assert isinstance(result.country_code_mismatch, PhoneNumberInputCountryCodeMismatch) is country_code_mismatch


@pytest.mark.asyncio
async def test_nanp_fill_recovers_plus_prefixed_national_digits_with_e164() -> None:
    # The ladder types national digits, which an international field reads as a country calling code
    # plus a number. The digits match, so only the withheld "+1" says the country is wrong; the E.164
    # attempt puts it back.
    element = _make_fill_element([f"+{NANP_DIGITS}", f"+{NANP_DIGITS}", NANP_E164])

    mismatch = await _fill_nanp_tel_with_readback(
        skyvern_element=element,
        tag_name="input",
        national_digits=NANP_DIGITS,
        e164_fallback=NANP_E164,
    )

    assert mismatch is None
    assert [call.kwargs["text"] for call in element.input_fill.await_args_list] == [NANP_DIGITS, NANP_E164]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_value", "retry_readback", "recovered"),
    [
        (NANP_E164, NANP_E164, True),
        # The field keeps reading its own leading digits as the calling code, so nothing recovered it.
        (NANP_E164, f"+{NANP_DIGITS}", False),
        # A truncated or substituted retry is not a successful recovery.
        (NANP_E164, f"+1{NANP_DIGITS[:-1]}", False),
        (NANP_E164, f"+1{NANP_DIGITS[:-1]}8", False),
        # Explicitly marked optional trunk zero and a constraint-valid NANP +1 rewrite are allowed.
        ("+44 (0) 7911 123456", "+447911123456", True),
        (NANP_DIGITS, NANP_E164, True),
    ],
)
async def test_tel_fill_recovery_re_enters_the_number_as_given(
    source_value: str, retry_readback: str, recovered: bool
) -> None:
    element = _make_fill_element([retry_readback])
    element.get_attr = AsyncMock(return_value=None)

    assert (
        await _tel_fill_recovers_with_source_number(
            skyvern_element=element,
            tag_name="input",
            source_value=source_value,
            task_id="tsk_1",
            step_id="stp_1",
        )
        is recovered
    )
    element.input_clear.assert_awaited_once()
    element.input_fill.assert_awaited_once_with(text=source_value)


@pytest.mark.asyncio
async def test_tel_fill_recovery_fails_when_source_readback_is_unavailable() -> None:
    element = _make_fill_element([])

    assert not await _tel_fill_recovers_with_source_number(
        skyvern_element=element,
        tag_name="input",
        source_value=NANP_E164,
        task_id="tsk_1",
        step_id="stp_1",
    )


def test_recommendation_drops_dial_code_with_national_trunk_prefix() -> None:
    assert _recommendation_drops_dial_code("+44 7911 123456", "07911 123456")


@pytest.mark.asyncio
async def test_phone_readback_keeps_plus_prefixed_digits_without_withheld_evidence() -> None:
    # Same read-back, no withheld country calling code: the digits given may themselves be an
    # international number, so the entry is not called wrong.
    locator = MagicMock()
    locator.input_value = AsyncMock(return_value=f"+{NANP_DIGITS}")

    await verify_phone_input_digits(tag_name="input", locator=locator, expected_value=NANP_DIGITS)


def _format_check_element(placeholder: str | None, autocomplete: str | None) -> MagicMock:
    element = _make_element(MagicMock())
    element.get_attr = AsyncMock(
        side_effect=lambda name, **_: {"placeholder": placeholder, "autocomplete": autocomplete}[name]
    )
    return element


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_current_value", "placeholder", "autocomplete", "expected_text"),
    [
        # SKY-18890: an international field whose own box holds the dial code keeps it, whether the box
        # shows the picker's dial code or an earlier attempt at the number.
        ("+48", None, None, NANP_E164),
        ("+48 605 550 199", None, None, NANP_E164),
        ("", "+1 (201) 555-0123", None, NANP_E164),
        ("", None, "tel", NANP_E164),
        # A national-number box next to a country-code element still drops the dial code.
        ("", "(201) 555-0123", None, NANP_DIGITS),
        ("(201) 555-0123", None, None, NANP_DIGITS),
        ("+48", None, "tel-national", NANP_DIGITS),
        # A placeholder whose "+" is prose, not a dial code.
        ("", "+ Add phone", None, NANP_DIGITS),
    ],
)
async def test_phone_format_check_keeps_the_dial_code_of_an_international_field(
    field_current_value: str, placeholder: str | None, autocomplete: str | None, expected_text: str
) -> None:
    element = _format_check_element(placeholder, autocomplete)
    llm_response = {
        "page_info": "a phone field with a country picker",
        "is_phone_number_input": True,
        "thought": "a country picker exists, so the country code is handled elsewhere",
        "phone_number_format": "national digits only",
        "is_current_format_correct": False,
        "recommended_phone_number": NANP_DIGITS,
    }

    with (
        patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
        patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
        patch(
            "skyvern.webeye.actions.handler.get_org_aware_secondary_llm_api_handler",
            new=MagicMock(return_value=AsyncMock(return_value=llm_response)),
        ),
    ):
        mock_prompt.load_prompt.return_value = "mocked prompt"
        mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)
        scraped_page = MagicMock()
        scraped_page.generate_scraped_page_without_screenshots = AsyncMock(return_value=MagicMock())

        text = await check_phone_number_format(
            value=NANP_E164,
            action=InputTextAction(element_id="elem-phone", text=NANP_E164),
            skyvern_element=element,
            scraped_page=scraped_page,
            task=_TASK,
            step=_STEP,
            field_current_value=field_current_value,
        )

    assert text == expected_text
