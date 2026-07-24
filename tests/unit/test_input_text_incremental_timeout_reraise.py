"""Ancestor-path regression for the neutral ``SkyvernPageAnalysisTimeout`` (RUS-5 / SKY-12007).

``IncrementalScrapePage.get_incremental_element_tree`` retries the analysis once without waiting;
if that second attempt also times out it raises ``SkyvernPageAnalysisTimeout`` up into
``handle_input_text_action``'s incremental-processing block. Previously this surfaced as a Playwright
``TimeoutError`` (a ``PlaywrightError``), which the handler's Playwright-specific ``except`` re-raised,
so the input action failed rather than silently succeeding. The neutral timeout is not a
``PlaywrightError``, so without explicit handling it would fall into the broad ``except Exception`` that
swallows incremental-processing errors and the action would falsely return ``ActionSuccess``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import SkyvernPageAnalysisTimeout
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import InputOrSelectContext, InputTextAction
from skyvern.webeye.actions.handler import handle_input_text_action
from skyvern.webeye.actions.responses import ActionSuccess
from skyvern.webeye.scraper.scraper import IncrementalScrapePage
from tests.unit.conftest import make_input_element_mock
from tests.unit.helpers import make_organization, make_step, make_task

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={}, navigation_goal="Fill the field")
_STEP = make_step(_NOW, _TASK, step_id="stp-1", status=StepStatus.created, order=0, output=None)


@pytest.mark.asyncio
async def test_incremental_element_tree_propagates_when_both_attempts_time_out() -> None:
    """Both the wait-until-finished attempt and the no-wait retry raising ``SkyvernPageAnalysisTimeout``
    must leave the timeout propagating out of ``get_incremental_element_tree`` (the ancestor that feeds
    the input handler), not be swallowed by the one-shot retry."""
    skyvern_frame = MagicMock()
    skyvern_frame.get_frame.return_value = MagicMock()
    skyvern_frame.get_incremental_element_tree = AsyncMock(
        side_effect=SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page")
    )

    incremental = IncrementalScrapePage(skyvern_frame=skyvern_frame)

    with pytest.raises(SkyvernPageAnalysisTimeout):
        await incremental.get_incremental_element_tree(AsyncMock())

    assert skyvern_frame.get_incremental_element_tree.await_count == 2


async def _run_input_with_incremental_error(error: BaseException) -> list:
    skyvern_el = make_input_element_mock(element_id="AADC")
    dom_instance = MagicMock()
    dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=skyvern_el)

    inc = MagicMock()
    inc.start_listen_dom_increment = AsyncMock()
    inc.stop_listen_dom_increment = AsyncMock()
    inc.get_incremental_element_tree = AsyncMock(side_effect=error)

    skyvern_frame = MagicMock()
    skyvern_frame.safe_wait_for_animation_end = AsyncMock()

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"AADC": {"tagName": "input"}}

    context = InputOrSelectContext(field="Account", is_search_bar=True, is_location_input=False)
    action = InputTextAction(element_id="AADC", text="123456", reasoning="type the account number")

    with (
        patch("skyvern.webeye.actions.handler.DomUtil", return_value=dom_instance),
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new=AsyncMock(return_value=skyvern_frame)),
        patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=inc),
        patch("skyvern.webeye.actions.handler.get_input_value", new=AsyncMock(return_value="")),
        patch(
            "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
            return_value="123456",
        ),
        patch("skyvern.webeye.actions.handler._get_input_or_select_context", new=AsyncMock(return_value=context)),
    ):
        return await handle_input_text_action(
            action=action, page=MagicMock(), scraped_page=scraped_page, task=_TASK, step=_STEP
        )


@pytest.mark.asyncio
async def test_input_action_reraises_semantic_analysis_timeout() -> None:
    """A ``SkyvernPageAnalysisTimeout`` out of incremental processing must propagate (former
    Playwright-timeout behavior), never be swallowed into an ``ActionSuccess``."""
    with pytest.raises(SkyvernPageAnalysisTimeout):
        await _run_input_with_incremental_error(
            SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page")
        )


@pytest.mark.asyncio
async def test_input_action_still_swallows_unrelated_incremental_error() -> None:
    """A genuinely unexpected non-timeout error in incremental processing keeps the pre-existing
    tolerant behavior (logged and swallowed, action returns success) — the fix is scoped to the
    semantic timeout only."""
    results = await _run_input_with_incremental_error(RuntimeError("unexpected DOM state"))
    assert len(results) == 1 and isinstance(results[0], ActionSuccess)
