"""Tests for SkyvernFrame.evaluate routing into the main-world eval hook."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Frame, Page

from skyvern.webeye.main_world_eval import (
    clear_main_world_prefix,
    configure_main_world_prefix,
)
from skyvern.webeye.utils.page import SkyvernFrame


class _HashableContext:
    """Hashable + weak-ref-able stand-in for BrowserContext (WeakKeyDictionary key)."""


def _make_page_mock(prefix: str | None) -> tuple[MagicMock, _HashableContext]:
    context = _HashableContext()
    page = MagicMock(spec=Page)
    page.context = context
    page.evaluate = AsyncMock(return_value="page-evaluate-result")
    cdp_session = MagicMock()
    cdp_session.send = AsyncMock(return_value={"result": {"value": "runtime-evaluate-result"}})
    cdp_session.detach = AsyncMock()
    page.context.new_cdp_session = AsyncMock(return_value=cdp_session)  # type: ignore[attr-defined]
    if prefix is not None:
        configure_main_world_prefix(context, prefix)  # type: ignore[arg-type]
    return page, context


def _make_frame_mock() -> MagicMock:
    """A Frame mock that is NOT a Page (iframe-style)."""
    frame = MagicMock(spec=Frame)
    frame.evaluate = AsyncMock(return_value="frame-evaluate-result")
    return frame


@pytest.fixture(autouse=True)
def _short_timeout() -> object:
    return None


class TestSkyvernFrameEvaluateRouting:
    @pytest.mark.asyncio
    async def test_page_without_prefix_delegates_to_page_evaluate(self) -> None:
        page, ctx = _make_page_mock(prefix=None)

        result = await SkyvernFrame.evaluate(page, "() => 1")

        assert result == "page-evaluate-result"
        page.evaluate.assert_awaited_once_with(expression="() => 1", arg=None)
        page.context.new_cdp_session.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_page_with_prefix_and_no_arg_routes_through_runtime_evaluate(self) -> None:
        page, ctx = _make_page_mock(prefix="// MARK")
        try:
            result = await SkyvernFrame.evaluate(page, "() => 7")
        finally:
            clear_main_world_prefix(ctx)  # type: ignore[arg-type]

        assert result == "runtime-evaluate-result"
        page.evaluate.assert_not_awaited()
        params = page.context.new_cdp_session.return_value.send.await_args.args[1]  # type: ignore[attr-defined]
        assert params["expression"].startswith("// MARK\n")
        assert "(() => 7)()" in params["expression"]

    @pytest.mark.asyncio
    async def test_page_with_prefix_and_json_arg_inlines_arg(self) -> None:
        page, ctx = _make_page_mock(prefix="// MARK")
        try:
            await SkyvernFrame.evaluate(page, "(pos) => __pwCursorMove(pos)", [1.5, 2.5])
        finally:
            clear_main_world_prefix(ctx)  # type: ignore[arg-type]

        params = page.context.new_cdp_session.return_value.send.await_args.args[1]  # type: ignore[attr-defined]
        assert "((pos) => __pwCursorMove(pos))([1.5, 2.5])" in params["expression"]
        page.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_page_with_prefix_and_non_json_arg_falls_back_to_page_evaluate(self) -> None:
        """ElementHandle args can't be JSON-inlined → keep Playwright marshalling."""
        page, ctx = _make_page_mock(prefix="// MARK")
        element_handle_like = MagicMock()  # stands in for an ElementHandle/JSHandle
        try:
            await SkyvernFrame.evaluate(page, "(el) => el.blur()", element_handle_like)
        finally:
            clear_main_world_prefix(ctx)  # type: ignore[arg-type]

        page.evaluate.assert_awaited_once_with(expression="(el) => el.blur()", arg=element_handle_like)
        page.context.new_cdp_session.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_frame_target_never_routes_through_runtime_evaluate(self) -> None:
        """Iframe Frames have their own world; page-level Runtime.evaluate lands elsewhere."""
        frame = _make_frame_mock()

        await SkyvernFrame.evaluate(frame, "() => 1")

        frame.evaluate.assert_awaited_once_with(expression="() => 1", arg=None)

    @pytest.mark.asyncio
    async def test_runtime_evaluate_navigation_context_lost_triggers_recovery(self) -> None:
        """Runtime.evaluate context-destroyed must hit recovery, like PlaywrightError."""
        page, ctx = _make_page_mock(prefix="// MARK")
        page.context.new_cdp_session.return_value.send = AsyncMock(  # type: ignore[attr-defined]
            return_value={
                "exceptionDetails": {
                    "text": "Uncaught",
                    "exception": {"description": "Error: Execution context was destroyed by a navigation"},
                }
            }
        )

        with patch.object(
            SkyvernFrame,
            "_evaluate_with_navigation_recovery",
            new=AsyncMock(return_value="recovered"),
        ) as recovery:
            try:
                result = await SkyvernFrame.evaluate(page, "() => 1")
            finally:
                clear_main_world_prefix(ctx)  # type: ignore[arg-type]

        assert result == "recovered"
        recovery.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_runtime_evaluate_non_recovery_runtime_error_propagates(self) -> None:
        """Non-navigation RuntimeError must propagate, not get swallowed."""
        page, ctx = _make_page_mock(prefix="// MARK")
        page.context.new_cdp_session.return_value.send = AsyncMock(  # type: ignore[attr-defined]
            return_value={
                "exceptionDetails": {
                    "text": "Uncaught",
                    "exception": {"description": "TypeError: undefined is not a function"},
                }
            }
        )

        try:
            with pytest.raises(RuntimeError, match="undefined is not a function"):
                await SkyvernFrame.evaluate(page, "() => x.y()")
        finally:
            clear_main_world_prefix(ctx)  # type: ignore[arg-type]


