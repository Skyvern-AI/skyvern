from __future__ import annotations

from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import agent_functions
from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import InputTextAction, SelectOption, SelectOptionAction
from skyvern.webeye.actions.responses import ActionFailure
from skyvern.webeye.browser_engine import BrowserEngineMetadata, BrowserEngineSelection


class _SelectedError(Exception):
    pass


class _SelectedTimeout(_SelectedError):
    pass


class _ForeignError(Exception):
    pass


async def _never_start() -> None:
    raise AssertionError("driver startup is outside this test")


def _selection() -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name="selected",
        start_driver=_never_start,
        error_type=_SelectedError,
        timeout_error_type=_SelectedTimeout,
        metadata=BrowserEngineMetadata(name="selected", version="test"),
        selection_reason="test",
    )


def _task() -> MagicMock:
    return MagicMock(
        task_id="task-1",
        workflow_run_id="run-1",
        navigation_goal="choose the matching option",
        navigation_payload={},
    )


def _element_double() -> MagicMock:
    element = MagicMock()
    element.get_element_handler = AsyncMock(return_value=MagicMock())
    element.is_interactable.return_value = True
    element.is_custom_option = AsyncMock(return_value=False)
    element.scroll_into_view = AsyncMock()
    element.click = AsyncMock()
    return element


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [_selection(), None])
async def test_svg_eligibility_reuses_frame_selection_after_browser_state_removal(
    selection: BrowserEngineSelection | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task()
    locator = MagicMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True))
    frame = MagicMock(locator=MagicMock(return_value=locator))
    skyvern_frame = MagicMock(
        engine_selection=selection,
        get_frame=MagicMock(return_value=frame),
        get_blocking_element_id=AsyncMock(return_value=(None, False)),
    )
    constructor = MagicMock(return_value=_element_double())
    manager = MagicMock(get_for_task=MagicMock(return_value=None))
    resolver = MagicMock(return_value=None)
    monkeypatch.setattr(agent_functions, "SkyvernElement", constructor)
    monkeypatch.setattr(agent_functions.app, "BROWSER_MANAGER", manager)
    monkeypatch.setattr(agent_functions, "_resolve_engine_selection", resolver)

    assert await agent_functions._check_svg_eligibility(skyvern_frame, {"id": "svg-1", "tagName": "svg"}, task)

    assert constructor.call_args.kwargs["engine_selection"] is selection
    resolver.assert_not_called()
    manager.get_for_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [_selection(), None])
