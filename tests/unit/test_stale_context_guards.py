from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.exceptions import MissingElement, MissingElementDict, MultipleElementsFound
from skyvern.webeye.scraper import scraper as scraper_module
from skyvern.webeye.utils.dom import SkyvernElement, is_element_detached_error, resolve_locator

_CONTEXT_DESTROYED_ERROR = "Execution context was destroyed, most likely because of a navigation."


class _NeverResolvingHandle:
    """Frame ElementHandle whose direct handle reads never resolve. Both the selector-based
    ``get_attribute`` (which the driver re-resolves through a 30s ``:scope`` wait after the parent
    document navigated) and the handle-bound ``evaluate`` hang forever, so any regression that reads
    the id straight off the handle instead of through the common ``SkyvernFrame.evaluate`` abstraction
    trips the bounded wait."""

    async def is_visible(self) -> bool:
        return True

    async def get_attribute(self, name: str) -> str | None:
        await asyncio.Event().wait()
        raise AssertionError("get_attribute should never resolve")

    async def evaluate(self, expression: str, arg: object = None) -> object:
        await asyncio.Event().wait()
        raise AssertionError("handle.evaluate should never resolve")


class _FrameStub:
    def __init__(self, handle: _NeverResolvingHandle, parent_frame: object, page: object = None) -> None:
        self._handle = handle
        self.parent_frame = parent_frame
        self.page = page

    async def frame_element(self) -> _NeverResolvingHandle:
        return self._handle


class _EvaluateSpy:
    """Stands in for the common ``SkyvernFrame.evaluate`` abstraction and records how it was called."""

    def __init__(self, *, result: object) -> None:
        self._result = result
        self.calls: list[SimpleNamespace] = []

    async def __call__(self, *, frame: object, expression: str, arg: object = None, **kwargs: object) -> object:
        self.calls.append(SimpleNamespace(frame=frame, expression=expression, arg=arg, kwargs=kwargs))
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


@pytest.mark.asyncio
async def test_add_frame_interactable_elements_reads_id_through_parent_frame_abstraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_id = "AB12"
    handle = _NeverResolvingHandle()
    parent = object()
    frame = _FrameStub(handle, parent_frame=parent)
    engine_selection = object()
    spy = _EvaluateSpy(result=frame_id)
    monkeypatch.setattr(scraper_module.SkyvernFrame, "evaluate", spy)
    built_child = {"id": frame_id, "attributes": {}}
    skyvern_frame = MagicMock()
    skyvern_frame.build_tree_from_body = AsyncMock(return_value=([built_child], [built_child], {}))
    monkeypatch.setattr(scraper_module.SkyvernFrame, "create_instance", AsyncMock(return_value=skyvern_frame))
    monkeypatch.setattr(scraper_module, "_wait_for_scrape_ready", AsyncMock())

    elements: list[dict] = [{"id": frame_id}]
    tree: list[dict] = [{"id": frame_id}]
    result_elements, result_tree = await asyncio.wait_for(
        scraper_module.add_frame_interactable_elements(frame, 0, elements, tree, {}, engine_selection=engine_selection),
        timeout=2,
    )

    assert built_child in result_elements
    assert result_tree[0]["children"] == [built_child]
    assert skyvern_frame.build_tree_from_body.await_args.kwargs["frame_name"] == frame_id
    assert len(spy.calls) == 1
    # The iframe's ElementHandle lives in the PARENT frame's execution context, so the id must be
    # read there with the handle as the argument -- not from the child frame or off the handle.
    assert spy.calls[0].frame is parent
    assert spy.calls[0].arg is handle
    assert spy.calls[0].kwargs.get("engine_selection") is engine_selection


