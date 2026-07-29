from __future__ import annotations

from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import agent_functions
from skyvern.webeye.actions import handler
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
async def test_svg_eligibility_threads_live_task_engine_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    selection = _selection()
    task = _task()
    locator = MagicMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True))
    frame = MagicMock(locator=MagicMock(return_value=locator))
    skyvern_frame = MagicMock(
        get_frame=MagicMock(return_value=frame),
        get_blocking_element_id=AsyncMock(return_value=(None, False)),
    )
    constructor = MagicMock(return_value=_element_double())
    manager = MagicMock(get_for_task=MagicMock(return_value=SimpleNamespace(engine_selection=selection)))
    monkeypatch.setattr(agent_functions, "SkyvernElement", constructor)
    monkeypatch.setattr(agent_functions.app, "BROWSER_MANAGER", manager)

    assert await agent_functions._check_svg_eligibility(skyvern_frame, {"id": "svg-1", "tagName": "svg"}, task)

    assert constructor.call_args.kwargs["engine_selection"] is selection
    manager.get_for_task.assert_called_once_with(task.task_id, workflow_run_id=task.workflow_run_id)


@pytest.mark.asyncio
async def test_css_shape_conversion_threads_live_task_engine_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    selection = _selection()
    task = _task()
    locator = MagicMock(count=AsyncMock(return_value=1))
    frame = MagicMock(locator=MagicMock(return_value=locator))
    skyvern_frame = MagicMock(
        get_frame=MagicMock(return_value=frame),
        get_blocking_element_id=AsyncMock(return_value=(None, True)),
    )
    constructor = MagicMock(return_value=_element_double())
    manager = MagicMock(get_for_task=MagicMock(return_value=SimpleNamespace(engine_selection=selection)))
    monkeypatch.setattr(agent_functions, "SkyvernElement", constructor)
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
    manager.get_for_task.assert_called_once_with(task.task_id, workflow_run_id=task.workflow_run_id)


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
    locator = MagicMock(count=AsyncMock(return_value=1))
    frame.locator.return_value = locator
    source_element = MagicMock(
        get_frame=MagicMock(return_value=frame),
        get_element_handler=AsyncMock(return_value=MagicMock()),
        press_fill=AsyncMock(),
        input_clear=AsyncMock(),
        is_visible=AsyncMock(return_value=False),
    )
    skyvern_frame = MagicMock(safe_wait_for_animation_end=AsyncMock())
    incremental = MagicMock(
        start_listen_dom_increment=AsyncMock(),
        stop_listen_dom_increment=AsyncMock(),
        get_incremental_element_tree=AsyncMock(return_value=[{"id": "option-1", "text": "match"}]),
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
    )
    resolver.assert_called_once_with(task, handler.app.BROWSER_MANAGER)
    assert incremental_constructor.call_args.kwargs["engine_selection"] is selection
    assert constructor.call_args.kwargs["engine_selection"] is selection
    selected_element.click.assert_awaited_once()
    assert selected_element.click.await_args.kwargs["engine_selection"] is selection


def test_selected_engine_rejects_foreign_error_family() -> None:
    selection = _selection()
    assert selection.is_engine_error(_SelectedError())
    assert not selection.is_engine_error(_ForeignError())
