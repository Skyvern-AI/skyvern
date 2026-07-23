from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.exceptions import MissingElement, MultipleElementsFound
from skyvern.webeye.utils.dom import SkyvernElement, is_element_detached_error, resolve_locator

_DETACHED_ERROR = "ElementHandle.content_frame: Element is not attached to the DOM"
_FRAME_DETACHED_ERROR = "Locator.count: Frame was detached"
_TYPE_TIMEOUT_ERROR = "Locator.type: Timeout 10000ms exceeded."


def test_predicate_matches_detached_errors() -> None:
    assert is_element_detached_error(PlaywrightError(_DETACHED_ERROR)) is True
    assert is_element_detached_error(PlaywrightError(_FRAME_DETACHED_ERROR)) is True
    assert is_element_detached_error(PlaywrightTimeoutError(_TYPE_TIMEOUT_ERROR)) is False


def _scrape_page_with_frame() -> MagicMock:
    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"frame-1": {"id": "frame-1", "frame": "main.frame"}}
    return scraped_page


def _frame_handler(content_frame_result: object) -> MagicMock:
    handler = MagicMock()
    if isinstance(content_frame_result, BaseException):
        handler.content_frame = AsyncMock(side_effect=content_frame_result)
    else:
        handler.content_frame = AsyncMock(return_value=content_frame_result)
    return handler


@pytest.mark.asyncio
async def test_resolve_locator_requeries_detached_iframe_handle() -> None:
    content = MagicMock()
    page = MagicMock()
    page.query_selector = AsyncMock(
        side_effect=[_frame_handler(PlaywrightError(_DETACHED_ERROR)), _frame_handler(content)]
    )
    _, frame = await resolve_locator(_scrape_page_with_frame(), page, "frame-1", "[unique_id='el-1']")
    assert frame is content
    assert page.query_selector.await_count == 2


@pytest.mark.asyncio
async def test_resolve_locator_classifies_iframe_gone_after_detach() -> None:
    page = MagicMock()
    page.query_selector = AsyncMock(side_effect=[_frame_handler(PlaywrightError(_DETACHED_ERROR)), None])
    with pytest.raises(MissingElement):
        await resolve_locator(_scrape_page_with_frame(), page, "frame-1", "[unique_id='el-1']")


@pytest.mark.asyncio
async def test_resolve_locator_classifies_repeated_detach() -> None:
    page = MagicMock()
    page.query_selector = AsyncMock(
        side_effect=[
            _frame_handler(PlaywrightError(_DETACHED_ERROR)),
            _frame_handler(PlaywrightError(_DETACHED_ERROR)),
        ]
    )
    with pytest.raises(MissingElement):
        await resolve_locator(_scrape_page_with_frame(), page, "frame-1", "[unique_id='el-1']")


@pytest.mark.asyncio
async def test_resolve_locator_reraises_unrelated_errors() -> None:
    page = MagicMock()
    page.query_selector = AsyncMock(side_effect=[_frame_handler(PlaywrightError("Protocol error"))])
    with pytest.raises(PlaywrightError):
        await resolve_locator(_scrape_page_with_frame(), page, "frame-1", "[unique_id='el-1']")


def _make_element(locator: MagicMock, frame: MagicMock | None = None, xpath: str | None = None) -> SkyvernElement:
    static_element: dict = {"id": "el-1", "tagName": "input"}
    if xpath:
        static_element["xpath"] = xpath
    return SkyvernElement(locator, frame or MagicMock(), static_element)


def _locator_with_counts(*counts: object) -> MagicMock:
    locator = MagicMock()
    locator.count = AsyncMock(side_effect=list(counts))
    return locator