@pytest.mark.asyncio
async def test_add_frame_interactable_elements_reads_orphan_id_via_main_frame_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An orphan iframe (child attach arrives before its parent) briefly has parent_frame=None; its
    # handle is owned by main_frame, so the read must evaluate there -- not from the child frame.
    frame_id = "AB12"
    handle = _NeverResolvingHandle()
    main_frame = object()
    frame = _FrameStub(handle, parent_frame=None, page=SimpleNamespace(main_frame=main_frame))
    spy = _EvaluateSpy(result=frame_id)
    monkeypatch.setattr(scraper_module.SkyvernFrame, "evaluate", spy)
    built_child = {"id": frame_id, "attributes": {}}
    skyvern_frame = MagicMock()
    skyvern_frame.build_tree_from_body = AsyncMock(return_value=([built_child], [built_child], {}))
    monkeypatch.setattr(scraper_module.SkyvernFrame, "create_instance", AsyncMock(return_value=skyvern_frame))
    monkeypatch.setattr(scraper_module, "_wait_for_scrape_ready", AsyncMock())

    elements: list[dict] = [{"id": frame_id}]
    tree: list[dict] = [{"id": frame_id}]
    await asyncio.wait_for(
        scraper_module.add_frame_interactable_elements(frame, 0, elements, tree, {}),
        timeout=2,
    )

    assert len(spy.calls) == 1
    assert spy.calls[0].frame is main_frame
    assert spy.calls[0].arg is handle


@pytest.mark.asyncio
async def test_add_frame_interactable_elements_skips_when_frame_id_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = _FrameStub(_NeverResolvingHandle(), parent_frame=object())
    monkeypatch.setattr(scraper_module.SkyvernFrame, "evaluate", _EvaluateSpy(result=None))
    elements: list[dict] = [{"id": "existing"}]
    tree: list[dict] = [{"id": "existing"}]

    result = await asyncio.wait_for(
        scraper_module.add_frame_interactable_elements(frame, 0, elements, tree, {}),
        timeout=2,
    )

    assert result == (elements, tree)


@pytest.mark.asyncio
async def test_add_frame_interactable_elements_skips_when_execution_context_destroyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _FrameStub(_NeverResolvingHandle(), parent_frame=object())
    monkeypatch.setattr(
        scraper_module.SkyvernFrame, "evaluate", _EvaluateSpy(result=PlaywrightError(_CONTEXT_DESTROYED_ERROR))
    )
    elements: list[dict] = [{"id": "existing"}]
    tree: list[dict] = [{"id": "existing"}]

    result = await asyncio.wait_for(
        scraper_module.add_frame_interactable_elements(frame, 0, elements, tree, {}),
        timeout=2,
    )

    assert result == (elements, tree)


_DETACHED_ERROR = "ElementHandle.content_frame: Element is not attached to the DOM"
_FRAME_DETACHED_ERROR = "Locator.count: Frame was detached"
_FRAME_HAS_BEEN_DETACHED_ERROR = "Frame.frame_element: Frame has been detached."
_TYPE_TIMEOUT_ERROR = "Locator.type: Timeout 10000ms exceeded."


def test_predicate_matches_detached_errors() -> None:
    assert is_element_detached_error(PlaywrightError(_DETACHED_ERROR)) is True
    assert is_element_detached_error(PlaywrightError(_FRAME_DETACHED_ERROR)) is True
    assert is_element_detached_error(PlaywrightError(_FRAME_HAS_BEEN_DETACHED_ERROR)) is True
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


@pytest.mark.asyncio
async def test_find_label_for_returns_none_when_for_control_was_not_scraped() -> None:
    # The label's for= control is live (found once, carries a unique id) but is absent from the scraped dict.
    control_locator = MagicMock()
    control_locator.count = AsyncMock(return_value=1)
    control_locator.get_attribute = AsyncMock(return_value="AACH")
    frame = MagicMock()
    frame.locator = MagicMock(return_value=control_locator)
    label = SkyvernElement(MagicMock(), frame, {"id": "AABQ", "tagName": "label", "attributes": {"for": "ctl"}})
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(side_effect=MissingElementDict("AACH"))

    assert await label.find_label_for(dom) is None

    mapped = object()
    dom.get_skyvern_element_by_id = AsyncMock(return_value=mapped)
    assert await label.find_label_for(dom) is mapped