async def test_css_shape_conversion_reuses_frame_selection_after_browser_state_replacement(
    selection: BrowserEngineSelection | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = _selection()
    task = _task()
    locator = MagicMock(count=AsyncMock(return_value=1))
    frame = MagicMock(locator=MagicMock(return_value=locator))
    skyvern_frame = MagicMock(
        engine_selection=selection,
        get_frame=MagicMock(return_value=frame),
        get_blocking_element_id=AsyncMock(return_value=(None, True)),
    )
    constructor = MagicMock(return_value=_element_double())
    manager = MagicMock(get_for_task=MagicMock(return_value=SimpleNamespace(engine_selection=replacement)))
    resolver = MagicMock(return_value=replacement)
    monkeypatch.setattr(agent_functions, "SkyvernElement", constructor)
    monkeypatch.setattr(agent_functions, "_resolve_engine_selection", resolver)
    monkeypatch.setattr(
        agent_functions,
        "app",
        SimpleNamespace(
            CACHE=SimpleNamespace(get=AsyncMock(return_value=None)),
            BROWSER_MANAGER=manager,
        ),
    )
    monkeypatch.setattr(agent_functions, "_is_element_already_dropped", MagicMock(return_value=False))
    monkeypatch.setattr(agent_functions, "_mark_element_as_dropped", MagicMock())
    monkeypatch.setattr(agent_functions, "json_to_html", MagicMock(return_value="<span></span>"))

    await agent_functions._convert_css_shape_to_string(
        skyvern_frame,
        {"id": "shape-1", "tagName": "span", "attributes": {}},
        task,
    )

    assert constructor.call_args.kwargs["engine_selection"] is selection
    resolver.assert_not_called()
    manager.get_for_task.assert_not_called()


@pytest.mark.asyncio
async def test_dom_filter_threads_dom_public_engine_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    selection = _selection()
    dom = MagicMock(engine_selection=selection, check_id_in_dom=AsyncMock(return_value=True))
    constructor = MagicMock(return_value=_element_double())
    monkeypatch.setattr(handler, "SkyvernElement", constructor)

    helper = handler.check_existed_but_not_option_element_in_dom_factory(dom)
    assert await helper({"id": "option-1"}, MagicMock()) is True

    assert constructor.call_args.kwargs["engine_selection"] is selection


@pytest.mark.asyncio
async def test_autocomplete_threads_one_fresh_selection_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    selection = _selection()
    task = _task()
    frame = MagicMock()
    locator = MagicMock(count=AsyncMock(return_value=1), element_handle=AsyncMock(return_value=MagicMock()))
    frame.locator.return_value = locator
    source_element = MagicMock(
        get_frame=MagicMock(return_value=frame),
        get_element_handler=AsyncMock(return_value=MagicMock()),
        press_fill=AsyncMock(),
        input_clear=AsyncMock(),
        is_visible=AsyncMock(return_value=False),
    )
    skyvern_frame = MagicMock(
        safe_wait_for_animation_end=AsyncMock(), parse_element_from_html=AsyncMock(return_value={})
    )
    cleanup = AsyncMock(side_effect=lambda _frame, _url, tree: tree)
    cleanup_factory = MagicMock(return_value=cleanup)

    async def scrape(cleaner: object) -> list[dict]:
        return await cleaner(frame, "", [{"id": "option-1", "text": "match"}])  # type: ignore[operator]

    incremental = MagicMock(
        start_listen_dom_increment=AsyncMock(),
        stop_listen_dom_increment=AsyncMock(),
        get_incremental_element_tree=AsyncMock(side_effect=scrape),
        build_html_tree=MagicMock(return_value="<div>match</div>"),
        id_to_element_dict={"option-1": {"id": "option-1", "tagName": "div"}},
    )
    selected_element = _element_double()
    constructor = MagicMock(return_value=selected_element)
    resolver = MagicMock(return_value=selection)
    monkeypatch.setattr(handler, "resolve_engine_selection_for_task", resolver)
    monkeypatch.setattr(handler.SkyvernFrame, "create_instance", AsyncMock(return_value=skyvern_frame))
    incremental_constructor = MagicMock(return_value=incremental)
    monkeypatch.setattr(handler, "IncrementalScrapePage", incremental_constructor)
    monkeypatch.setattr(handler, "SkyvernElement", constructor)
    monkeypatch.setattr(handler.app.AGENT_FUNCTION, "cleanup_element_tree_factory", cleanup_factory)
    monkeypatch.setattr(
        handler.app,
        "AUTO_COMPLETION_LLM_API_HANDLER",
        AsyncMock(return_value={"id": "option-1", "relevance_float": 1.0}),
    )
    monkeypatch.setattr(handler.prompt_engine, "load_prompt", MagicMock(return_value="prompt"))
    monkeypatch.setattr(handler, "get_slim_output_template_value", AsyncMock(return_value=None))
    monkeypatch.setattr(
        handler.skyvern_context,
        "ensure_context",
        MagicMock(return_value=SimpleNamespace(tz_info=UTC)),
    )
    await handler.choose_auto_completion_dropdown(
        context=MagicMock(is_search_bar=False, field="field", intention=None),
        page=MagicMock(),
        scraped_page=MagicMock(),
        dom=MagicMock(),
        text="match",
        skyvern_element=source_element,
        step=MagicMock(),
        task=task,
        preserved_elements=[{"id": "preserved"}],
    )
    await handler._reset_autocomplete_for_llm_fallback(
        current_incremental_scraped=incremental,
        skyvern_frame=skyvern_frame,
        skyvern_element=source_element,
        page=MagicMock(),
        scraped_page=MagicMock(),
        dom=MagicMock(),
        text="match",
        task=task,
        step=MagicMock(),
        engine_selection=selection,
    )
    resolver.assert_called_once_with(task, handler.app.BROWSER_MANAGER)
    assert cleanup.await_count == 3
    assert all(call.kwargs["engine_selection"] is selection for call in cleanup_factory.call_args_list)
    assert incremental_constructor.call_args.kwargs["engine_selection"] is selection
    assert any(call.kwargs["engine_selection"] is selection for call in constructor.call_args_list)
    selected_element.click.assert_awaited_once()
    assert selected_element.click.await_args.kwargs["engine_selection"] is selection


@pytest.mark.asyncio
async def test_input_handler_passes_its_selection_snapshot_to_context_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection()
    task = _task()
    element = MagicMock(
        supports_text_input=AsyncMock(return_value=False),
        has_hidden_attr=AsyncMock(return_value=True),
        get_selectable=AsyncMock(return_value=False),
    )
    element.get_attr = AsyncMock(return_value=None)
    element.get_frame.return_value = MagicMock()
    element.get_tag_name.return_value = "input"
    element.get_id.return_value = "input-1"
    dom = MagicMock(get_skyvern_element_by_id=AsyncMock(return_value=element))
    scraped_page = MagicMock(id_to_element_dict={"input-1": {"tagName": "input"}})
    resolver = MagicMock(return_value=selection)
    context_parser = AsyncMock(return_value=MagicMock(is_date_related=False))
    monkeypatch.setattr(handler, "DomUtil", MagicMock(return_value=dom))
    monkeypatch.setattr(handler, "resolve_engine_selection_for_task", resolver)
    monkeypatch.setattr(handler.SkyvernFrame, "create_instance", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(handler, "IncrementalScrapePage", MagicMock())
    monkeypatch.setattr(handler, "get_input_value", AsyncMock(return_value=""))
    monkeypatch.setattr(handler, "get_actual_value_of_parameter_if_secret_with_task", MagicMock(return_value="hello"))
    monkeypatch.setattr(handler.SkyvernElement, "wait_until_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(handler, "_get_input_or_select_context", context_parser)
    await handler.handle_input_text_action(
        InputTextAction(element_id="input-1", text="hello"),
        MagicMock(),
        scraped_page,
        task,
        MagicMock(),
    )
    resolver.assert_called_once_with(task, handler.app.BROWSER_MANAGER)
    context_parser.assert_awaited_once()
    assert context_parser.await_args.kwargs["engine_selection"] is selection


@pytest.mark.asyncio
async def test_custom_select_handler_passes_its_selection_snapshot_to_context_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection()
    task = _task()
    element = _element_double()
    element.get_id.return_value, element.get_tag_name.return_value = "select-1", "div"
    element.get_frame.return_value = MagicMock()
    element.is_selectable = AsyncMock(return_value=True)
    for method in ("is_checkbox", "is_radio", "is_btn_input"):
        setattr(element, method, AsyncMock(return_value=False))
    element.is_visible = AsyncMock(return_value=False)
    element.blur = AsyncMock()
    dom = MagicMock(get_skyvern_element_by_id=AsyncMock(return_value=element))
    scraped_page = MagicMock(id_to_element_dict={"select-1": {"tagName": "div"}})
    resolver = MagicMock(return_value=selection)
    context_parser = AsyncMock(
        return_value=MagicMock(is_date_related=False, intention=None, field=None, is_required=False)
    )
    incremental = MagicMock(
        start_listen_dom_increment=AsyncMock(),
        stop_listen_dom_increment=AsyncMock(),
        get_incremental_element_tree=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(handler, "DomUtil", MagicMock(return_value=dom))
    monkeypatch.setattr(handler, "resolve_engine_selection_for_task", resolver)
    monkeypatch.setattr(
        handler.SkyvernFrame,
        "create_instance",
        AsyncMock(return_value=MagicMock(safe_wait_for_animation_end=AsyncMock())),
    )
    monkeypatch.setattr(handler, "IncrementalScrapePage", MagicMock(return_value=incremental))
    monkeypatch.setattr(handler.SkyvernElement, "wait_until_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(handler, "_get_input_or_select_context", context_parser)
    monkeypatch.setattr(
        handler,
        "select_from_emerging_elements",
        AsyncMock(return_value=ActionFailure(Exception("stop after context parsing"))),
    )
    await handler.handle_select_option_action(
        SelectOptionAction(element_id="select-1", option=SelectOption(label="Choice")),
        MagicMock(),
        scraped_page,
        task,
        MagicMock(),
    )
    resolver.assert_called_once_with(task, handler.app.BROWSER_MANAGER)
    context_parser.assert_awaited_once()
    assert context_parser.await_args.kwargs["engine_selection"] is selection


@pytest.mark.asyncio
async def test_dropdown_screenshot_uses_frame_selection_and_skips_selected_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection()
    locator = MagicMock(
        page=MagicMock(is_closed=MagicMock(return_value=False)),
        screenshot=AsyncMock(side_effect=_SelectedTimeout("timed out")),
    )
    candidate = MagicMock(
        get_locator=MagicMock(return_value=locator),
        is_next_to_element=AsyncMock(return_value=True),
        find_children_element_id_by_callback=AsyncMock(return_value=None),
        get_attr=AsyncMock(return_value=None),
    )
    frame = MagicMock(
        engine_selection=selection,
        get_element_visible=AsyncMock(return_value=True),
        get_scroll_x_y=AsyncMock(return_value=(4, 8)),
        safe_scroll_to_x_y=AsyncMock(),
    )
    incremental = MagicMock(skyvern_frame=frame, element_tree=[{"id": "candidate"}])
    anchor = MagicMock(is_visible=AsyncMock(return_value=True))
    monkeypatch.setattr(handler.SkyvernElement, "create_from_incremental", AsyncMock(return_value=candidate))
    llm = AsyncMock()
    monkeypatch.setattr(handler.app, "SECONDARY_LLM_API_HANDLER", llm)

    assert await handler.locate_dropdown_menu(anchor, incremental, MagicMock(), _task()) is None

    assert locator.screenshot.await_count == 2
    frame.safe_scroll_to_x_y.assert_awaited_once_with(4, 8)
    llm.assert_not_awaited()


def test_selected_engine_rejects_foreign_error_family() -> None:
    selection = _selection()
    assert selection.is_engine_error(_SelectedError())
    assert not selection.is_engine_error(_ForeignError())
