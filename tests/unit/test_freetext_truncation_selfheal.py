"""Generic free-text truncation self-heal and fail-closed integrity.

``input_sequentially`` sets ``text[:-TEXT_PRESS_MAX_LENGTH]`` in one atomic fill, then types the last
``TEXT_PRESS_MAX_LENGTH`` characters one at a time. A field that asynchronously resets on the ``input``
event can wipe that atomic leading fill after it lands but before/early in the per-character tail, leaving
the field holding only a short trailing suffix of the intended text. The generic free-text path detects
exactly that signature and re-enters the value with a single atomic fill, while leaving a fully-present
value (even one the field upper/lower-cased) and any autocomplete expansion untouched.

When the single refill does NOT restore the full intended value -- a field that keeps rejecting the value's
format, or a post-refill read-back that cannot be observed -- the heal fails closed: it returns a structured
``ActionFailure(FreeTextInputMismatch)`` so a persistent partial value is never reported as success and never
followed by a batched Submit. Confirmation requires full case-folded equality with the intended value, with
only the browser's deterministic CRLF/lone-CR to LF normalization accepted for textareas; the mere absence of
the loss signature is not success (SKY-13631).

Every value below is synthetic; the strings carry no meaning and exist only to exercise the length,
suffix, and case boundaries of the detector.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.constants import TEXT_PRESS_MAX_LENGTH
from skyvern.exceptions import FreeTextInputMismatch
from skyvern.webeye.actions.handler import (
    _freetext_mismatch_failure,
    _heal_truncated_freetext_input,
    _is_prefix_loss_truncation,
    _static_declared_constraint_evidence,
)
from skyvern.webeye.actions.responses import ActionFailure, ActionResult
from tests.unit.conftest import make_input_element_mock

# The static retention-only check runs after an unconfirmed refill; neutralize it (return None -> UNKNOWN) in
# the fail-closed tests so they exercise the generic privacy-safe fallback with no live-field diagnostic.
_STATIC_PATCH = "skyvern.webeye.actions.handler._static_declared_constraint_evidence"

# A synthetic value comfortably longer than the split boundary; placeholder filler only.
_LONG = "lorem ipsum dolor sit amet consectetur adipiscing"


@pytest.mark.parametrize(
    "intended, rendered",
    [
        (_LONG, _LONG[-TEXT_PRESS_MAX_LENGTH:]),  # the whole typed tail survived (10 chars)
        (_LONG, _LONG[-(TEXT_PRESS_MAX_LENGTH - 1) :]),  # a leading tail char was lost too (9 chars)
        (_LONG, _LONG[-TEXT_PRESS_MAX_LENGTH:].upper()),  # the field upper-cased the surviving tail
        (_LONG, _LONG[-(TEXT_PRESS_MAX_LENGTH - 2) :].upper()),  # 8-char upper-cased tail
    ],
)
def test_prefix_loss_detected_for_trailing_suffix(intended: str, rendered: str) -> None:
    assert _is_prefix_loss_truncation(tag_name="input", intended=intended, rendered=rendered) is True


def test_prefix_loss_tail_bound_uses_raw_length_before_casefold() -> None:
    tail = "abcdefghiß"
    intended = f"leading segment{tail}"
    assert len(tail) == TEXT_PRESS_MAX_LENGTH
    assert len(tail.casefold()) > TEXT_PRESS_MAX_LENGTH
    assert _is_prefix_loss_truncation(tag_name="input", intended=intended, rendered=tail) is True


@pytest.mark.parametrize("tail", ["abc\r\ndefgh", "abcd\refghi"])
def test_prefix_loss_detected_for_browser_normalized_textarea_tail(tail: str) -> None:
    intended = f"leading segment{tail}"
    rendered = tail.replace("\r\n", "\n").replace("\r", "\n")
    assert len(tail) == TEXT_PRESS_MAX_LENGTH
    assert _is_prefix_loss_truncation(tag_name="textarea", intended=intended, rendered=rendered) is True


def test_prefix_loss_does_not_normalize_newlines_for_input() -> None:
    tail = "abc\r\ndefgh"
    intended = f"leading segment{tail}"
    rendered = tail.replace("\r\n", "\n")
    assert _is_prefix_loss_truncation(tag_name="input", intended=intended, rendered=rendered) is False


@pytest.mark.parametrize(
    "intended, rendered",
    [
        # Full value present, even if the field upper-cased it -> not a truncation.
        (_LONG, _LONG.upper()),
        # Exact echo.
        (_LONG, _LONG),
        # Autocomplete expansion (longer than intended) -> must be left alone.
        ("north market", "north market plaza, suite 200, sampleton"),
        # Short, but not a trailing suffix of the intended text.
        (_LONG, "xyz"),
        # Unreadable value -> cannot verify, no heal.
        (_LONG, None),
        # Empty read-back -> not a confident suffix match.
        (_LONG, ""),
    ],
)
def test_no_prefix_loss_for_full_autocomplete_or_unreadable(intended: str, rendered: str | None) -> None:
    assert _is_prefix_loss_truncation(tag_name="input", intended=intended, rendered=rendered) is False


def test_prefix_loss_bounded_by_press_max_length() -> None:
    intended = "zzz" + "abcdefghijk"  # 14 chars; the last 11 form a genuine suffix
    # A suffix no longer than the per-character tail is the fill-loss signature ...
    assert (
        _is_prefix_loss_truncation(tag_name="input", intended=intended, rendered=intended[-TEXT_PRESS_MAX_LENGTH:])
        is True
    )
    # ... a longer surviving suffix is not (the atomic fill can only lose the leading part).
    assert (
        _is_prefix_loss_truncation(
            tag_name="input", intended=intended, rendered=intended[-(TEXT_PRESS_MAX_LENGTH + 1) :]
        )
        is False
    )


def _assert_fails_closed(result: ActionResult | None, *, intended_length: int = len(_LONG)) -> None:
    # A persistent partial value (or unobservable post-refill read-back) must surface the structured failure
    # that the agent's default propagation uses to stop the rest of the batch -- including a queued Submit.
    assert isinstance(result, ActionFailure)
    assert result.success is False
    assert result.exception_type == "FreeTextInputMismatch"
    assert result.stop_execution_on_failure is True
    # Terminal same-batch stop: skip_remaining_actions is set so the duplicate-element-id branch cannot
    # continue into a queued Submit on the same field.
    assert result.skip_remaining_actions is True
    assert str(intended_length) in (result.exception_message or "")


async def _run_heal(
    intended: str, rendered: str | None, tag_name: str = "input", is_secret_value: bool = False
) -> tuple[AsyncMock, ActionResult | None]:
    element = make_input_element_mock(element_id="EL1")
    with (
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value=rendered)),
        patch(_STATIC_PATCH, new=AsyncMock(return_value=None)),
    ):
        result = await _heal_truncated_freetext_input(
            skyvern_element=element,
            tag_name=tag_name,
            text=intended,
            is_secret_value=is_secret_value,
            engine_selection=None,
        )
    return element, result


async def _run_heal_capture(
    intended: str, readbacks: list[str | None], tag_name: str = "input"
) -> tuple[AsyncMock, MagicMock, ActionResult | None]:
    element = make_input_element_mock(element_id="EL1")
    with (
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(side_effect=readbacks)),
        patch(_STATIC_PATCH, new=AsyncMock(return_value=None)),
        patch("skyvern.webeye.actions.handler.LOG") as log,
    ):
        result = await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name=tag_name, text=intended, engine_selection=None
        )
    return element, log, result


@pytest.mark.asyncio
async def test_heal_does_no_live_probe_after_persistent_mismatch() -> None:
    # After a persistent refill mismatch with no declared retention constraint (static UNKNOWN), the heal must
    # fail closed WITHOUT any diagnostic live-field mutation: no clear and no per-character retyping. Only the
    # one atomic refill may touch the field -- a doomed diagnostic probe that clears and re-types the candidate
    # could trigger site input/autocomplete/XHR/auto-submit effects that same-batch Submit blocking cannot
    # contain (SKY-13631, r3755222701).
    element = make_input_element_mock(element_id="EL1")
    tail = _LONG[-TEXT_PRESS_MAX_LENGTH:]
    with (
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value=tail)),
        patch(_STATIC_PATCH, new=AsyncMock(return_value=None)),  # UNKNOWN -> generic fail-closed, no probe
    ):
        result = await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=_LONG, engine_selection=None
        )
    _assert_fails_closed(result)
    element.press_fill.assert_not_called()  # no per-character diagnostic retyping
    element.input_fill.assert_awaited_once_with(text=_LONG)  # only the single atomic refill; no diagnostic clear
    msg = result.exception_message or ""
    assert "unlikely to succeed" in msg  # generic privacy-safe reason
    assert "categories" not in msg and "appended" not in msg and "stall" not in msg


@pytest.mark.asyncio
async def test_heal_reenters_and_confirms_full_value_returns_no_failure() -> None:
    # Pre-fill read-back sees the truncated tail (heal fires); post-fill read-back sees the full value.
    element, log, result = await _run_heal_capture(_LONG, readbacks=[_LONG[-TEXT_PRESS_MAX_LENGTH:], _LONG])
    element.input_fill.assert_awaited_once_with(text=_LONG)
    assert log.info.call_args.kwargs["refill_confirmed"] is True
    assert result is None


@pytest.mark.asyncio
async def test_heal_reenters_raw_ten_character_casefold_expanding_tail() -> None:
    tail = "abcdefghiß"
    intended = f"leading segment{tail}"
    element, log, result = await _run_heal_capture(intended, readbacks=[tail, intended])
    element.input_fill.assert_awaited_once_with(text=intended)
    assert log.info.call_args.kwargs["refill_confirmed"] is True
    assert result is None


@pytest.mark.parametrize("tail", ["abc\r\ndefgh", "abcd\refghi"])
@pytest.mark.asyncio
async def test_heal_detects_and_confirms_browser_normalized_textarea_line_endings(tail: str) -> None:
    intended = f"leading segment{tail}"
    browser_value = intended.replace("\r\n", "\n").replace("\r", "\n")
    browser_tail = tail.replace("\r\n", "\n").replace("\r", "\n")
    element, log, result = await _run_heal_capture(
        intended,
        readbacks=[browser_tail, browser_value],
        tag_name="textarea",
    )
    element.input_fill.assert_awaited_once_with(text=intended)
    assert log.info.call_args.kwargs["refill_confirmed"] is True
    assert result is None


@pytest.mark.asyncio
async def test_heal_input_does_not_normalize_initial_newline_mismatch() -> None:
    tail = "abc\r\ndefgh"
    intended = f"leading segment{tail}"
    element, result = await _run_heal(intended, rendered=tail.replace("\r\n", "\n"), tag_name="input")
    element.input_fill.assert_not_awaited()
    assert result is None


@pytest.mark.asyncio
async def test_heal_input_crlf_lf_mismatch_still_fails_closed() -> None:
    tail = "abcdefghij"
    intended = f"leading segment\r\n{tail}"
    element, log, result = await _run_heal_capture(
        intended,
        readbacks=[tail, intended.replace("\r\n", "\n")],
        tag_name="input",
    )
    element.input_fill.assert_awaited_once_with(text=intended)
    assert log.info.call_args.kwargs["refill_confirmed"] is False
    _assert_fails_closed(result, intended_length=len(intended))


@pytest.mark.asyncio
async def test_heal_is_noop_when_value_fully_present_even_if_case_changed() -> None:
    element, result = await _run_heal(_LONG, rendered=_LONG.upper())
    element.input_fill.assert_not_awaited()
    assert result is None


@pytest.mark.asyncio
async def test_heal_is_noop_for_autocomplete_expansion() -> None:
    element, result = await _run_heal("north market", rendered="north market plaza, suite 200, sampleton")
    element.input_fill.assert_not_awaited()
    assert result is None


@pytest.mark.asyncio
async def test_heal_skips_short_values_without_reading_back() -> None:
    short_value = "a" * TEXT_PRESS_MAX_LENGTH  # not longer than the split boundary: no prefix to lose
    element = make_input_element_mock(element_id="EL1")
    read_back = AsyncMock(return_value="a")
    with patch("skyvern.webeye.actions.handler.get_input_value", new=read_back):
        result = await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=short_value, engine_selection=None
        )
    read_back.assert_not_awaited()
    element.input_fill.assert_not_awaited()
    assert result is None


@pytest.mark.asyncio
async def test_heal_skips_non_freetext_tag() -> None:
    element, result = await _run_heal(_LONG, rendered=_LONG[-TEXT_PRESS_MAX_LENGTH:], tag_name="select")
    element.input_fill.assert_not_awaited()
    assert result is None


@pytest.mark.asyncio
async def test_heal_performs_at_most_one_fill() -> None:
    element, result = await _run_heal(_LONG, rendered=_LONG[-TEXT_PRESS_MAX_LENGTH:].upper())
    assert element.input_fill.await_count == 1


@pytest.mark.asyncio
async def test_heal_fails_closed_for_persistent_prefix_loss_after_refill() -> None:
    # Pre-fill and post-fill read-backs both see only the truncated tail: the field keeps rejecting the value
    # (the whole-value filter incident). The single refill cannot restore it, so the heal must fail closed.
    element, log, result = await _run_heal_capture(
        _LONG, readbacks=[_LONG[-TEXT_PRESS_MAX_LENGTH:], _LONG[-TEXT_PRESS_MAX_LENGTH:]]
    )
    element.input_fill.assert_awaited_once_with(text=_LONG)
    assert log.info.call_args.kwargs["refill_confirmed"] is False
    _assert_fails_closed(result)


@pytest.mark.asyncio
async def test_heal_fails_closed_for_empty_post_refill_value() -> None:
    # The refill did not stick (the field cleared): an empty post-refill read-back is NOT a full-value match,
    # so it must fail closed rather than be treated as "not the loss signature".
    element, log, result = await _run_heal_capture(_LONG, readbacks=[_LONG[-TEXT_PRESS_MAX_LENGTH:], ""])
    element.input_fill.assert_awaited_once_with(text=_LONG)
    assert log.info.call_args.kwargs["refill_confirmed"] is False
    _assert_fails_closed(result)


@pytest.mark.asyncio
async def test_heal_fails_closed_for_unrelated_post_refill_value() -> None:
    # An unrelated post-refill value (long enough to dodge the suffix-loss check) must still fail closed:
    # confirmation is a full case-folded match, not merely the absence of the loss signature.
    element, log, result = await _run_heal_capture(
        _LONG, readbacks=[_LONG[-TEXT_PRESS_MAX_LENGTH:], "something else entirely different"]
    )
    element.input_fill.assert_awaited_once_with(text=_LONG)
    assert log.info.call_args.kwargs["refill_confirmed"] is False
    _assert_fails_closed(result)


@pytest.mark.asyncio
async def test_heal_fails_closed_for_legal_suffix_normalization() -> None:
    # A field that normalizes a suffix (https://form.test -> form.test) trips the detector once; the single
    # bounded refill is re-normalized. Case-insensitive full equality is the ONLY accepted transform, so a
    # scheme-stripped value is not confirmed and the heal fails closed (no site-specific special-casing).
    # "form.test" is <= TEXT_PRESS_MAX_LENGTH so the detector fires; ".test" is an RFC 2606/6761 reserved TLD,
    # keeping this synced-to-public fixture free of any real domain.
    intended = "https://form.test"
    element, log, result = await _run_heal_capture(intended, readbacks=["form.test", "form.test"])
    element.input_fill.assert_awaited_once_with(text=intended)
    assert element.input_fill.await_count == 1
    assert log.info.call_args.kwargs["refill_confirmed"] is False
    _assert_fails_closed(result, intended_length=len(intended))


@pytest.mark.asyncio
async def test_heal_fails_closed_when_post_refill_readback_unobservable() -> None:
    # After the single refill, a post-refill read-back that raises leaves the value unconfirmed. It must not
    # write a second time, and -- because success cannot be observed -- it must fail closed (SKY-13631).
    element = make_input_element_mock(element_id="EL1")
    readbacks = AsyncMock(side_effect=[_LONG[-TEXT_PRESS_MAX_LENGTH:], RuntimeError("detached")])
    with (
        patch("skyvern.webeye.actions.handler.get_input_value", new=readbacks),
        patch("skyvern.webeye.actions.handler.LOG") as log,
    ):
        result = await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=_LONG, engine_selection=None
        )
    assert element.input_fill.await_count == 1
    assert log.info.call_args.kwargs["refill_confirmed"] is False
    _assert_fails_closed(result)


@pytest.mark.asyncio
async def test_heal_skips_secret_value_without_reading_back() -> None:
    # A secret that misses the dedicated verify_secret_input gate (e.g. a long token in a textarea) must not
    # be healed here: no read-back (which would log the exact length) and no rewrite of the unmasked value,
    # and therefore no failure raised from this generic path.
    element = make_input_element_mock(element_id="EL1")
    read_back = AsyncMock(return_value=_LONG[-TEXT_PRESS_MAX_LENGTH:])
    with patch("skyvern.webeye.actions.handler.get_input_value", new=read_back):
        result = await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=_LONG, is_secret_value=True, engine_selection=None
        )
    read_back.assert_not_awaited()
    element.input_fill.assert_not_awaited()
    assert result is None


@pytest.mark.asyncio
async def test_heal_does_not_fail_or_fill_when_prefill_readback_raises() -> None:
    # A pre-refill read-back that raises means truncation was never confirmed: the value could not be observed,
    # so nothing is re-entered AND the action is not failed (we only fail closed once a refill has actually run).
    element = make_input_element_mock(element_id="EL1")
    with patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(side_effect=RuntimeError("detached"))):
        result = await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=_LONG, engine_selection=None
        )
    element.input_fill.assert_not_awaited()
    assert result is None


@pytest.mark.asyncio
async def test_heal_does_not_fail_or_fill_when_prefill_readback_times_out() -> None:
    # The bounded pre-refill read-back caps at BROWSER_ACTION_TIMEOUT_MS; a read that never returns is abandoned
    # (no heal, no fill, no failure) rather than stalling on Playwright's 30s default.
    async def _never_returns(*args: object, **kwargs: object) -> str | None:
        await asyncio.sleep(3600)
        return None

    element = make_input_element_mock(element_id="EL1")
    with (
        patch("skyvern.webeye.actions.handler.get_input_value", new=_never_returns),
        patch("skyvern.webeye.actions.handler.settings.BROWSER_ACTION_TIMEOUT_MS", 10),
    ):
        result = await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=_LONG, engine_selection=None
        )
    element.input_fill.assert_not_awaited()
    assert result is None


def test_freetext_mismatch_message_is_privacy_safe_and_planner_actionable() -> None:
    exc = FreeTextInputMismatch(element_id="EL1", intended_length=len(_LONG))
    message = str(exc)
    # Safe metadata only: element id and a length, plus a planner-actionable hint.
    assert "EL1" in message
    assert str(len(_LONG)) in message
    assert "unlikely to succeed" in message
    # Never leak the raw value, a rendered value, rejected characters, or character-class descriptions.
    for banned in (_LONG, _LONG[-TEXT_PRESS_MAX_LENGTH:], "letters", "spaces", "alphabetic", "character class"):
        assert banned not in message


def test_freetext_mismatch_generic_message_unchanged_without_evidence() -> None:
    exc = FreeTextInputMismatch(element_id="EL1", intended_length=48)
    message = str(exc)
    assert "48" in message
    assert "unlikely to succeed" in message
    assert "categories" not in message


def test_freetext_mismatch_failure_helper_sets_terminal_batch_stop_shape() -> None:
    # The heal seam's failures are built through _freetext_mismatch_failure so they carry exactly the
    # ActionResult shape the agent loop needs to stop the rest of the batch INCLUDING a duplicate element id:
    # success False, stop_execution_on_failure True, AND skip_remaining_actions True.
    result = _freetext_mismatch_failure(FreeTextInputMismatch(element_id="EL1", intended_length=48))
    assert result.success is False
    assert result.stop_execution_on_failure is True
    assert result.skip_remaining_actions is True


def test_freetext_mismatch_failure_helper_shape_with_declared_reason() -> None:
    # A declared-constraint reason must not change the terminal batch-halting shape.
    result = _freetext_mismatch_failure(
        FreeTextInputMismatch(element_id="EL1", intended_length=55, declared_max_length=50)
    )
    assert result.success is False
    assert result.stop_execution_on_failure is True
    assert result.skip_remaining_actions is True


# ---------------------------------------------------------------------------
# Static RETENTION-only fast path (the only diagnosis after a persistent mismatch; there is no live probe).
#
# After a persistent mismatch, only browser-declared constraints that affect value RETENTION are inspected on a
# non-mutating detached clone: a maxlength overflow (browser-reflected clone.maxLength + normalized value
# length) and a number input's value sanitization (valueStuck). A reliable declared violation returns a
# privacy-safe reason; anything it cannot explain -- including the incident field, which declares nothing --
# fails closed with the generic privacy-safe reason. No live-field diagnostic (clear/retype) is ever run.
# ---------------------------------------------------------------------------

_STATIC_EVAL = "skyvern.webeye.actions.handler.SkyvernFrame.evaluate"


def _declared_element(*, pattern=None, maxlength=None, type_attr=None, multiple=None, element_id="EL1"):
    attrs = {"pattern": pattern, "maxlength": maxlength, "type": type_attr, "multiple": multiple}
    el = make_input_element_mock(element_id=element_id, attrs=attrs)
    el.press_fill = AsyncMock()
    return el


async def _static(element, text, eval_result, tag="input"):
    # The browser now returns the reflected IDL type; default it to "text" (the Chromium reflection for a
    # missing/plain/unknown type) unless a test pins a specific reflected type.
    if "typeReflected" not in eval_result:
        eval_result = {**eval_result, "typeReflected": "text"}
    with patch(_STATIC_EVAL, new=AsyncMock(return_value=eval_result)) as ev:
        result = await _static_declared_constraint_evidence(skyvern_element=element, text=text, tag_name=tag)
    return result, ev


@pytest.mark.asyncio
async def test_static_declared_pattern_is_not_retention_evidence_falls_through() -> None:
    # HTML pattern validity does not prevent the value from being RETAINED, so a pattern is neither read nor
    # diagnosed statically -- it falls through to the generic fail-closed fallback (no evaluate; pattern alone is not a gate).
    el = _declared_element(pattern="[A-Za-z ]{0,50}")
    with patch(_STATIC_EVAL, new=AsyncMock(return_value={})) as ev:
        result = await _static_declared_constraint_evidence(
            skyvern_element=el, text="abc7def value here padded long", tag_name="input"
        )
    assert result is None
    ev.assert_not_awaited()  # pattern is not read and does not trigger the detached-clone evaluate
    el.input_fill.assert_not_awaited()
    el.press_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_static_declared_maxlength_overflow_reports_exact_max() -> None:
    el = _declared_element(maxlength="50")
    result, _ = await _static(
        el,
        "a" * 60,
        {"patternMismatch": False, "typeMismatch": False, "valueStuck": True, "utf16Len": 60, "maxLengthReflected": 50},
    )
    assert isinstance(result, FreeTextInputMismatch)
    assert result.declared_max_length == 50
    assert "50" in str(result)
    el.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_static_invalid_email_is_retained_falls_through() -> None:
    # An invalid email/url value is SET and retained by the browser (typeMismatch is validity-only, not
    # retention), so it must fall through to the generic fail-closed fallback -- never a static diagnosis.
    el = _declared_element(type_attr="email")
    result, _ = await _static(
        el,
        "not-an-email-value-here-long",
        {"valueStuck": True, "utf16Len": 28, "typeReflected": "email"},
    )
    assert result is None


@pytest.mark.asyncio
async def test_static_invalid_url_is_retained_falls_through() -> None:
    el = _declared_element(type_attr="url")
    result, _ = await _static(
        el,
        "not a url value padded longer",
        {"valueStuck": True, "utf16Len": 29, "typeReflected": "url"},
    )
    assert result is None


@pytest.mark.asyncio
async def test_static_declared_number_violation_via_sanitization() -> None:
    # A number input sanitizes an invalid value to "" (valueStuck False) rather than setting typeMismatch.
    el = _declared_element(type_attr="number")
    result, _ = await _static(
        el,
        "12a not a number here padded",
        {
            "patternMismatch": False,
            "typeMismatch": False,
            "valueStuck": False,
            "utf16Len": 28,
            "typeReflected": "number",
        },
    )
    assert isinstance(result, FreeTextInputMismatch)
    assert result.declared_constraint == "number"


@pytest.mark.asyncio
async def test_static_declared_satisfied_returns_none() -> None:
    el = _declared_element(pattern="[A-Za-z ]{0,50}", maxlength="50")
    result, _ = await _static(
        el,
        "abcdef ghij",
        {"patternMismatch": False, "typeMismatch": False, "valueStuck": True, "utf16Len": 11, "maxLengthReflected": 50},
    )
    assert result is None  # candidate satisfies declared constraints -> generic fail-closed fallback


@pytest.mark.asyncio
async def test_static_no_declared_attrs_returns_none_without_evaluate() -> None:
    # The exact incident control: no declared attributes -> UNKNOWN without a frame evaluate.
    el = _declared_element()
    with patch(_STATIC_EVAL, new=AsyncMock(return_value={})) as ev:
        result = await _static_declared_constraint_evidence(
            skyvern_element=el, text="abc7 padded value here long", tag_name="input"
        )
    assert result is None
    ev.assert_not_awaited()


@pytest.mark.asyncio
async def test_static_evaluate_exception_returns_none() -> None:
    el = _declared_element(pattern="[A-Za-z ]{0,50}")
    with patch(_STATIC_EVAL, new=AsyncMock(side_effect=RuntimeError("frame detached"))):
        result = await _static_declared_constraint_evidence(
            skyvern_element=el, text="abc7 padded value here long", tag_name="input"
        )
    assert result is None  # evaluate failure -> generic fail-closed fallback


@pytest.mark.asyncio
async def test_static_email_falls_through_and_multiple_is_not_read() -> None:
    # Email validity is not retention evidence, so an email input falls through to the generic fail-closed fallback. `multiple`
    # is no longer read or copied onto the clone (it only affects validity, not retention).
    el = _declared_element(type_attr="email", multiple="")
    result, _ = await _static(
        el,
        "a@example.com, b@example.com",
        {"valueStuck": True, "utf16Len": 27, "typeReflected": "email"},
    )
    assert result is None  # email is not a retention constraint -> generic fail-closed fallback
    assert not any(c.args[0] == "multiple" for c in el.get_attr.call_args_list)  # `multiple` never read


@pytest.mark.asyncio
async def test_static_number_maxlength_not_applied() -> None:
    # M3: maxlength must never be enforced for type=number. A valid number longer than maxlength must not be
    # statically failed on maxlength.
    el = _declared_element(type_attr="number", maxlength="2")
    result, _ = await _static(
        el,
        "123",
        {"patternMismatch": False, "typeMismatch": False, "valueStuck": True, "utf16Len": 3, "typeReflected": "number"},
    )
    assert result is None  # valid number, maxlength inapplicable to number -> no false diagnosis


@pytest.mark.asyncio
async def test_static_passes_raw_type_as_evaluate_arg_not_normalized() -> None:
    # The raw type must reach the browser verbatim (as an arg), never Python-normalized, so the detached clone
    # reflects it. A stray-whitespace/mixed-case value must appear in the arg list, not in the JS source.
    el = _declared_element(type_attr=" NuMbEr ", maxlength="10")
    _, ev = await _static(
        el,
        "a" * 20,
        {
            "patternMismatch": False,
            "typeMismatch": False,
            "valueStuck": True,
            "utf16Len": 20,
            "maxLengthReflected": 10,
            "typeReflected": "text",
        },
    )
    ev.assert_awaited_once()
    assert ev.await_args.kwargs["arg"][0] == " NuMbEr "  # raw, un-normalized (type is the first arg)
    assert " NuMbEr " not in ev.await_args.kwargs["expression"]  # never interpolated into the JS


@pytest.mark.asyncio
async def test_static_whitespace_type_reflects_text_maxlength_applies_not_number() -> None:
    # Reviewer case: type=" number " (stray whitespace) is an invalid keyword, so Chromium reflects "text".
    # maxlength therefore APPLIES (text input) and the field is NOT a number constraint. The old Python
    # strip()+lower() wrongly made it a number input (maxlength inapplicable + a false number constraint).
    el = _declared_element(type_attr=" number ", maxlength="10")
    result, _ = await _static(
        el,
        "a" * 20,
        {
            "patternMismatch": False,
            "typeMismatch": False,
            "valueStuck": True,
            "utf16Len": 20,
            "maxLengthReflected": 10,
            "typeReflected": "text",
        },
    )
    assert isinstance(result, FreeTextInputMismatch)
    assert result.declared_max_length == 10  # maxlength applies to the reflected text input
    assert result.declared_constraint is None  # not treated as a number constraint


@pytest.mark.asyncio
async def test_static_uppercase_type_reflected_number_applies_number_constraint() -> None:
    # A valid but upper-cased keyword reflects the canonical lower-case type; maxlength stays inapplicable.
    el = _declared_element(type_attr="NUMBER", maxlength="2")
    result, _ = await _static(
        el,
        "12a bad number padded here x",
        {
            "patternMismatch": False,
            "typeMismatch": False,
            "valueStuck": False,
            "utf16Len": 28,
            "maxLengthReflected": 2,
            "typeReflected": "number",
        },
    )
    assert isinstance(result, FreeTextInputMismatch)
    assert result.declared_constraint == "number"  # reflected number -> number constraint
    assert result.declared_max_length is None  # maxlength inapplicable to a number input


@pytest.mark.asyncio
async def test_static_reflected_type_missing_falls_through() -> None:
    # Defensive: an evaluate result lacking a string typeReflected must fall through to the generic fail-closed fallback.
    el = _declared_element(pattern="[A-Za-z ]{0,50}")
    with patch(_STATIC_EVAL, new=AsyncMock(return_value={"patternMismatch": True})):
        result = await _static_declared_constraint_evidence(
            skyvern_element=el, text="abc7 padded value here long", tag_name="input"
        )
    assert result is None


@pytest.mark.asyncio
async def test_static_textarea_pattern_and_type_are_inert() -> None:
    # M3: pattern and input-type grammar must not be enforced for <textarea> even if such attrs exist.
    el = _declared_element(pattern="[A-Za-z ]{0,50}", type_attr="email")
    result, ev = await _static(
        el,
        "abc7 not-an-email padded here",
        {"patternMismatch": True, "typeMismatch": True, "valueStuck": True, "utf16Len": 29},
        tag="textarea",
    )
    assert result is None  # inert attrs on textarea -> generic fail-closed fallback
    ev.assert_not_awaited()  # no applicable declared constraint -> no evaluate


@pytest.mark.asyncio
async def test_static_textarea_maxlength_overflow_still_works() -> None:
    # M3: maxlength DOES apply to textarea.
    el = _declared_element(maxlength="50")
    result, _ = await _static(
        el,
        "a" * 60,
        {"patternMismatch": False, "typeMismatch": False, "valueStuck": True, "utf16Len": 60, "maxLengthReflected": 50},
        tag="textarea",
    )
    assert isinstance(result, FreeTextInputMismatch)
    assert result.declared_max_length == 50


@pytest.mark.asyncio
async def test_static_passes_is_textarea_flag_as_evaluate_arg() -> None:
    # The tag drives a tag-faithful clone in the browser, so isTextarea must be passed as an arg (True for a
    # textarea, False for an input) rather than the clone being hard-coded to <input>.
    ta = _declared_element(maxlength="50")
    _, ev_ta = await _static(
        ta, "a" * 60, {"valueStuck": True, "utf16Len": 60, "maxLengthReflected": 50}, tag="textarea"
    )
    assert ev_ta.await_args.kwargs["arg"][2] is True

    inp = _declared_element(maxlength="50")
    _, ev_in = await _static(inp, "a" * 60, {"valueStuck": True, "utf16Len": 60, "maxLengthReflected": 50}, tag="input")
    assert ev_in.await_args.kwargs["arg"][2] is False


@pytest.mark.asyncio
async def test_static_textarea_crlf_normalized_within_maxlength_no_failure() -> None:
    # A textarea normalizes CRLF/lone-CR to a single LF before counting maxlength: raw "aaa\r\nb" is 6 raw
    # units but the browser value "aaa\nb" is 5, so a maxlength=5 textarea must NOT be flagged. The static
    # helper uses the browser-normalized clone.value.length (returned as utf16Len), not the raw string length.
    el = _declared_element(maxlength="5")
    result, _ = await _static(
        el,
        "aaa\r\nb",
        {"valueStuck": False, "utf16Len": 5, "maxLengthReflected": 5, "typeReflected": "textarea"},
        tag="textarea",
    )
    assert result is None  # 5 normalized units == maxlength 5 -> not over -> generic fail-closed fallback


@pytest.mark.asyncio
async def test_static_textarea_normalized_over_maxlength_reports_max() -> None:
    # A textarea whose browser-normalized length exceeds maxlength must still report the exact declared max.
    el = _declared_element(maxlength="5")
    result, _ = await _static(
        el,
        "aaa\r\nbb",
        {"valueStuck": False, "utf16Len": 6, "maxLengthReflected": 5, "typeReflected": "textarea"},
        tag="textarea",
    )
    assert isinstance(result, FreeTextInputMismatch)
    assert result.declared_max_length == 5


@pytest.mark.asyncio
async def test_static_unknown_input_type_keeps_text_semantics_for_maxlength() -> None:
    # An unknown input type reflects "text", so maxlength still applies (it is a retention constraint).
    el = _declared_element(type_attr="foobar", maxlength="10")
    result, _ = await _static(
        el,
        "a" * 20,
        {"valueStuck": True, "utf16Len": 20, "maxLengthReflected": 10, "typeReflected": "text"},
    )
    assert isinstance(result, FreeTextInputMismatch)
    assert result.declared_max_length == 10


class _HangingAttrElement(MagicMock):
    async def get_attr(self, name, mode="auto", timeout=None):
        await asyncio.sleep(5)  # longer than the whole-helper budget
        return None

    def get_frame(self):
        return MagicMock()


@pytest.mark.asyncio
async def test_static_whole_helper_timeout_returns_none() -> None:
    # M1: a hung attribute read (or evaluate) must be cancelled by the helper's own whole-run budget and
    # fall through to the generic fail-closed fallback, not hang. The failsafe wait_for would fail (not hang) the test.
    el = _HangingAttrElement()
    el.get_id.return_value = "EL1"
    with patch("skyvern.webeye.actions.handler._STATIC_PROBE_TIMEOUT_S", 0.05):
        result = await asyncio.wait_for(
            _static_declared_constraint_evidence(skyvern_element=el, text="a" * 30, tag_name="input"),
            timeout=3.0,
        )
    assert result is None


def test_static_declared_messages_are_privacy_safe() -> None:
    candidate = "abcdefghij 1234 secret-value"
    for exc in (
        FreeTextInputMismatch(element_id="EL1", intended_length=len(candidate), declared_max_length=50),
        FreeTextInputMismatch(element_id="EL1", intended_length=len(candidate), declared_constraint="pattern"),
        FreeTextInputMismatch(element_id="EL1", intended_length=len(candidate), declared_constraint="email"),
    ):
        m = str(exc).casefold()
        assert candidate.casefold() not in m
        cf = candidate.casefold()
        for i in range(len(cf) - 5):
            assert cf[i : i + 6] not in m
        assert "[a-za-z" not in m  # never the raw regex


def test_static_declared_number_message_names_number_retention() -> None:
    exc = FreeTextInputMismatch(element_id="EL1", intended_length=30, declared_constraint="number")
    m = str(exc)
    assert "number input" in m and "does not retain a non-numeric value" in m
    assert "Propose a valid number." in m


def test_static_declared_maxlength_message_states_utf16_code_units() -> None:
    # HTML maxlength counts UTF-16 code units, so the declared-max message must say so, not "characters".
    exc = FreeTextInputMismatch(element_id="EL1", intended_length=60, declared_max_length=50)
    m = str(exc)
    assert "maximum length of 50 UTF-16 code units" in m
    assert "within 50 UTF-16 code units" in m
    assert "50 characters" not in m


@pytest.mark.asyncio
async def test_static_reads_only_maxlength_and_type_dynamically() -> None:
    # Retention-only: the helper reads just maxlength and type LIVE (mode="dynamic"); pattern and multiple are
    # neither read nor evaluated, since they do not affect value retention.
    el = _declared_element(maxlength="50", type_attr="number")
    await _static(
        el,
        "abc7 padded value here long",
        {"valueStuck": True, "utf16Len": 27, "maxLengthReflected": 50, "typeReflected": "number"},
    )
    read_names = {c.args[0] for c in el.get_attr.call_args_list}
    assert {"maxlength", "type"} <= read_names
    assert "pattern" not in read_names and "multiple" not in read_names
    assert all(c.kwargs.get("mode") == "dynamic" for c in el.get_attr.call_args_list)


class _StaleVsLiveField:
    """get_attr returns a STALE scraped value by default but the true LIVE value under mode='dynamic'. Proves
    the static check must read live: the scrape says a pattern exists, the live element declares none."""

    def __init__(self, scraped: dict, live: dict, element_id: str = "EL1") -> None:
        self._scraped = scraped
        self._live = live
        self._id = element_id

    def get_id(self) -> str:
        return self._id

    def get_frame(self) -> MagicMock:
        return MagicMock()

    async def get_attr(self, name, mode="auto", timeout=None):
        return self._live.get(name) if mode == "dynamic" else self._scraped.get(name)


@pytest.mark.asyncio
async def test_static_stale_cache_does_not_create_false_explanation() -> None:
    # Scrape cached a pattern; the live element declares nothing. Reading live must yield UNKNOWN (None) with
    # no frame evaluate, not a false pattern explanation from the stale cache.
    el = _StaleVsLiveField(scraped={"pattern": "[A-Za-z ]{0,50}"}, live={})
    with patch(_STATIC_EVAL, new=AsyncMock(return_value={"patternMismatch": True})) as ev:
        result = await _static_declared_constraint_evidence(
            skyvern_element=el, text="abc7 padded value here long", tag_name="input"
        )
    assert result is None
    ev.assert_not_awaited()


@pytest.mark.asyncio
async def test_static_dynamic_read_error_falls_through() -> None:
    # A live (dynamic) attribute read that raises must fall through to the generic fail-closed fallback (None), not explode.
    el = _declared_element(pattern="[A-Za-z ]{0,50}")
    el.get_attr = AsyncMock(side_effect=RuntimeError("stale locator detached"))
    with patch(_STATIC_EVAL, new=AsyncMock(return_value={"patternMismatch": True})):
        result = await _static_declared_constraint_evidence(
            skyvern_element=el, text="abc7 padded value here long", tag_name="input"
        )
    assert result is None


@pytest.mark.asyncio
async def test_static_declared_maxlength_supplementary_unicode_says_utf16() -> None:
    # 6 supplementary code points = 12 UTF-16 code units; an HTML maxlength=10 field overflows on UTF-16 units
    # even though it is only 6 Python characters. The static comparison uses the browser utf16Len, and the
    # message must say UTF-16 code units (not "10 characters") while never leaking the raw candidate.
    candidate = "\U0001f600" * 6  # 6 emoji code points, 12 UTF-16 code units
    el = _declared_element(maxlength="10")
    result, _ = await _static(
        el,
        candidate,
        {"patternMismatch": False, "typeMismatch": False, "valueStuck": True, "utf16Len": 12, "maxLengthReflected": 10},
    )
    assert isinstance(result, FreeTextInputMismatch)
    assert result.declared_max_length == 10
    m = str(result)
    assert "maximum length of 10 UTF-16 code units" in m
    assert "10 characters" not in m
    assert candidate not in m and "\U0001f600" not in m  # privacy: no raw candidate


@pytest.mark.asyncio
async def test_static_declared_maxlength_uses_browser_reflected_value_not_python_int() -> None:
    # `1_0` is a malformed HTML maxlength that Python int() reads as 10 but Chromium's clone.maxLength reflects
    # (and the field enforces) as 1. The static check must trust the browser-reflected value: a 5 UTF-16 unit
    # candidate overflows the real limit of 1, so it must fail with declared_max_length == 1, not fall through
    # as it would if Python int("1_0") == 10 were used.
    el = _declared_element(maxlength="1_0")
    result, _ = await _static(
        el,
        "abcde",
        {"patternMismatch": False, "typeMismatch": False, "valueStuck": True, "utf16Len": 5, "maxLengthReflected": 1},
    )
    assert isinstance(result, FreeTextInputMismatch)
    assert result.declared_max_length == 1


@pytest.mark.asyncio
async def test_static_declared_maxlength_reflected_value_is_used_verbatim_not_raw_string() -> None:
    # A leading-zero maxlength Chromium accepts (reflects 10); the value placed in FreeTextInputMismatch must be
    # the browser-parsed integer 10, never the raw "010" string.
    el = _declared_element(maxlength="010")
    result, _ = await _static(
        el,
        "a" * 15,
        {"patternMismatch": False, "typeMismatch": False, "valueStuck": True, "utf16Len": 15, "maxLengthReflected": 10},
    )
    assert isinstance(result, FreeTextInputMismatch)
    assert result.declared_max_length == 10
    m = str(result)
    assert "maximum length of 10 UTF-16 code units" in m
    assert "010" not in m


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["-5", "٣", "  ", ""])
async def test_static_declared_maxlength_browser_rejected_falls_through(raw: str) -> None:
    # Values Chromium rejects as a maximum reflect clone.maxLength == -1 (the field enforces no limit). The
    # static check must never invent a constraint from them; it falls through to the generic fail-closed fallback.
    el = _declared_element(maxlength=raw)
    result, _ = await _static(
        el,
        "a" * 30,
        {"patternMismatch": False, "typeMismatch": False, "valueStuck": True, "utf16Len": 30, "maxLengthReflected": -1},
    )
    assert result is None


@pytest.mark.asyncio
async def test_static_passes_raw_maxlength_as_evaluate_arg_never_interpolated() -> None:
    # The raw maxlength string must reach the browser only as an evaluate argument (so the browser parses it),
    # never spliced into the JS source. A malformed value must appear in the arg list, not the expression.
    el = _declared_element(maxlength="1_0")
    _, ev = await _static(
        el,
        "a" * 20,
        {"patternMismatch": False, "typeMismatch": False, "valueStuck": True, "utf16Len": 20, "maxLengthReflected": 1},
    )
    ev.assert_awaited_once()
    kwargs = ev.await_args.kwargs
    assert "1_0" in kwargs["arg"]  # raw maxlength passed as an argument
    assert "1_0" not in kwargs["expression"]  # never interpolated into the JS source


@pytest.mark.asyncio
async def test_static_declared_maxlength_reflected_missing_falls_through() -> None:
    # A defensive guard: if the browser result omits maxLengthReflected (or returns a non-numeric), the static
    # check must not invent a maximum from the raw attribute string.
    el = _declared_element(maxlength="10")
    result, _ = await _static(
        el, "a" * 30, {"patternMismatch": False, "typeMismatch": False, "valueStuck": True, "utf16Len": 30}
    )
    assert result is None


@pytest.mark.asyncio
async def test_heal_static_declared_reason_halts_batch_without_live_probe() -> None:
    element = make_input_element_mock(element_id="EL1")
    tail = _LONG[-TEXT_PRESS_MAX_LENGTH:]
    static_failure = FreeTextInputMismatch(element_id="EL1", intended_length=len(_LONG), declared_max_length=50)
    with (
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(side_effect=[tail, tail])),
        patch(_STATIC_PATCH, new=AsyncMock(return_value=static_failure)) as static,
    ):
        result = await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=_LONG, engine_selection=None
        )
    static.assert_awaited_once()
    element.press_fill.assert_not_called()  # no live-field diagnostic
    assert isinstance(result, ActionFailure)
    assert result.exception_type == "FreeTextInputMismatch"
    assert result.stop_execution_on_failure is True
    assert "maximum length of 50 UTF-16 code units" in (result.exception_message or "")


@pytest.mark.asyncio
async def test_heal_generic_fail_closed_when_static_unknown_no_live_probe() -> None:
    element = make_input_element_mock(element_id="EL1")
    tail = _LONG[-TEXT_PRESS_MAX_LENGTH:]
    with (
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(side_effect=[tail, tail])),
        patch(_STATIC_PATCH, new=AsyncMock(return_value=None)) as static,
    ):
        result = await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=_LONG, engine_selection=None
        )
    static.assert_awaited_once()  # UNKNOWN -> generic fail-closed, NO live diagnostic probe
    element.press_fill.assert_not_called()
    element.input_fill.assert_awaited_once_with(text=_LONG)  # only the atomic refill; no diagnostic clear
    assert isinstance(result, ActionFailure)
    assert "unlikely to succeed" in (result.exception_message or "")
    assert "categories" not in (result.exception_message or "")


@pytest.mark.asyncio
async def test_heal_does_not_run_static_when_post_refill_unobservable() -> None:
    element = make_input_element_mock(element_id="EL1")
    tail = _LONG[-TEXT_PRESS_MAX_LENGTH:]
    with (
        patch(
            "skyvern.webeye.actions.handler.get_input_value",
            new=AsyncMock(side_effect=[tail, RuntimeError("detached")]),
        ),
        patch(
            _STATIC_PATCH,
            new=AsyncMock(
                return_value=FreeTextInputMismatch(element_id="EL1", intended_length=len(_LONG), declared_max_length=50)
            ),
        ) as static,
    ):
        result = await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=_LONG, engine_selection=None
        )
    static.assert_not_awaited()  # unobservable post-refill -> static skipped -> generic fail-closed
    element.press_fill.assert_not_called()
    assert isinstance(result, ActionFailure)
    assert "unlikely to succeed" in (result.exception_message or "")
