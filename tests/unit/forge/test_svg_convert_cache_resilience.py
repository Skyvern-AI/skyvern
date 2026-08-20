import asyncio
import copy
import hashlib
from collections.abc import Generator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge import agent_functions
from skyvern.forge.sdk.cache.base import CACHE_EXPIRE_TIME
from skyvern.forge.sdk.core import skyvern_context


def _svg_element() -> dict[str, Any]:
    return {
        "tagName": "svg",
        "id": "AAAK",
        "attributes": {"id": "AAAK"},
        "children": [{"tagName": "path", "attributes": {"d": "M0 0h10v10z"}}],
    }


def _svg_cache_key(element: dict[str, Any]) -> str:
    svg_element = agent_functions._remove_skyvern_attributes(element)
    svg_html = agent_functions.json_to_html(svg_element)
    return agent_functions._get_svg_cache_key(hashlib.sha256(svg_html.encode("utf-8")).hexdigest())


class _FailingCache:
    async def get(self, key: str) -> Any:
        raise ConnectionError("redis unavailable")

    async def set(self, key: str, value: Any, ex: Any = CACHE_EXPIRE_TIME) -> None:
        raise ConnectionError("redis unavailable")


class _RecordingFailingCache(_FailingCache):
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, Any, Any]] = []

    async def set(self, key: str, value: Any, ex: Any = CACHE_EXPIRE_TIME) -> None:
        self.set_calls.append((key, value, ex))
        await super().set(key, value, ex=ex)


class _MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.fail_get = False

    async def get(self, key: str) -> Any:
        if self.fail_get:
            raise ConnectionError("redis get unavailable")
        return self.values.get(key)

    async def set(self, key: str, value: Any, ex: Any = CACHE_EXPIRE_TIME) -> None:
        self.values[key] = value


class _SetFailingMemoryCache:
    async def get(self, key: str) -> Any:
        return None

    async def set(self, key: str, value: Any, ex: Any = CACHE_EXPIRE_TIME) -> None:
        raise ConnectionError("redis set unavailable")


class _FakeLocator:
    @property
    def page(self) -> Any:
        return SimpleNamespace(is_closed=lambda: False)

    async def count(self) -> int:
        return 1

    async def is_visible(self, timeout: float) -> bool:
        return True

    async def element_handle(self, timeout: float) -> object:
        return object()

    async def scroll_into_view_if_needed(self, timeout: float) -> None:
        return None

    async def wait_for(self, state: str, timeout: float) -> None:
        return None

    async def screenshot(self, timeout: float, animations: str) -> bytes:
        return b"fake-png"


class _FakeFrame:
    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator()


class _FakeSkyvernFrame:
    engine_selection = None

    def get_frame(self) -> _FakeFrame:
        return _FakeFrame()

    async def get_blocking_element_id(self, element: object) -> tuple[None, bool]:
        return None, False


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [MagicMock(), None])
async def test_taskless_cleanup_threads_explicit_running_driver_selection(selection: object | None) -> None:
    frame = MagicMock(url="https://example.com")

    with (
        patch.object(agent_functions, "_resolve_engine_selection") as resolve,
        patch.object(agent_functions.settings, "SVG_MAX_PARSING_ELEMENT_CNT", 3000),
        patch.object(agent_functions.settings, "ENABLE_CSS_SVG_PARSING", False),
        patch.object(agent_functions, "app", SimpleNamespace(SVG_CSS_CONVERTER_LLM_API_HANDLER=None)),
        patch.object(
            agent_functions.SkyvernFrame, "create_instance", new=AsyncMock(return_value=MagicMock())
        ) as create,
    ):
        cleanup = agent_functions.AgentFunction().cleanup_element_tree_factory(engine_selection=selection)
        await cleanup(frame, frame.url, [])

    resolve.assert_not_called()
    create.assert_awaited_once_with(frame=frame, engine_selection=selection)


