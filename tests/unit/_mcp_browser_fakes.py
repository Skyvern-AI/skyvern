from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_manager import SessionState
from skyvern.core.script_generations.skyvern_page import SkyvernPage


def make_real_wait_for_timeout() -> AsyncMock:
    """A `page.wait_for_timeout` fake that actually sleeps, for tests driving real deadline loops."""

    async def _wait(delay_ms: int) -> None:
        await asyncio.sleep(delay_ms / 1000)

    return AsyncMock(side_effect=_wait)


def make_session_state(**overrides: Any) -> SessionState:
    state = SessionState()
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def make_page(raw: MagicMock | None = None) -> SimpleNamespace:
    if raw is None:
        raw = MagicMock()
        raw.on = MagicMock()
    return SimpleNamespace(page=raw)


def make_mock_page(
    url: str = "https://example.com",
    *,
    with_context: bool = True,
    with_evaluate: bool = True,
    with_locator: bool = True,
    with_self_page: bool = True,
) -> MagicMock:
    page = MagicMock()
    page.url = url
    if with_self_page:
        page.page = page
    if with_evaluate:
        page.evaluate = AsyncMock(return_value={})
    if with_context:
        page.context = MagicMock()
        page.context.grant_permissions = AsyncMock()
    if with_locator:
        locator = MagicMock()
        locator.evaluate = AsyncMock(return_value="<span>hello</span>")
        locator.input_value = AsyncMock(return_value="test-value")
        page.locator = MagicMock(return_value=locator)
    return page


def make_probe_locator(
    *,
    count: int = 1,
    visible: bool = True,
    enabled: bool = True,
    side_effect: Exception | None = None,
) -> MagicMock:
    locator = MagicMock()
    locator.first = locator
    locator.count = AsyncMock(side_effect=side_effect, return_value=count)
    locator.is_visible = AsyncMock(side_effect=side_effect, return_value=visible)
    locator.is_enabled = AsyncMock(side_effect=side_effect, return_value=enabled)
    return locator


def make_skyvern_page(page: MagicMock) -> MagicMock:
    wrapper = MagicMock(spec=SkyvernPage)
    wrapper.page = page
    wrapper.url = page.url
    if "evaluate" in page.__dict__:
        wrapper.evaluate = page.evaluate
    if "locator" in page.__dict__:
        wrapper.locator = page.locator
    if "context" in page.__dict__:
        wrapper.context = page.context
    return wrapper


def make_select_option_page(*, locator_scope: Any | None = None) -> tuple[SimpleNamespace, AsyncMock]:
    native_select_option = AsyncMock(return_value="selected")
    raw_page = MagicMock()
    # The credential-safety boundary probe reads whether the target is a password input.
    probe = MagicMock()
    probe.first = probe
    probe.evaluate = AsyncMock(return_value=False)
    probe.select_option = AsyncMock(return_value="selected")
    raw_page.locator = MagicMock(return_value=probe)
    page = SimpleNamespace(page=raw_page, select_option=native_select_option)
    if locator_scope is not None:
        if isinstance(locator_scope, MagicMock):
            locator_scope.locator = MagicMock(return_value=probe)
        page._locator_scope = locator_scope
    return page, native_select_option


def make_select_like_page(target: dict[str, Any]) -> tuple[MagicMock, MagicMock]:
    control = MagicMock()
    control.first = control
    control.evaluate = AsyncMock(return_value=target)
    control.click = AsyncMock()
    control.fill = AsyncMock()
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=[])
    page.locator = MagicMock(return_value=control)
    return page, control


def patch_get_page(monkeypatch: pytest.MonkeyPatch, module: Any, page: MagicMock, ctx: BrowserContext) -> AsyncMock:
    mock = AsyncMock(return_value=(make_skyvern_page(page), ctx))
    monkeypatch.setattr(module, "get_page", mock)
    return mock
