"""The post-click dropdown pass gives up, rather than holding the action, when a click reveals a whole document."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from skyvern.webeye import action_deadline
from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import ClickAction


def _scraped_page(after_open: SimpleNamespace | AsyncMock) -> SimpleNamespace:
    rescrape = after_open if isinstance(after_open, AsyncMock) else AsyncMock(return_value=after_open)
    return SimpleNamespace(url="https://test", id_to_css_dict={}, generate_scraped_page_without_screenshots=rescrape)


async def _run_dropdown_pass(scraped_page: SimpleNamespace) -> object:
    return await handler.handle_sequential_click_for_dropdown(
        action=ClickAction(element_id="anchor", reasoning="open", intention="open the result"),
        action_history=[],
        anchor_element=MagicMock(get_id=Mock(return_value="anchor")),
        dom=MagicMock(),
        page=SimpleNamespace(url="https://test"),
        skyvern_frame=SimpleNamespace(safe_wait_for_animation_end=AsyncMock(), engine_selection=object()),
        scraped_page=scraped_page,
        incremental_scraped=SimpleNamespace(
            get_incremental_elements_num=AsyncMock(return_value=1),
            get_incremental_element_tree=AsyncMock(return_value=[{"id": "new"}]),
        ),
        task=MagicMock(),
        step=SimpleNamespace(step_id="step"),
    )


@pytest.fixture
def menu_handling(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    locate = AsyncMock(return_value=None)
    monkeypatch.setattr(handler, "locate_dropdown_menu", locate)
    return SimpleNamespace(locate=locate)


@pytest.mark.asyncio
async def test_a_click_revealing_more_controls_than_any_menu_skips_the_menu_search(
    menu_handling: SimpleNamespace,
) -> None:
    count = handler.DROPDOWN_SCAN_MAX_NEW_ELEMENTS + 1
    ids = [f"e{i}" for i in range(count)]
    after_open = SimpleNamespace(
        id_to_css_dict={i: f"[data-id={i}]" for i in ids},
        id_to_element_dict={i: {"interactable": True} for i in ids},
    )

    assert await _run_dropdown_pass(_scraped_page(after_open)) is None
    menu_handling.locate.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_scan_past_its_budget_ends_the_pass_instead_of_the_action(
    menu_handling: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler, "DROPDOWN_SCAN_BUDGET_MS", 50)
    monkeypatch.setattr(action_deadline, "ACTION_DEADLINE_HEADROOM_MS", 0)
    never_finishes = asyncio.Event()

    async def slow_rescrape() -> None:
        await never_finishes.wait()

    result = await asyncio.wait_for(_run_dropdown_pass(_scraped_page(AsyncMock(side_effect=slow_rescrape))), 5)

    assert result is None
    menu_handling.locate.assert_not_awaited()