@pytest.mark.asyncio
async def test_cleanup_without_selection_resolves_once_and_reuses_identity() -> None:
    selection = MagicMock()
    task = MagicMock()
    frame = MagicMock(url="https://example.com")

    with (
        patch.object(agent_functions, "_resolve_engine_selection", return_value=selection) as resolve,
        patch.object(agent_functions.settings, "SVG_MAX_PARSING_ELEMENT_CNT", 3000),
        patch.object(agent_functions.settings, "ENABLE_CSS_SVG_PARSING", False),
        patch.object(agent_functions, "app", SimpleNamespace(SVG_CSS_CONVERTER_LLM_API_HANDLER=None)),
        patch.object(
            agent_functions.SkyvernFrame, "create_instance", new=AsyncMock(return_value=MagicMock())
        ) as create,
    ):
        cleanup = agent_functions.AgentFunction().cleanup_element_tree_factory(task=task)
        await cleanup(frame, frame.url, [])
        await cleanup(frame, frame.url, [])

    resolve.assert_called_once_with(task)
    assert create.await_count == 2
    assert all(call.kwargs["engine_selection"] is selection for call in create.await_args_list)


def test_svg_local_invalid_shape_cache_uses_short_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_functions.time, "monotonic", lambda: 100.0)

    agent_functions._cache_svg_shape_locally(
        "svg-key",
        agent_functions.INVALID_SHAPE,
        ex=timedelta(weeks=1),
    )

    assert agent_functions._SVG_LOCAL_SHAPE_CACHE["svg-key"] == (
        agent_functions.INVALID_SHAPE,
        100.0 + agent_functions.SVG_LOCAL_NEGATIVE_CACHE_EXPIRE_TIME.total_seconds(),
    )


