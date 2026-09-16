"""Tests for MCP clipboard tools (skyvern_clipboard_read, skyvern_clipboard_write)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import browser as mcp_browser
from tests.unit._mcp_browser_fakes import make_mock_page as _make_mock_page
from tests.unit._mcp_browser_fakes import patch_get_page

# ═══════════════════════════════════════════════════
# _ensure_clipboard_permissions
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ensure_clipboard_permissions_calls_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import _ensure_clipboard_permissions

    page = _make_mock_page()
    await _ensure_clipboard_permissions(page, asyncio.get_running_loop().time() + 30)
    page.context.grant_permissions.assert_awaited_once_with(["clipboard-read", "clipboard-write"])


@pytest.mark.asyncio
async def test_ensure_clipboard_permissions_survives_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import _ensure_clipboard_permissions

    page = _make_mock_page()
    page.context.grant_permissions = AsyncMock(side_effect=Exception("not supported"))
    # Should not raise
    await _ensure_clipboard_permissions(page, asyncio.get_running_loop().time() + 30)


@pytest.mark.asyncio
async def test_ensure_clipboard_permissions_stops_at_the_shared_deadline() -> None:
    from skyvern.cli.mcp_tools.browser import _ensure_clipboard_permissions

    async def never_answers(_permissions: list[str]) -> None:
        await asyncio.sleep(3600)

    page = _make_mock_page()
    page.context.grant_permissions = never_answers
    loop = asyncio.get_running_loop()
    started = loop.time()

    await _ensure_clipboard_permissions(page, started + 0.05)

    assert loop.time() - started < 1.0


# ═══════════════════════════════════════════════════
# skyvern_clipboard_read
# ═══════════════════════════════════════════════════


async def _torn_down_on_cancel(*_args: Any, **_kwargs: Any) -> Any:
    """The real-browser shape: the driver reports its torn-down target rather than honouring the
    cancellation, so asyncio surfaces that instead of converting the deadline into a TimeoutError."""
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        raise PlaywrightError("Target page, context or browser has been closed") from None


@pytest.mark.asyncio
async def test_clipboard_read_propagates_a_cancellation_the_grant_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grant handler swallows every exception, so a driver-translated cancel must be re-raised
    there or the tool dispatches the clipboard operation despite the caller having cancelled."""
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read

    started = asyncio.Event()

    async def cancel_translating_grant(_permissions: list[str]) -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise PlaywrightError("Target page, context or browser has been closed") from None

    page = _make_mock_page()
    page.context.grant_permissions = cancel_translating_grant
    page.evaluate = AsyncMock(return_value="should never run")
    patch_get_page(monkeypatch, mcp_browser, page, BrowserContext(mode="local"))

    task = asyncio.create_task(skyvern_clipboard_read())
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_clipboard_read_propagates_a_cancellation_navigation_recovery_rode_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery settles and retries straight through a cancellation the driver swallowed, so the
    retry succeeds and no except block ever runs; success is not proof the caller still wants it."""
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read

    settling = asyncio.Event()
    calls = {"evaluate": 0}

    async def context_lost_then_succeeds(expression: object = None, arg: object | None = None) -> object:
        calls["evaluate"] += 1
        if calls["evaluate"] == 1:
            raise PlaywrightError("Execution context was destroyed, most likely because of a navigation")
        return "clipboard text"

    async def settle_that_swallows_the_cancel(state: object = None, timeout: float | None = None) -> None:
        settling.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return  # the driver absorbs it, exactly as _wait_for_navigation_settle does

    page = _make_mock_page()
    page.evaluate = context_lost_then_succeeds
    page.page.wait_for_load_state = settle_that_swallows_the_cancel
    patch_get_page(monkeypatch, mcp_browser, page, BrowserContext(mode="local"))

    task = asyncio.create_task(skyvern_clipboard_read())
    await settling.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_clipboard_write_is_not_re_sent_when_recovery_rides_through_a_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery retries the dispatch against the new document. The write must not go out again once
    the caller has been cancelled, even though the cancellation is reported either way."""
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_write

    settling = asyncio.Event()
    writes = 0

    async def context_lost_then_succeeds(expression: object = None, arg: object | None = None) -> object:
        nonlocal writes
        if "writeText" in str(expression):
            writes += 1
            if writes == 1:
                raise PlaywrightError("Execution context was destroyed, most likely because of a navigation")
        return None

    async def settle_that_swallows_the_cancel(state: object = None, timeout: float | None = None) -> None:
        settling.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return

    page = _make_mock_page()
    page.evaluate = context_lost_then_succeeds
    page.page.wait_for_load_state = settle_that_swallows_the_cancel
    patch_get_page(monkeypatch, mcp_browser, page, BrowserContext(mode="local"))

    task = asyncio.create_task(skyvern_clipboard_write(text="copied"))
    await settling.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert writes == 1, f"the write was re-sent after cancellation ({writes} dispatches)"


