from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._errors import Error as PlaywrightError

from skyvern.exceptions import InputTextCommitMismatch, PhoneNumberInputMismatch
from skyvern.webeye.actions.handler import (
    _fill_nanp_tel_with_readback,
    _log_tel_fallback_fill_digit_counts,
    _verify_generic_input_commit,
    input_text_values_match,
    verify_phone_input_digits,
)
from skyvern.webeye.actions.responses import ActionFailure

NANP_DIGITS = "2245550199"
NANP_E164 = f"+1{NANP_DIGITS}"


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


@pytest.mark.parametrize(
    ("expected", "actual", "input_type"),
    [
        ("5551234567", "(555) 123-4567", "text"),
        ("12345", "12345-____", "text"),
        ("ab12 cd", "AB12-CD", "search"),
        ("postal code", "POSTAL_CODE", "textarea"),
    ],
)
def test_input_text_values_match_ignores_plain_text_mask_formatting(
    expected: str, actual: str, input_type: str
) -> None:
    assert input_text_values_match(expected, actual, input_type=input_type)


@pytest.mark.parametrize(
    ("expected", "actual", "input_type"),
    [
        ("user@example.test", "user@exampletest", "email"),
        ("https://example.test/path", "https://exampletest/path", "url"),
        ("-12.5", "125", "number"),
    ],
)
def test_input_text_values_match_preserves_semantic_punctuation(expected: str, actual: str, input_type: str) -> None:
    assert not input_text_values_match(expected, actual, input_type=input_type)


def test_input_text_values_match_trims_semantic_input_whitespace() -> None:
    assert input_text_values_match(" user@example.test ", "user@example.test", input_type="email")


@pytest.mark.parametrize(
    ("expected", "actual", "committed", "why"),
    [
        # A field has committed when its value derives from what was typed.
        ("12345", "12345", True, "exact"),
        ("1234567890", "(123) 456-7890", True, "reformat only rearranges separators"),
        ("12345", "12345-6789", True, "widget enriches the typed value"),
        ("foo_bar", "foo_bar", True, "placeholder char inside a fully typed value"),
        # Only these are failures.
        ("12345", "", False, "empty"),
        ("12345", "_____", False, "mask kept none of the keystrokes"),
        ("12345", "123__", False, "mask kept only some of them"),
        ("12345", "99999", False, "genuinely different value"),
        ("12345", "999123459", False, "contains the typed value but does not derive from it"),
        ("---", "___", True, "separators typed, separators back: a reformat of nothing"),
        ("---", "xyz", False, "separators typed, a real value back: not a reformat of it"),
    ],
)
def test_input_text_values_match_treats_derived_values_as_committed(
    expected: str, actual: str, committed: bool, why: str
) -> None:
    assert input_text_values_match(expected, actual, input_type="text") is committed, why


def test_input_text_values_match_rejects_empty_commit() -> None:
    assert not input_text_values_match("-", "", input_type="text")


def _make_commit_element(readbacks: list[str]) -> MagicMock:
    locator = MagicMock()
    locator.input_value = AsyncMock(side_effect=readbacks)
    locator.evaluate = AsyncMock()
    element = _make_element(locator)
    element.input_clear = AsyncMock()
    element.input_sequentially = AsyncMock()
    element.input_fill = AsyncMock()
    return element


def _make_commit_frame() -> MagicMock:
    frame = MagicMock()
    frame.safe_wait_for_animation_end = AsyncMock()
    return frame


@pytest.mark.asyncio
async def test_generic_commit_verification_accepts_initial_readback() -> None:
    element = _make_commit_element(["committed"])
    dom = MagicMock()

    result = await _verify_generic_input_commit(
        skyvern_element=element,
        skyvern_frame=_make_commit_frame(),
        dom=dom,
        tag_name="input",
        input_type="text",
        expected_value="committed",
    )

    assert result is None
    element.input_clear.assert_not_awaited()
    element.input_fill.assert_not_awaited()
    element.get_locator().evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_commit_verification_settles_before_destroying_a_slow_value() -> None:
    # A masked or debounced field can land its value a beat after the keystrokes settle. Escalating on
    # the first read-back would clear and retype a field that was about to commit correctly.
    element = _make_commit_element(["", "committed"])
    frame = _make_commit_frame()
    dom = MagicMock()

    result = await _verify_generic_input_commit(
        skyvern_element=element,
        skyvern_frame=frame,
        dom=dom,
        tag_name="input",
        input_type="text",
        expected_value="committed",
    )

    assert result is None
    frame.safe_wait_for_animation_end.assert_awaited_once()
    element.input_clear.assert_not_awaited()
    element.input_sequentially.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_commit_verification_escalates_to_fill() -> None:
    element = _make_commit_element(["", "", "", "committed"])
    dom = MagicMock()

    result = await _verify_generic_input_commit(
        skyvern_element=element,
        skyvern_frame=_make_commit_frame(),
        dom=dom,
        tag_name="input",
        input_type="text",
        expected_value="committed",
    )

    assert result is None
    element.input_clear.assert_awaited_once()
    element.input_sequentially.assert_awaited_once_with(text="committed")
    element.input_fill.assert_awaited_once_with(text="committed")
    element.get_locator().evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_commit_verification_escalates_to_native_setter() -> None:
    element = _make_commit_element(["", "", "", "", "committed"])
    dom = MagicMock()

    result = await _verify_generic_input_commit(
        skyvern_element=element,
        skyvern_frame=_make_commit_frame(),
        dom=dom,
        tag_name="textarea",
        input_type="textarea",
        expected_value="committed",
    )

    assert result is None
    element.input_fill.assert_awaited_once_with(text="committed")
    element.get_locator().evaluate.assert_awaited_once()
    assert element.get_locator().evaluate.await_args.args[1] == "committed"


