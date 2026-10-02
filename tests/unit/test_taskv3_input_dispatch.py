from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import ElementHandle

from skyvern.forge.taskv3 import input_dispatch

Call = tuple[str, tuple[Any, ...], dict[str, Any]]


class _Device:
    def __init__(self, log: list[Call], name: str) -> None:
        self._log = log
        self._name = name

    def __getattr__(self, attr: str) -> Callable[..., Awaitable[None]]:
        async def record(*args: Any, **kwargs: Any) -> None:
            self._log.append((f"{self._name}.{attr}", args, kwargs))

        return record


class _FakeLocator(_Device):
    def __init__(self, page: _FakePage, selector: str) -> None:
        super().__init__(page.calls, f"locator({selector})")


class _FakePage(_Device):
    def __init__(self) -> None:
        self.calls: list[Call] = []
        super().__init__(self.calls, "page")
        self.keyboard = _Device(self.calls, "keyboard")
        self.mouse = _Device(self.calls, "mouse")

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)


@pytest.fixture
def page() -> _FakePage:
    return _FakePage()


@pytest.mark.asyncio
async def test_each_gesture_is_the_exact_playwright_call_it_replaced(page: _FakePage) -> None:
    locator = page.locator("#seg")
    await input_dispatch.click(page, "#go", timeout=5000, force=True)
    await input_dispatch.click(page, locator, timeout=2000)
    await input_dispatch.click_at(page, 10.0, 20.0)
    await input_dispatch.hover(page, "#menu", timeout=2000)
    await input_dispatch.focus(page, "#f", timeout=2000)
    await input_dispatch.fill(page, "#f", "v", timeout=100)
    await input_dispatch.clear(page, "#f", timeout=100)
    await input_dispatch.type_keys(page, "#f", "ab", delay=15, timeout=900)
    await input_dispatch.type_keys(page, "#f", "ab", timeout=900)
    await input_dispatch.type_keys(page, None, "7", delay=40)
    await input_dispatch.type_keys(page, locator, "7", delay=40)
    await input_dispatch.press(page, "#f", "Enter")
    await input_dispatch.press(page, None, "Escape")
    await input_dispatch.press(page, locator, "Backspace")
    await input_dispatch.select_option(page, "#s", value=["a"], timeout=100, force=False)
    await input_dispatch.wheel(page, 0, -800)

    assert page.calls == [
        ("page.click", ("#go",), {"timeout": 5000, "force": True}),
        ("locator(#seg).click", (), {"timeout": 2000}),
        ("mouse.click", (10.0, 20.0), {}),
        ("page.hover", ("#menu",), {"timeout": 2000}),
        ("page.focus", ("#f",), {"timeout": 2000}),
        ("page.fill", ("#f", "v"), {"timeout": 100}),
        ("page.fill", ("#f", ""), {"timeout": 100}),
        ("page.type", ("#f", "ab"), {"delay": 15, "timeout": 900}),
        ("page.type", ("#f", "ab"), {"timeout": 900}),
        ("keyboard.type", ("7",), {"delay": 40}),
        ("locator(#seg).press_sequentially", ("7",), {"delay": 40}),
        ("page.press", ("#f", "Enter"), {}),
        ("keyboard.press", ("Escape",), {}),
        ("locator(#seg).press", ("Backspace",), {}),
        ("page.select_option", ("#s",), {"value": ["a"], "timeout": 100, "force": False}),
        ("mouse.wheel", (0, -800), {}),
    ]


@pytest.mark.asyncio
async def test_an_element_handle_is_clicked_with_only_the_arguments_given() -> None:
    handle = MagicMock(spec=ElementHandle)
    handle.click = AsyncMock()

    await input_dispatch.click_handle(_FakePage(), handle)

    handle.click.assert_awaited_once_with()