async def _grant_that_eats_the_deadline(_permissions: list[str]) -> None:
    await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_clipboard_grant_timeout_records_exactly_one_browser_strike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine never runs on this path, so nothing else tallies the browser that stopped
    answering — and the synthesized timeout must not be double-counted afterwards either."""
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read
    from skyvern.forge.sdk.core import skyvern_context
    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
    from skyvern.webeye.browser_health import BrowserOperation

    monkeypatch.setattr(mcp_browser, "DEFAULT_ACTION_TIMEOUT_MS", 50)
    context = SkyvernContext(request_id="test")
    monkeypatch.setattr(skyvern_context, "current", lambda: context)
    page = _make_mock_page()
    page.context.grant_permissions = _grant_that_eats_the_deadline
    page.evaluate = AsyncMock(return_value="should never run")
    patch_get_page(monkeypatch, mcp_browser, page, BrowserContext(mode="local"))

    result = await skyvern_clipboard_read()

    assert result["error"]["code"] == mcp_browser.ErrorCode.TIMEOUT, result
    assert context.browser_health.consecutive_timeouts == 1
    assert context.browser_health.stuck_operations == {BrowserOperation.EVALUATE}


@pytest.mark.asyncio
async def test_clipboard_read_does_not_dispatch_when_the_grant_used_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read

    monkeypatch.setattr(mcp_browser, "DEFAULT_ACTION_TIMEOUT_MS", 50)
    page = _make_mock_page()
    page.context.grant_permissions = _grant_that_eats_the_deadline
    page.evaluate = AsyncMock(return_value="should never run")
    patch_get_page(monkeypatch, mcp_browser, page, BrowserContext(mode="local"))

    result = await skyvern_clipboard_read()

    page.evaluate.assert_not_awaited()
    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.TIMEOUT, result


@pytest.mark.asyncio
async def test_clipboard_write_does_not_dispatch_when_the_grant_used_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_write

    monkeypatch.setattr(mcp_browser, "DEFAULT_ACTION_TIMEOUT_MS", 50)
    page = _make_mock_page()
    page.context.grant_permissions = _grant_that_eats_the_deadline
    page.evaluate = AsyncMock(return_value=None)
    patch_get_page(monkeypatch, mcp_browser, page, BrowserContext(mode="local"))

    result = await skyvern_clipboard_write(text="copied")

    # A write dispatched under an expired deadline leaves the clipboard in an unknown state,
    # whether it lands or is cancelled in flight.
    page.evaluate.assert_not_awaited()
    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.TIMEOUT, result


@pytest.mark.asyncio
async def test_clipboard_read_reports_a_deadline_transport_error_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read

    monkeypatch.setattr(mcp_browser, "DEFAULT_ACTION_TIMEOUT_MS", 50)
    page = _make_mock_page()
    page.evaluate = _torn_down_on_cancel
    patch_get_page(monkeypatch, mcp_browser, page, BrowserContext(mode="local"))

    result = await skyvern_clipboard_read()

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.TIMEOUT, result


@pytest.mark.asyncio
async def test_clipboard_write_reports_a_deadline_transport_error_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_write

    monkeypatch.setattr(mcp_browser, "DEFAULT_ACTION_TIMEOUT_MS", 50)
    page = _make_mock_page()
    page.evaluate = _torn_down_on_cancel
    patch_get_page(monkeypatch, mcp_browser, page, BrowserContext(mode="local"))

    result = await skyvern_clipboard_write(text="copied")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.TIMEOUT, result


@pytest.mark.asyncio
async def test_clipboard_read_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read

    page = _make_mock_page()
    page.evaluate = AsyncMock(return_value="hello world")
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_browser, page, ctx)

    result = await skyvern_clipboard_read()

    assert result["ok"] is True
    assert result["data"]["text"] == "hello world"


@pytest.mark.asyncio
async def test_clipboard_read_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read

    page = _make_mock_page()
    page.evaluate = AsyncMock(return_value="")
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_browser, page, ctx)

    result = await skyvern_clipboard_read()

    assert result["ok"] is True
    assert result["data"]["text"] == ""


@pytest.mark.asyncio
async def test_clipboard_read_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools import browser as mcp_browser
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read

    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))

    result = await skyvern_clipboard_read()
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_clipboard_read_evaluate_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read

    page = _make_mock_page()
    page.evaluate = AsyncMock(side_effect=Exception("Clipboard API not available"))
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_browser, page, ctx)

    result = await skyvern_clipboard_read()

    assert result["ok"] is False
    assert "Clipboard API not available" in result["error"]["message"]


# ═══════════════════════════════════════════════════
# skyvern_clipboard_write
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_clipboard_write_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_write

    page = _make_mock_page()
    page.evaluate = AsyncMock(return_value=None)
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_browser, page, ctx)

    result = await skyvern_clipboard_write(text="copied text")

    assert result["ok"] is True
    assert result["data"]["written"] is True
    assert result["data"]["length"] == 11


@pytest.mark.asyncio
async def test_clipboard_write_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_write

    page = _make_mock_page()
    page.evaluate = AsyncMock(return_value=None)
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_browser, page, ctx)

    result = await skyvern_clipboard_write(text="")

    assert result["ok"] is True
    assert result["data"]["written"] is True
    assert result["data"]["length"] == 0


@pytest.mark.asyncio
async def test_clipboard_write_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools import browser as mcp_browser
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_write

    monkeypatch.setattr(mcp_browser, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))

    result = await skyvern_clipboard_write(text="hello")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_clipboard_write_evaluate_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_write

    page = _make_mock_page()
    page.evaluate = AsyncMock(side_effect=Exception("Permission denied"))
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_browser, page, ctx)

    result = await skyvern_clipboard_write(text="hello")

    assert result["ok"] is False
    assert "Permission denied" in result["error"]["message"]


# ═══════════════════════════════════════════════════
# Roundtrip
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_clipboard_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write → Read roundtrip using a simulated clipboard."""
    from skyvern.cli.mcp_tools.browser import skyvern_clipboard_read, skyvern_clipboard_write

    clipboard_store: dict[str, str] = {"text": ""}

    page = _make_mock_page()

    async def mock_evaluate(expr: Any, *args: Any) -> Any:
        if "writeText" in str(expr):
            clipboard_store["text"] = args[0] if args else ""
            return None
        if "readText" in str(expr):
            return clipboard_store["text"]
        return None

    page.evaluate = mock_evaluate
    ctx = BrowserContext(mode="local")
    patch_get_page(monkeypatch, mcp_browser, page, ctx)

    # Write
    write_result = await skyvern_clipboard_write(text="roundtrip test")
    assert write_result["ok"] is True

    # Read back
    read_result = await skyvern_clipboard_read()
    assert read_result["ok"] is True
    assert read_result["data"]["text"] == "roundtrip test"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (mcp_browser.skyvern_clipboard_read, {}),
        (mcp_browser.skyvern_clipboard_write, {"text": "never lands"}),
    ],
    ids=["read", "write"],
)
async def test_clipboard_evaluate_action_lifecycle_bounds_a_permission_prompt_hang(
    monkeypatch: pytest.MonkeyPatch,
    tool: Callable[..., Awaitable[dict[str, Any]]],
    kwargs: dict[str, str],
) -> None:
    released = asyncio.Event()
    awaits = 0

    async def hanging_evaluate(expression: str, *args: Any) -> str:
        nonlocal awaits
        awaits += 1
        await released.wait()
        return ""

    page = _make_mock_page()
    page.evaluate = hanging_evaluate
    patch_get_page(monkeypatch, mcp_browser, page, BrowserContext(mode="local"))
    monkeypatch.setattr(mcp_browser, "DEFAULT_ACTION_TIMEOUT_MS", 150)

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await tool(**kwargs)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_browser.ErrorCode.TIMEOUT
    assert loop.time() - started < 1.0
    assert awaits == 1