@pytest.fixture(autouse=True)
def _reset_svg_convert_state(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    agent_functions._SVG_LOCAL_SHAPE_CACHE.clear()
    agent_functions._SVG_CONVERSION_LOCKS.clear()
    skyvern_context.set(skyvern_context.SkyvernContext())
    monkeypatch.setattr(agent_functions.prompt_engine, "load_prompt", lambda *args, **kwargs: "svg prompt")
    yield
    skyvern_context.reset()
    agent_functions._SVG_LOCAL_SHAPE_CACHE.clear()
    agent_functions._SVG_CONVERSION_LOCKS.clear()


@pytest.mark.asyncio
async def test_svg_convert_does_not_retry_llm_when_cache_set_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def handler(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"shape": "search icon", "recognized": True}

    monkeypatch.setattr(
        agent_functions,
        "app",
        SimpleNamespace(CACHE=_FailingCache(), SVG_CSS_CONVERTER_LLM_API_HANDLER=handler),
    )

    element = _svg_element()
    await agent_functions._convert_svg_to_string(element)

    assert calls == 1
    assert element["attributes"] == {"alt": "search icon"}
    assert "children" not in element


def test_svg_converter_stays_disabled_without_global_or_org_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = MagicMock(side_effect=AssertionError("org resolver must not bypass the None gate"))
    monkeypatch.setattr(
        agent_functions,
        "app",
        SimpleNamespace(SVG_CSS_CONVERTER_LLM_API_HANDLER=None),
    )
    monkeypatch.setattr(agent_functions, "get_org_aware_secondary_llm_api_handler", resolver)

    assert agent_functions._get_org_aware_svg_css_converter_llm_api_handler() is None
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_svg_converter_uses_org_fast_when_global_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = AsyncMock(return_value={"shape": "search icon", "recognized": True})
    monkeypatch.setattr(
        agent_functions,
        "app",
        SimpleNamespace(CACHE=_MemoryCache(), SVG_CSS_CONVERTER_LLM_API_HANDLER=None),
    )
    resolver = MagicMock(return_value=handler)
    monkeypatch.setattr(agent_functions, "get_org_aware_secondary_llm_api_handler", resolver)
    skyvern_context.ensure_context().org_default_secondary_llm_key = "CUSTOM_LLM_oat_fast"

    element = _svg_element()
    await agent_functions._convert_svg_to_string(element)

    resolver.assert_called_once_with()
    handler.assert_awaited_once()
    assert element["attributes"] == {"alt": "search icon"}


@pytest.mark.asyncio
async def test_svg_convert_uses_local_fallback_when_redis_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def handler(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"shape": "search icon", "recognized": True}

    monkeypatch.setattr(
        agent_functions,
        "app",
        SimpleNamespace(CACHE=_FailingCache(), SVG_CSS_CONVERTER_LLM_API_HANDLER=handler),
    )

    first = _svg_element()
    second = _svg_element()
    await agent_functions._convert_svg_to_string(first)
    await agent_functions._convert_svg_to_string(second)

    assert calls == 1
    assert first["attributes"] == {"alt": "search icon"}
    assert second["attributes"] == {"alt": "search icon"}


@pytest.mark.asyncio
async def test_svg_convert_caches_invalid_shape_locally_when_redis_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def handler(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"shape": "", "recognized": False}

    monkeypatch.setattr(
        agent_functions,
        "app",
        SimpleNamespace(CACHE=_FailingCache(), SVG_CSS_CONVERTER_LLM_API_HANDLER=handler),
    )

    first = _svg_element()
    await agent_functions._convert_svg_to_string(first)

    assert calls == 3
    assert first["isDropped"] is True
    assert "children" not in first

    skyvern_context.set(skyvern_context.SkyvernContext())
    second = _svg_element()
    await agent_functions._convert_svg_to_string(second)

    assert calls == 3
    assert second["attributes"] == {}
    assert "children" not in second


@pytest.mark.asyncio
async def test_svg_convert_caches_invalid_shape_loaded_from_redis_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def handler(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"shape": "search icon", "recognized": True}

    cache = _MemoryCache()
    cache.values[_svg_cache_key(_svg_element())] = agent_functions.INVALID_SHAPE
    monkeypatch.setattr(
        agent_functions,
        "app",
        SimpleNamespace(CACHE=cache, SVG_CSS_CONVERTER_LLM_API_HANDLER=handler),
    )

    first = _svg_element()
    await agent_functions._convert_svg_to_string(first)

    assert calls == 0
    assert first["attributes"] == {}
    assert "children" not in first

    cache.fail_get = True
    skyvern_context.set(skyvern_context.SkyvernContext())
    second = _svg_element()
    await agent_functions._convert_svg_to_string(second)

    assert calls == 0
    assert second["attributes"] == {}
    assert "children" not in second


@pytest.mark.asyncio
async def test_svg_convert_single_flights_concurrent_duplicate_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def handler(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"shape": "search icon", "recognized": True}

    monkeypatch.setattr(
        agent_functions,
        "app",
        SimpleNamespace(CACHE=_MemoryCache(), SVG_CSS_CONVERTER_LLM_API_HANDLER=handler),
    )

    elements = [copy.deepcopy(_svg_element()) for _ in range(5)]
    await asyncio.gather(*[agent_functions._convert_svg_to_string(element) for element in elements])

    assert calls == 1
    assert all(element["attributes"] == {"alt": "search icon"} for element in elements)


@pytest.mark.asyncio
async def test_svg_convert_disable_flag_bypasses_local_cache_and_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def handler(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"shape": "search icon", "recognized": True}

    provider = SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True))
    cache = _RecordingFailingCache()
    monkeypatch.setattr(
        agent_functions,
        "app",
        SimpleNamespace(
            CACHE=cache,
            SVG_CSS_CONVERTER_LLM_API_HANDLER=handler,
            EXPERIMENTATION_PROVIDER=provider,
        ),
    )
    skyvern_context.set(skyvern_context.SkyvernContext(run_id="wr_1", organization_id="o_1"))

    elements = [copy.deepcopy(_svg_element()) for _ in range(5)]
    await asyncio.gather(*[agent_functions._convert_svg_to_string(element) for element in elements])

    assert calls == len(elements)
    assert len(cache.set_calls) == len(elements)
    assert all(element["attributes"] == {"alt": "search icon"} for element in elements)
    provider.is_feature_enabled_cached.assert_any_await(
        agent_functions.DISABLE_SVG_CONVERT_CACHE_RESILIENCE_FLAG,
        "wr_1",
        properties={"organization_id": "o_1"},
    )


@pytest.mark.asyncio
async def test_svg_convert_flag_error_defaults_to_cache_resilience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def handler(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"shape": "search icon", "recognized": True}

    provider = SimpleNamespace(is_feature_enabled_cached=AsyncMock(side_effect=RuntimeError("posthog down")))
    monkeypatch.setattr(
        agent_functions,
        "app",
        SimpleNamespace(
            CACHE=_FailingCache(),
            SVG_CSS_CONVERTER_LLM_API_HANDLER=handler,
            EXPERIMENTATION_PROVIDER=provider,
        ),
    )
    skyvern_context.set(skyvern_context.SkyvernContext(run_id="wr_1", organization_id="o_1"))

    first = _svg_element()
    second = _svg_element()
    await agent_functions._convert_svg_to_string(first)
    await agent_functions._convert_svg_to_string(second)

    assert calls == 1
    assert first["attributes"] == {"alt": "search icon"}
    assert second["attributes"] == {"alt": "search icon"}


