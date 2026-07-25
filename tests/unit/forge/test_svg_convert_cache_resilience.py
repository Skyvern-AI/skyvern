import asyncio
import copy
import hashlib
from collections.abc import Generator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

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
    def get_frame(self) -> _FakeFrame:
        return _FakeFrame()

    async def get_blocking_element_id(self, element: object) -> tuple[None, bool]:
        return None, False


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
