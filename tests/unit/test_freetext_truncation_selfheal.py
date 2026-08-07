"""Generic free-text truncation self-heal.

``input_sequentially`` sets ``text[:-TEXT_PRESS_MAX_LENGTH]`` in one atomic fill, then types the last
``TEXT_PRESS_MAX_LENGTH`` characters one at a time. A field that asynchronously resets on the ``input``
event can wipe that atomic leading fill after it lands but before/early in the per-character tail, leaving
the field holding only a short trailing suffix of the intended text. The generic free-text path detects
exactly that signature and re-enters the value with a single atomic fill, while leaving a fully-present
value (even one the field upper/lower-cased) and any autocomplete expansion untouched.

Every value below is synthetic; the strings carry no meaning and exist only to exercise the length,
suffix, and case boundaries of the detector.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.constants import TEXT_PRESS_MAX_LENGTH
from skyvern.webeye.actions.handler import _heal_truncated_freetext_input, _is_prefix_loss_truncation
from tests.unit.conftest import make_input_element_mock

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
    assert _is_prefix_loss_truncation(intended=intended, rendered=rendered) is True


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
    assert _is_prefix_loss_truncation(intended=intended, rendered=rendered) is False


def test_prefix_loss_bounded_by_press_max_length() -> None:
    intended = "zzz" + "abcdefghijk"  # 14 chars; the last 11 form a genuine suffix
    # A suffix no longer than the per-character tail is the fill-loss signature ...
    assert _is_prefix_loss_truncation(intended=intended, rendered=intended[-TEXT_PRESS_MAX_LENGTH:]) is True
    # ... a longer surviving suffix is not (the atomic fill can only lose the leading part).
    assert _is_prefix_loss_truncation(intended=intended, rendered=intended[-(TEXT_PRESS_MAX_LENGTH + 1) :]) is False


async def _run_heal(intended: str, rendered: str | None, tag_name: str = "input") -> AsyncMock:
    element = make_input_element_mock(element_id="EL1")
    with patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value=rendered)):
        await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name=tag_name, text=intended, engine_selection=None
        )
    return element


@pytest.mark.asyncio
async def test_heal_reenters_full_value_on_truncation() -> None:
    element = await _run_heal(_LONG, rendered=_LONG[-TEXT_PRESS_MAX_LENGTH:])
    element.input_fill.assert_awaited_once_with(text=_LONG)


@pytest.mark.asyncio
async def test_heal_is_noop_when_value_fully_present_even_if_case_changed() -> None:
    element = await _run_heal(_LONG, rendered=_LONG.upper())
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_heal_is_noop_for_autocomplete_expansion() -> None:
    element = await _run_heal("north market", rendered="north market plaza, suite 200, sampleton")
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_heal_skips_short_values_without_reading_back() -> None:
    short_value = "a" * TEXT_PRESS_MAX_LENGTH  # not longer than the split boundary: no prefix to lose
    element = make_input_element_mock(element_id="EL1")
    read_back = AsyncMock(return_value="a")
    with patch("skyvern.webeye.actions.handler.get_input_value", new=read_back):
        await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=short_value, engine_selection=None
        )
    read_back.assert_not_awaited()
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_heal_skips_non_freetext_tag() -> None:
    element = await _run_heal(_LONG, rendered=_LONG[-TEXT_PRESS_MAX_LENGTH:], tag_name="select")
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_heal_performs_at_most_one_fill() -> None:
    element = await _run_heal(_LONG, rendered=_LONG[-TEXT_PRESS_MAX_LENGTH:].upper())
    assert element.input_fill.await_count == 1


async def _run_heal_capture(
    intended: str, readbacks: list[str | None], tag_name: str = "input"
) -> tuple[AsyncMock, MagicMock]:
    element = make_input_element_mock(element_id="EL1")
    with (
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(side_effect=readbacks)),
        patch("skyvern.webeye.actions.handler.LOG") as log,
    ):
        await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name=tag_name, text=intended, engine_selection=None
        )
    return element, log


@pytest.mark.asyncio
async def test_post_refill_readback_logs_confirmed_when_fill_sticks() -> None:
    # Pre-fill read-back sees the truncated tail (heal fires); post-fill read-back sees the full value.
    element, log = await _run_heal_capture(_LONG, readbacks=[_LONG[-TEXT_PRESS_MAX_LENGTH:], _LONG])
    element.input_fill.assert_awaited_once_with(text=_LONG)
    assert log.info.call_args.kwargs["refill_confirmed"] is True


@pytest.mark.asyncio
async def test_post_refill_readback_non_confirmed_for_legal_suffix_normalization() -> None:
    # A field that legally normalizes a suffix (https://form.test -> form.test) trips the detector once; the
    # single bounded refill is re-normalized, so the outcome is recorded as NON-confirmed and no second fill
    # runs. "form.test" is <= TEXT_PRESS_MAX_LENGTH so the detector fires; ".test" is an RFC 2606/6761
    # reserved TLD, keeping this synced-to-public fixture free of any real domain.
    intended = "https://form.test"
    element, log = await _run_heal_capture(intended, readbacks=["form.test", "form.test"])
    element.input_fill.assert_awaited_once_with(text=intended)
    assert element.input_fill.await_count == 1
    assert log.info.call_args.kwargs["refill_confirmed"] is False


@pytest.mark.asyncio
async def test_post_refill_readback_non_confirmed_for_empty_value() -> None:
    # The refill did not stick (the field cleared): an empty post-refill read-back is NOT a full-value match,
    # so it must log as non-confirmed rather than "not the loss signature" (SKY-13631 follow-up).
    element, log = await _run_heal_capture(_LONG, readbacks=[_LONG[-TEXT_PRESS_MAX_LENGTH:], ""])
    element.input_fill.assert_awaited_once_with(text=_LONG)
    assert log.info.call_args.kwargs["refill_confirmed"] is False


@pytest.mark.asyncio
async def test_post_refill_readback_non_confirmed_for_unrelated_value() -> None:
    # An unrelated post-refill value (long enough to dodge the suffix-loss check) must still log as
    # non-confirmed: confirmation is a full case-folded match, not merely the absence of the loss signature.
    element, log = await _run_heal_capture(
        _LONG, readbacks=[_LONG[-TEXT_PRESS_MAX_LENGTH:], "something else entirely different"]
    )
    element.input_fill.assert_awaited_once_with(text=_LONG)
    assert log.info.call_args.kwargs["refill_confirmed"] is False


@pytest.mark.asyncio
async def test_heal_skips_secret_value_without_reading_back() -> None:
    # A secret that misses the dedicated verify_secret_input gate (e.g. a long token in a textarea) must not
    # be healed here: no read-back (which would log the exact length) and no rewrite of the unmasked value.
    element = make_input_element_mock(element_id="EL1")
    read_back = AsyncMock(return_value=_LONG[-TEXT_PRESS_MAX_LENGTH:])
    with patch("skyvern.webeye.actions.handler.get_input_value", new=read_back):
        await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=_LONG, is_secret_value=True, engine_selection=None
        )
    read_back.assert_not_awaited()
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_heal_does_not_fill_when_prefill_readback_raises() -> None:
    # An observational read-back that raises (a re-mounting field, a driver error) must not fail the action
    # and must not heal: the value could not be observed, so nothing is re-entered.
    element = make_input_element_mock(element_id="EL1")
    with patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(side_effect=RuntimeError("detached"))):
        await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=_LONG, engine_selection=None
        )
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_heal_does_not_fill_when_prefill_readback_times_out() -> None:
    # The bounded read-back caps at BROWSER_ACTION_TIMEOUT_MS; a read that never returns must be abandoned
    # (no heal, no raise) rather than stalling on Playwright's 30s default.
    async def _never_returns(*args: object, **kwargs: object) -> str | None:
        await asyncio.sleep(3600)
        return None

    element = make_input_element_mock(element_id="EL1")
    with (
        patch("skyvern.webeye.actions.handler.get_input_value", new=_never_returns),
        patch("skyvern.webeye.actions.handler.settings.BROWSER_ACTION_TIMEOUT_MS", 10),
    ):
        await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=_LONG, engine_selection=None
        )
    element.input_fill.assert_not_awaited()


@pytest.mark.asyncio
async def test_heal_survives_post_refill_readback_failure_without_second_write() -> None:
    # After the single refill, a post-refill read-back that raises must not fail the action, must not write a
    # second time, and is recorded as non-confirmed.
    element = make_input_element_mock(element_id="EL1")
    readbacks = AsyncMock(side_effect=[_LONG[-TEXT_PRESS_MAX_LENGTH:], RuntimeError("detached")])
    with (
        patch("skyvern.webeye.actions.handler.get_input_value", new=readbacks),
        patch("skyvern.webeye.actions.handler.LOG") as log,
    ):
        await _heal_truncated_freetext_input(
            skyvern_element=element, tag_name="input", text=_LONG, engine_selection=None
        )
    assert element.input_fill.await_count == 1
    assert log.info.call_args.kwargs["refill_confirmed"] is False
