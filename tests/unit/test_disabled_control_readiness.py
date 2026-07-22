from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import InteractWithDisabledElement
from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import (
    ClickAction,
    InputTextAction,
    SelectOption,
    SelectOptionAction,
    UploadFileAction,
)
from skyvern.webeye.actions.responses import ActionFailure
from skyvern.webeye.utils.dom import SkyvernElement


def _skyvern_element(tag_name: str = "button") -> SkyvernElement:
    return SkyvernElement(
        locator=MagicMock(),
        frame=MagicMock(),
        static_element={"id": "control", "tagName": tag_name, "attributes": {}},
    )


def _assert_disabled_failure(results: list[object]) -> None:
    assert len(results) == 1
    assert isinstance(results[0], ActionFailure)
    assert results[0].exception_type == InteractWithDisabledElement.__name__


@pytest.mark.asyncio
async def test_wait_until_enabled_returns_immediately_when_enabled() -> None:
    element = _skyvern_element()
    element.is_disabled = AsyncMock(return_value=False)  # type: ignore[method-assign]

    assert await element.wait_until_enabled() is True
    element.is_disabled.assert_awaited_once_with(dynamic=True)


@pytest.mark.asyncio
async def test_wait_until_enabled_retries_until_live_control_is_enabled() -> None:
    element = _skyvern_element()
    element.is_disabled = AsyncMock(side_effect=[True, True, False])  # type: ignore[method-assign]

    with patch("skyvern.webeye.utils.dom.asyncio.sleep", new=AsyncMock()) as sleep:
        assert await element.wait_until_enabled() is True

    assert element.is_disabled.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_wait_until_enabled_returns_false_when_deadline_expires() -> None:
    element = _skyvern_element()
    element.is_disabled = AsyncMock(return_value=True)  # type: ignore[method-assign]

    assert await element.wait_until_enabled(timeout=0) is False
    element.is_disabled.assert_awaited_once_with(dynamic=True)


@pytest.mark.asyncio
async def test_skyvern_element_click_waits_before_dispatch() -> None:
    element = _skyvern_element()
    element.is_disabled = AsyncMock(side_effect=[True, False])  # type: ignore[method-assign]
    dispatch = AsyncMock()

    with (
        patch("skyvern.webeye.utils.dom.asyncio.sleep", new=AsyncMock()),
        patch("skyvern.webeye.utils.dom.EventStrategyFactory.click_element", new=dispatch),
    ):
        await element.click(MagicMock())

    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_skyvern_element_click_classifies_permanently_disabled_control() -> None:
    element = _skyvern_element()
    element.is_disabled = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with pytest.raises(InteractWithDisabledElement):
        await element.click(MagicMock(), timeout=0)


@pytest.mark.asyncio
async def test_click_handler_classifies_control_that_stays_disabled() -> None:
    element = MagicMock()
    element.get_id.return_value = "control"
    element.is_disabled = AsyncMock(return_value=True)
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(return_value=element)
    page = MagicMock(url="https://example.test")

    with (
        patch.object(handler, "DomUtil", return_value=dom),
        patch.object(handler, "get_or_create_wait_config", new=AsyncMock(return_value=None)),
        patch.object(handler.asyncio, "sleep", new=AsyncMock()),
        patch.object(handler, "_retarget_disabled_element_for_click", new=AsyncMock(return_value=None)),
        patch.object(handler.SkyvernElement, "wait_until_enabled", new=AsyncMock(return_value=False)),
    ):
        results = await handler.handle_click_action(
            ClickAction(element_id="control"), page, MagicMock(), MagicMock(), MagicMock()
        )

    _assert_disabled_failure(results)


@pytest.mark.asyncio
async def test_input_handler_classifies_control_that_stays_disabled() -> None:
    element = MagicMock()
    element.get_id.return_value = "control"
    element.get_tag_name.return_value = "input"
    element.get_frame.return_value = MagicMock()
    element.get_locator.return_value = MagicMock()
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(return_value=element)
    frame = MagicMock()
    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"control": {"tagName": "input"}}

    with (
        patch.object(handler, "DomUtil", return_value=dom),
        patch.object(handler.SkyvernFrame, "create_instance", new=AsyncMock(return_value=frame)),
        patch.object(handler, "IncrementalScrapePage", return_value=MagicMock()),
        patch.object(handler, "get_input_value", new=AsyncMock(return_value="")),
        patch.object(handler, "get_actual_value_of_parameter_if_secret_with_task", return_value="hello"),
        patch.object(handler.SkyvernElement, "wait_until_enabled", new=AsyncMock(return_value=False)),
    ):
        results = await handler.handle_input_text_action(
            InputTextAction(element_id="control", text="hello"),
            MagicMock(),
            scraped_page,
            MagicMock(),
            MagicMock(),
        )

    _assert_disabled_failure(results)


@pytest.mark.asyncio
async def test_upload_handler_classifies_control_that_stays_disabled() -> None:
    file_url = "https://files.example.test/example.txt"
    element = MagicMock()
    element.get_id.return_value = "control"
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(return_value=element)
    task = MagicMock(navigation_goal=file_url, navigation_payload={}, organization_id="org")
    download_file = AsyncMock()

    with (
        patch.object(handler, "DomUtil", return_value=dom),
        patch.object(handler, "get_actual_value_of_parameter_if_secret_with_task", return_value=file_url),
        patch.object(handler.handler_utils, "download_file", new=download_file),
        patch.object(handler.SkyvernElement, "wait_until_enabled", new=AsyncMock(return_value=False)),
    ):
        results = await handler.handle_upload_file_action(
            UploadFileAction(element_id="control", file_url=file_url),
            MagicMock(),
            MagicMock(),
            task,
            MagicMock(),
        )

    _assert_disabled_failure(results)
    download_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_select_handler_classifies_control_that_stays_disabled() -> None:
    element = MagicMock()
    element.get_id.return_value = "control"
    element.get_tag_name.return_value = "select"
    element.is_custom_option = AsyncMock(return_value=False)
    element.is_selectable = AsyncMock(return_value=True)
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(return_value=element)
    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"control": {"tagName": "select"}}

    with (
        patch.object(handler, "DomUtil", return_value=dom),
        patch.object(handler.SkyvernElement, "wait_until_enabled", new=AsyncMock(return_value=False)),
    ):
        results = await handler.handle_select_option_action(
            SelectOptionAction(element_id="control", option=SelectOption(label="Choice")),
            MagicMock(),
            scraped_page,
            MagicMock(),
            MagicMock(),
        )

    _assert_disabled_failure(results)


@pytest.mark.asyncio
async def test_custom_option_handler_classifies_control_that_stays_disabled() -> None:
    element = MagicMock()
    element.get_id.return_value = "control"
    element.get_tag_name.return_value = "div"
    element.is_custom_option = AsyncMock(return_value=True)
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(return_value=element)
    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"control": {"tagName": "div"}}
    chain_click = AsyncMock()

    with (
        patch.object(handler, "DomUtil", return_value=dom),
        patch.object(handler.SkyvernElement, "wait_until_enabled", new=AsyncMock(return_value=False)),
        patch.object(handler, "chain_click", new=chain_click),
    ):
        results = await handler.handle_select_option_action(
            SelectOptionAction(element_id="control", option=SelectOption(label="Choice")),
            MagicMock(),
            scraped_page,
            MagicMock(),
            MagicMock(),
        )

    _assert_disabled_failure(results)
    chain_click.assert_not_awaited()