@pytest.mark.asyncio
async def test_css_shape_convert_keeps_result_when_cache_set_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def handler(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"shape": "calendar icon", "recognized": True}

    monkeypatch.setattr(
        agent_functions,
        "app",
        SimpleNamespace(CACHE=_SetFailingMemoryCache(), SVG_CSS_CONVERTER_LLM_API_HANDLER=handler),
    )

    element: dict[str, Any] = {
        "tagName": "span",
        "id": "AAAK",
        "attributes": {"id": "AAAK"},
    }
    await agent_functions._convert_css_shape_to_string(_FakeSkyvernFrame(), element)

    assert calls == 1
    assert element["attributes"]["shape-description"] == "calendar icon"


def _capturing_skyvern_element(captured: list[Any]) -> type:
    class _CapturingSkyvernElement:
        def __init__(self, *, locator: Any, frame: Any, static_element: Any, engine_selection: Any) -> None:
            captured.append(engine_selection)

        async def get_element_handler(self, timeout: float = 0.0) -> object:
            return object()

        def is_interactable(self) -> bool:
            return True

    return _CapturingSkyvernElement


def _css_shape_element() -> dict[str, Any]:
    return {"tagName": "span", "id": "AAAK", "attributes": {"id": "AAAK"}}


@pytest.mark.asyncio
async def test_svg_eligibility_taskless_path_stays_manager_free(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Any] = []
    monkeypatch.setattr(agent_functions, "app", SimpleNamespace())
    monkeypatch.setattr(agent_functions, "SkyvernElement", _capturing_skyvern_element(captured))

    eligible = await agent_functions._check_svg_eligibility(_FakeSkyvernFrame(), _svg_element(), task=None)

    assert eligible is True
    assert captured == [None]


@pytest.mark.asyncio
async def test_svg_eligibility_uses_frame_selection_when_task_present(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Any] = []
    selection = SimpleNamespace()
    browser_manager = SimpleNamespace(get_for_task=MagicMock())
    monkeypatch.setattr(agent_functions, "app", SimpleNamespace(BROWSER_MANAGER=browser_manager))
    monkeypatch.setattr(agent_functions, "SkyvernElement", _capturing_skyvern_element(captured))
    task = SimpleNamespace(task_id="tsk_1", workflow_run_id="wr_1")
    skyvern_frame = _FakeSkyvernFrame()
    skyvern_frame.engine_selection = selection

    eligible = await agent_functions._check_svg_eligibility(skyvern_frame, _svg_element(), task=task)  # type: ignore[arg-type]

    assert eligible is True
    assert captured == [selection]
    browser_manager.get_for_task.assert_not_called()


@pytest.mark.asyncio
async def test_css_shape_convert_taskless_path_stays_manager_free(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Any] = []

    async def handler(**kwargs: Any) -> dict[str, Any]:
        return {"shape": "calendar icon", "recognized": True}

    monkeypatch.setattr(
        agent_functions,
        "app",
        SimpleNamespace(CACHE=_MemoryCache(), SVG_CSS_CONVERTER_LLM_API_HANDLER=handler),
    )
    monkeypatch.setattr(agent_functions, "SkyvernElement", _capturing_skyvern_element(captured))

    await agent_functions._convert_css_shape_to_string(_FakeSkyvernFrame(), _css_shape_element(), task=None)

    assert captured == [None]


@pytest.mark.asyncio
async def test_css_shape_convert_uses_frame_selection_when_task_present(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Any] = []
    selection = SimpleNamespace()

    async def handler(**kwargs: Any) -> dict[str, Any]:
        return {"shape": "calendar icon", "recognized": True}

    browser_manager = SimpleNamespace(get_for_task=MagicMock())
    monkeypatch.setattr(
        agent_functions,
        "app",
        SimpleNamespace(
            CACHE=_MemoryCache(), SVG_CSS_CONVERTER_LLM_API_HANDLER=handler, BROWSER_MANAGER=browser_manager
        ),
    )
    monkeypatch.setattr(agent_functions, "SkyvernElement", _capturing_skyvern_element(captured))
    task = SimpleNamespace(task_id="tsk_1", workflow_run_id="wr_1")
    skyvern_frame = _FakeSkyvernFrame()
    skyvern_frame.engine_selection = selection

    await agent_functions._convert_css_shape_to_string(skyvern_frame, _css_shape_element(), task=task)  # type: ignore[arg-type]

    assert captured == [selection]
    browser_manager.get_for_task.assert_not_called()