@pytest.mark.asyncio
async def test_generic_commit_verification_returns_typed_failure_without_value() -> None:
    element = _make_commit_element(["", "", "", "", ""])
    dom = MagicMock()

    result = await _verify_generic_input_commit(
        skyvern_element=element,
        skyvern_frame=_make_commit_frame(),
        dom=dom,
        tag_name="input",
        input_type="text",
        expected_value="sensitive-value",
    )

    assert isinstance(result, ActionFailure)
    assert result.exception_type == InputTextCommitMismatch.__name__
    assert "expected length 14" in (result.exception_message or "")
    assert "actual length 0" in (result.exception_message or "")
    assert "sensitive-value" not in (result.exception_message or "")


@pytest.mark.asyncio
async def test_generic_commit_verification_skips_navigation_during_escalation() -> None:
    element = _make_commit_element(["", ""])
    dom = MagicMock()
    element.input_clear.side_effect = PlaywrightError("Execution context was destroyed during navigation")

    result = await _verify_generic_input_commit(
        skyvern_element=element,
        skyvern_frame=_make_commit_frame(),
        dom=dom,
        tag_name="input",
        input_type="text",
        expected_value="committed",
    )

    assert result is None


@pytest.mark.asyncio
async def test_generic_commit_verification_reresolves_detached_element() -> None:
    detached_element = _make_commit_element([])
    detached_element.get_locator().input_value.side_effect = PlaywrightError("Element is not attached to the DOM")
    replacement_element = _make_commit_element(["committed"])
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(return_value=replacement_element)

    result = await _verify_generic_input_commit(
        skyvern_element=detached_element,
        skyvern_frame=_make_commit_frame(),
        dom=dom,
        tag_name="input",
        input_type="text",
        expected_value="committed",
    )

    assert result is None
    dom.get_skyvern_element_by_id.assert_awaited_once_with(detached_element.get_id())


@pytest.mark.asyncio
async def test_generic_commit_verification_accepts_plausible_autocomplete_rewrite() -> None:
    element = _make_commit_element(["committed option"])

    result = await _verify_generic_input_commit(
        skyvern_element=element,
        skyvern_frame=_make_commit_frame(),
        dom=MagicMock(),
        tag_name="input",
        input_type="text",
        expected_value="committed",
        allow_autocomplete_rewrite=True,
    )

    assert result is None
    element.input_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_commit_verification_rejects_truncated_value_as_autocomplete_rewrite() -> None:
    element = _make_commit_element(["123", "123", "123", "123", "123"])

    result = await _verify_generic_input_commit(
        skyvern_element=element,
        skyvern_frame=_make_commit_frame(),
        dom=MagicMock(),
        tag_name="input",
        input_type="text",
        expected_value="12345",
        allow_autocomplete_rewrite=True,
    )

    assert isinstance(result, ActionFailure)
    assert result.exception_type == InputTextCommitMismatch.__name__
    element.input_clear.assert_awaited_once()
    element.input_fill.assert_awaited_once_with(text="12345")


@pytest.mark.asyncio
async def test_generic_commit_verification_escalates_garbled_readback_without_rewrite_allowance() -> None:
    element = _make_commit_element(["123", "123", "12345"])

    result = await _verify_generic_input_commit(
        skyvern_element=element,
        skyvern_frame=_make_commit_frame(),
        dom=MagicMock(),
        tag_name="input",
        input_type="text",
        expected_value="12345",
    )

    assert result is None
    element.input_clear.assert_awaited_once()
    element.input_sequentially.assert_awaited_once_with(text="12345")


@pytest.mark.asyncio
async def test_generic_commit_verification_reresolves_once_per_escalation_stage() -> None:
    first_element = _make_commit_element([])
    first_element.get_locator().input_value.side_effect = PlaywrightError("Element is not attached to the DOM")
    second_element = _make_commit_element([])
    second_element.get_locator().input_value.side_effect = [
        "",
        "",
        PlaywrightError("Element is not attached to the DOM"),
    ]
    third_element = _make_commit_element(["committed"])
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(side_effect=[second_element, third_element])

    result = await _verify_generic_input_commit(
        skyvern_element=first_element,
        skyvern_frame=_make_commit_frame(),
        dom=dom,
        tag_name="input",
        input_type="text",
        expected_value="committed",
    )

    assert result is None
    assert dom.get_skyvern_element_by_id.await_count == 2
    second_element.input_sequentially.assert_awaited_once_with(text="committed")


@pytest.mark.asyncio
async def test_generic_commit_verification_soft_skips_detach_during_fill_stage() -> None:
    detached_element = _make_commit_element([])
    detached_element.get_locator().input_value.side_effect = PlaywrightError("Element is not attached to the DOM")
    replacement_element = _make_commit_element(["", ""])
    replacement_element.input_clear.side_effect = PlaywrightError("Element is not attached to the DOM")
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(return_value=replacement_element)

    result = await _verify_generic_input_commit(
        skyvern_element=detached_element,
        skyvern_frame=_make_commit_frame(),
        dom=dom,
        tag_name="input",
        input_type="text",
        expected_value="committed",
    )

    assert result is None