@pytest.mark.asyncio
async def test_input_sequentially_reresolves_stale_locator_by_xpath(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh_locator = _locator_with_counts(1)
    frame = MagicMock()
    frame.locator.return_value = fresh_locator
    element = _make_element(_locator_with_counts(0), frame=frame, xpath="//form/input")

    typed_with = AsyncMock()
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.input_sequentially", typed_with)
    await element.input_sequentially("hello")

    frame.locator.assert_called_once_with("xpath=//form/input")
    assert element.get_locator() is fresh_locator
    assert typed_with.await_args is not None
    assert typed_with.await_args.args[0] is fresh_locator


@pytest.mark.asyncio
async def test_input_sequentially_reresolves_ambiguous_locator_by_xpath(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh_locator = _locator_with_counts(1)
    frame = MagicMock()
    frame.locator.return_value = fresh_locator
    element = _make_element(_locator_with_counts(16), frame=frame, xpath="//div/pre[3]")

    typed_with = AsyncMock()
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.input_sequentially", typed_with)
    await element.input_sequentially("hello")

    frame.locator.assert_called_once_with("xpath=//div/pre[3]")
    assert element.get_locator() is fresh_locator
    assert typed_with.await_args is not None
    assert typed_with.await_args.args[0] is fresh_locator


@pytest.mark.asyncio
async def test_input_sequentially_rejects_ambiguous_locator_without_unique_xpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambiguous_locator = _locator_with_counts(16)
    frame = MagicMock()
    frame.locator.return_value = _locator_with_counts(2)
    element = _make_element(ambiguous_locator, frame=frame, xpath="//div/pre")

    typed_with = AsyncMock()
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.input_sequentially", typed_with)

    with pytest.raises(MultipleElementsFound):
        await element.input_sequentially("hello")

    typed_with.assert_not_awaited()


@pytest.mark.asyncio
async def test_input_sequentially_keeps_locator_on_ambiguous_xpath(monkeypatch: pytest.MonkeyPatch) -> None:
    stale_locator = _locator_with_counts(0)
    frame = MagicMock()
    frame.locator.return_value = _locator_with_counts(2)
    element = _make_element(stale_locator, frame=frame, xpath="//form/input")

    typed_with = AsyncMock()
    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.input_sequentially", typed_with)
    await element.input_sequentially("hello")

    assert element.get_locator() is stale_locator


@pytest.mark.asyncio
async def test_input_sequentially_treats_detached_frame_count_as_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh_locator = _locator_with_counts(1)
    frame = MagicMock()
    frame.locator.return_value = fresh_locator
    element = _make_element(
        _locator_with_counts(PlaywrightError(_FRAME_DETACHED_ERROR)), frame=frame, xpath="//form/input"
    )

    monkeypatch.setattr("skyvern.webeye.actions.handler_utils.input_sequentially", AsyncMock())
    await element.input_sequentially("hello")

    assert element.get_locator() is fresh_locator


@pytest.mark.asyncio
async def test_input_sequentially_classifies_timeout_when_element_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _make_element(_locator_with_counts(1, 0))
    monkeypatch.setattr(
        "skyvern.webeye.actions.handler_utils.input_sequentially",
        AsyncMock(side_effect=PlaywrightTimeoutError(_TYPE_TIMEOUT_ERROR)),
    )
    with pytest.raises(MissingElement):
        await element.input_sequentially("hello")


@pytest.mark.asyncio
async def test_input_sequentially_reraises_timeout_when_element_present(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _make_element(_locator_with_counts(1, 1))
    monkeypatch.setattr(
        "skyvern.webeye.actions.handler_utils.input_sequentially",
        AsyncMock(side_effect=PlaywrightTimeoutError(_TYPE_TIMEOUT_ERROR)),
    )
    with pytest.raises(PlaywrightTimeoutError):
        await element.input_sequentially("hello")


@pytest.mark.asyncio
async def test_press_fill_classifies_timeout_when_element_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    element = _make_element(_locator_with_counts(1, 0))

    async def _raise(*args: object, **kwargs: object) -> None:
        raise PlaywrightTimeoutError(_TYPE_TIMEOUT_ERROR)

    monkeypatch.setattr("skyvern.webeye.utils.dom.EventStrategyFactory.type_text", _raise)
    with pytest.raises(MissingElement):
        await element.press_fill("hello")