class TestNavigationRecoveryRouting:
    """Recovery loop must keep using the main-world hook for prefixed Pages,
    otherwise the marker is dropped on the post-navigation re-injection + retry."""

    @pytest.mark.asyncio
    async def test_prefixed_page_recovery_uses_runtime_evaluate_throughout(self) -> None:
        """For a prefixed Page, the original call, the JS_FUNCTION_DEFS
        re-injection, AND the final retry must all run via CDP Runtime.evaluate."""
        page, ctx = _make_page_mock(prefix="// MARK")
        page.wait_for_load_state = AsyncMock()

        send_calls: list[dict[str, str]] = []

        async def fake_send(method: str, params: dict[str, str]) -> dict:
            send_calls.append({"method": method, "expression": params["expression"]})
            if len(send_calls) == 1:
                return {
                    "exceptionDetails": {
                        "text": "Uncaught",
                        "exception": {"description": "Error: Execution context was destroyed by a navigation"},
                    }
                }
            if len(send_calls) == 2:
                return {"result": {"value": None}}  # re-injection of JS_FUNCTION_DEFS
            return {"result": {"value": "final"}}  # final retry

        cdp_session = MagicMock()
        cdp_session.send = AsyncMock(side_effect=fake_send)
        cdp_session.detach = AsyncMock()
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)  # type: ignore[attr-defined]

        try:
            result = await SkyvernFrame.evaluate(page, "() => 1", timeout_ms=1000)
        finally:
            clear_main_world_prefix(ctx)  # type: ignore[arg-type]

        assert result == "final"
        assert len(send_calls) == 3
        # Every CDP call carried the configured prefix.
        for call in send_calls:
            assert call["method"] == "Runtime.evaluate"
            assert call["expression"].startswith("// MARK\n")
        # Recovery must NOT have fallen back to raw page.evaluate for a JSON-safe call.
        page.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recovery_with_element_handle_arg_preserves_page_evaluate_on_final_retry(
        self,
    ) -> None:
        """ElementHandle arg → final retry keeps page.evaluate (re-injection has
        arg=None so it can still go through the main-world hook)."""
        from playwright._impl._errors import Error as PlaywrightError

        page, ctx = _make_page_mock(prefix="// MARK")
        page.wait_for_load_state = AsyncMock()

        element_handle_like = MagicMock()  # non-JSON-inlinable
        page.evaluate = AsyncMock(
            side_effect=[
                PlaywrightError("Execution context was destroyed by a navigation"),
                "final-via-page-evaluate",
            ]
        )

        try:
            result = await SkyvernFrame.evaluate(page, "(el) => el.blur()", element_handle_like, timeout_ms=1000)
        finally:
            clear_main_world_prefix(ctx)  # type: ignore[arg-type]

        assert result == "final-via-page-evaluate"
        # Original call + final retry both went through page.evaluate (non-JSON arg).
        # Re-injection used Runtime.evaluate (None arg → main-world hook is safe).
        assert page.evaluate.await_count == 2
        page.context.new_cdp_session.assert_awaited_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_frame_target_recovery_still_uses_frame_evaluate(self) -> None:
        """Iframe Frames keep per-frame evaluate even during recovery."""
        from playwright._impl._errors import Error as PlaywrightError

        frame = _make_frame_mock()
        frame.wait_for_load_state = AsyncMock()
        frame.evaluate = AsyncMock(
            side_effect=[
                PlaywrightError("Execution context was destroyed by a navigation"),
                None,
                "frame-final",
            ]
        )

        result = await SkyvernFrame.evaluate(frame, "() => 1", timeout_ms=1000)

        assert result == "frame-final"
        assert frame.evaluate.await_count == 3
