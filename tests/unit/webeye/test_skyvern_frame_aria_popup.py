"""SkyvernFrame.get_open_aria_popup_trigger seam.

The Python wrapper evaluates the JS predicate through the frame evaluation seam and
MUST fail open (return None) on any evaluation error so screenshot-scroll policy keeps
the current scrolling behavior instead of crashing the scrape/action loop.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.utils.page import SkyvernFrame


def _frame() -> SkyvernFrame:
    return SkyvernFrame(frame=MagicMock())


@pytest.mark.asyncio
async def test_returns_dict_when_predicate_reports_open_popup() -> None:
    detail = {"role": "combobox", "hasPopup": "dialog", "tag": "div", "controlsResolved": 0}
    with patch.object(SkyvernFrame, "evaluate", new=AsyncMock(return_value=detail)) as mock_eval:
        result = await _frame().get_open_aria_popup_trigger()
    assert result == detail
    # The wrapper must call the named JS predicate.
    assert "getOpenAriaPopupTrigger" in mock_eval.await_args.kwargs["expression"]


@pytest.mark.asyncio
async def test_returns_none_when_predicate_reports_no_popup() -> None:
    with patch.object(SkyvernFrame, "evaluate", new=AsyncMock(return_value=None)):
        result = await _frame().get_open_aria_popup_trigger()
    assert result is None


@pytest.mark.asyncio
async def test_non_dict_result_coerced_to_none() -> None:
    with patch.object(SkyvernFrame, "evaluate", new=AsyncMock(return_value="unexpected")):
        result = await _frame().get_open_aria_popup_trigger()
    assert result is None


@pytest.mark.asyncio
async def test_evaluation_exception_fails_open_with_warning() -> None:
    with patch.object(SkyvernFrame, "evaluate", new=AsyncMock(side_effect=RuntimeError("boom"))):
        with patch("skyvern.webeye.utils.page.LOG.warning") as mock_warn:
            result = await _frame().get_open_aria_popup_trigger()
    assert result is None, "an evaluation error must fail open to the default (scrolling) behavior"
    assert mock_warn.called, "the fail-open path must warn"
