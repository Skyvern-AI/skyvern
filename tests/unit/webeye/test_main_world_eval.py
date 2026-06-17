"""Tests for the generic main-world evaluation hook."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.main_world_eval import (
    clear_main_world_prefix,
    configure_main_world_prefix,
    evaluate_in_main_world,
    get_main_world_prefix,
)


class _MockBrowserContext:
    """Hashable stand-in for BrowserContext (WeakKeyDictionary key)."""


def _mock_page(prefix: str | None = None) -> tuple[MagicMock, _MockBrowserContext]:
    context = _MockBrowserContext()
    page = MagicMock()
    page.context = context
    page.evaluate = AsyncMock()
    cdp_session = MagicMock()
    cdp_session.send = AsyncMock(return_value={"result": {"value": "ok"}})
    cdp_session.detach = AsyncMock()
    page.context_cdp_session = cdp_session
    context_mock = MagicMock(wraps=context)
    context_mock.new_cdp_session = AsyncMock(return_value=cdp_session)
    page.context = context_mock
    if prefix is not None:
        configure_main_world_prefix(context_mock, prefix)
    return page, context_mock


@pytest.fixture(autouse=True)
def _reset_prefix_state() -> None:
    # WeakKeyDictionary already drops mock contexts when they go out of scope.
    return None


class TestNoPrefixConfigured:
    @pytest.mark.asyncio
    async def test_passes_through_to_page_evaluate_no_arg(self) -> None:
        page = MagicMock()
        page.context = _MockBrowserContext()
        page.evaluate = AsyncMock(return_value=42)

        result = await evaluate_in_main_world(page, "() => 42")

        assert result == 42
        page.evaluate.assert_awaited_once_with("() => 42")

    @pytest.mark.asyncio
    async def test_passes_through_to_page_evaluate_with_arg(self) -> None:
        page = MagicMock()
        page.context = _MockBrowserContext()
        page.evaluate = AsyncMock(return_value="hi")

        result = await evaluate_in_main_world(page, "(x) => x", "hi")

        assert result == "hi"
        page.evaluate.assert_awaited_once_with("(x) => x", "hi")


class TestPrefixConfiguredNoArg:
    @pytest.mark.asyncio
    async def test_function_form_wraps_as_iife_and_uses_runtime_evaluate(self) -> None:
        page, context = _mock_page(prefix="// MARKER")
        page.context.new_cdp_session.return_value.send = AsyncMock(return_value={"result": {"value": 7}})

        result = await evaluate_in_main_world(page, "() => 7")

        assert result == 7
        page.evaluate.assert_not_awaited()
        page.context.new_cdp_session.assert_awaited_once_with(page)
        send_mock = page.context.new_cdp_session.return_value.send
        send_mock.assert_awaited_once()
        method, params = send_mock.await_args.args
        assert method == "Runtime.evaluate"
        assert params["expression"].startswith("// MARKER\n")
        assert "(() => 7)()" in params["expression"]
        assert params["returnByValue"] is True
        assert params["awaitPromise"] is True
        page.context.new_cdp_session.return_value.detach.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_statement_form_passes_through_unchanged_to_runtime_evaluate(self) -> None:
        page, context = _mock_page(prefix="/* P */")
        page.context.new_cdp_session.return_value.send = AsyncMock(return_value={"result": {"value": None}})

        await evaluate_in_main_world(page, "window.x = 1;")

        send_mock = page.context.new_cdp_session.return_value.send
        method, params = send_mock.await_args.args
        assert method == "Runtime.evaluate"
        assert params["expression"] == "/* P */\nwindow.x = 1;"

    @pytest.mark.asyncio
    async def test_async_arrow_function_wraps_as_iife(self) -> None:
        page, _ = _mock_page(prefix="// P")
        page.context.new_cdp_session.return_value.send = AsyncMock(return_value={"result": {"value": "done"}})

        await evaluate_in_main_world(page, "async () => fetch('/x')")

        params = page.context.new_cdp_session.return_value.send.await_args.args[1]
        assert "(async () => fetch('/x'))()" in params["expression"]

    @pytest.mark.asyncio
    async def test_runtime_evaluate_exception_is_raised(self) -> None:
        page, _ = _mock_page(prefix="// P")
        page.context.new_cdp_session.return_value.send = AsyncMock(
            return_value={
                "exceptionDetails": {
                    "text": "Uncaught",
                    "exception": {"description": "ReferenceError: x is not defined"},
                }
            }
        )

        with pytest.raises(RuntimeError, match="ReferenceError: x is not defined"):
            await evaluate_in_main_world(page, "() => x")

    @pytest.mark.asyncio
    async def test_detach_is_called_even_on_failure(self) -> None:
        page, _ = _mock_page(prefix="// P")
        page.context.new_cdp_session.return_value.send = AsyncMock(side_effect=RuntimeError("boom"))
        detach_mock = page.context.new_cdp_session.return_value.detach

        with pytest.raises(RuntimeError, match="boom"):
            await evaluate_in_main_world(page, "() => 1")

        detach_mock.assert_awaited_once()


class TestPrefixConfiguredWithArg:
    @pytest.mark.asyncio
    async def test_function_form_with_arg_inlines_arg_via_runtime_evaluate(self) -> None:
        page, _ = _mock_page(prefix="// MARK")
        page.context.new_cdp_session.return_value.send = AsyncMock(return_value={"result": {"value": "arg-value"}})

        result = await evaluate_in_main_world(page, "(x) => x", "arg-value")

        assert result == "arg-value"
        # No page.evaluate: a leading prefix line breaks Playwright's function-string normalisation.
        page.evaluate.assert_not_awaited()
        params = page.context.new_cdp_session.return_value.send.await_args.args[1]
        assert params["expression"] == '// MARK\n((x) => x)("arg-value")'

    @pytest.mark.asyncio
    async def test_function_declaration_form_with_arg_inlines_arg(self) -> None:
        page, _ = _mock_page(prefix="// MARK")
        page.context.new_cdp_session.return_value.send = AsyncMock(return_value={"result": {"value": 5}})

        await evaluate_in_main_world(page, "function (x) { return x + 1 }", 4)

        params = page.context.new_cdp_session.return_value.send.await_args.args[1]
        assert params["expression"] == "// MARK\n(function (x) { return x + 1 })(4)"

    @pytest.mark.asyncio
    async def test_non_function_expression_with_arg_drops_arg(self) -> None:
        """page.evaluate ignores extra args for non-function strings; mirror that."""
        page, _ = _mock_page(prefix="// MARK")
        page.context.new_cdp_session.return_value.send = AsyncMock(return_value={"result": {"value": 3}})

        await evaluate_in_main_world(page, "1 + 2", "ignored")

        params = page.context.new_cdp_session.return_value.send.await_args.args[1]
        assert params["expression"] == "// MARK\n1 + 2"


class TestExpressionShapeRegressions:
    """Shapes the earlier prefix-startswith-`(` heuristic mis-wrapped."""

    @pytest.mark.asyncio
    async def test_parenthesised_arithmetic_is_not_wrapped(self) -> None:
        page, _ = _mock_page(prefix="// P")
        page.context.new_cdp_session.return_value.send = AsyncMock(return_value={"result": {"value": 3}})

        await evaluate_in_main_world(page, "(1 + 2)")

        params = page.context.new_cdp_session.return_value.send.await_args.args[1]
        assert params["expression"] == "// P\n(1 + 2)"  # not ((1 + 2))() — that would TypeError

    @pytest.mark.asyncio
    async def test_object_literal_in_parens_is_not_wrapped(self) -> None:
        page, _ = _mock_page(prefix="// P")
        page.context.new_cdp_session.return_value.send = AsyncMock(return_value={"result": {"value": {"foo": 1}}})

        await evaluate_in_main_world(page, "({foo: 1})")

        params = page.context.new_cdp_session.return_value.send.await_args.args[1]
        assert params["expression"] == "// P\n({foo: 1})"  # not (({foo: 1}))() — that would TypeError

    @pytest.mark.asyncio
    async def test_single_param_arrow_function_wraps_as_iife(self) -> None:
        page, _ = _mock_page(prefix="// P")
        page.context.new_cdp_session.return_value.send = AsyncMock(return_value={"result": {"value": 1}})

        await evaluate_in_main_world(page, "x => x")

        params = page.context.new_cdp_session.return_value.send.await_args.args[1]
        assert "(x => x)()" in params["expression"]

    @pytest.mark.asyncio
    async def test_destructured_param_arrow_function_wraps_as_iife(self) -> None:
        page, _ = _mock_page(prefix="// P")
        page.context.new_cdp_session.return_value.send = AsyncMock(return_value={"result": {"value": 3}})

        await evaluate_in_main_world(page, "({a, b}) => a + b")

        params = page.context.new_cdp_session.return_value.send.await_args.args[1]
        assert "(({a, b}) => a + b)()" in params["expression"]

    @pytest.mark.asyncio
    async def test_default_param_arrow_function_wraps_as_iife(self) -> None:
        page, _ = _mock_page(prefix="// P")
        page.context.new_cdp_session.return_value.send = AsyncMock(return_value={"result": {"value": 1}})

        await evaluate_in_main_world(page, "(x = 1) => x")

        params = page.context.new_cdp_session.return_value.send.await_args.args[1]
        assert "((x = 1) => x)()" in params["expression"]

    @pytest.mark.asyncio
    async def test_named_function_with_arg_inlines_arg(self) -> None:
        page, _ = _mock_page(prefix="// P")
        page.context.new_cdp_session.return_value.send = AsyncMock(return_value={"result": {"value": 5}})

        await evaluate_in_main_world(page, "function foo(x) { return x }", 5)

        params = page.context.new_cdp_session.return_value.send.await_args.args[1]
        assert "(function foo(x) { return x })(5)" in params["expression"]


class TestRuntimeResultDecoding:
    @pytest.mark.asyncio
    async def test_returns_none_when_runtime_value_absent(self) -> None:
        page, _ = _mock_page(prefix="// P")
        page.context.new_cdp_session.return_value.send = AsyncMock(return_value={"result": {}})

        result = await evaluate_in_main_world(page, "() => undefined")

        assert result is None

    @pytest.mark.asyncio
    async def test_unserializable_value_falls_through_to_none(self) -> None:
        """CDP encodes NaN/Infinity via ``unserializableValue``; callers expect None."""
        page, _ = _mock_page(prefix="// P")
        page.context.new_cdp_session.return_value.send = AsyncMock(
            return_value={"result": {"type": "number", "unserializableValue": "NaN"}}
        )

        result = await evaluate_in_main_world(page, "() => NaN")

        assert result is None

    @pytest.mark.asyncio
    async def test_exception_details_without_description_uses_text_fallback(self) -> None:
        page, _ = _mock_page(prefix="// P")
        page.context.new_cdp_session.return_value.send = AsyncMock(
            return_value={
                "exceptionDetails": {
                    "text": "Uncaught (in promise)",
                    "exception": {"type": "object"},
                }
            }
        )

        with pytest.raises(RuntimeError, match="Uncaught"):
            await evaluate_in_main_world(page, "() => 1")


class TestPrefixRegistry:
    def test_configure_and_get_round_trip(self) -> None:
        context = _MockBrowserContext()
        configure_main_world_prefix(context, "// FOO")
        assert get_main_world_prefix(context) == "// FOO"

    def test_clear_removes_prefix(self) -> None:
        context = _MockBrowserContext()
        configure_main_world_prefix(context, "// FOO")
        clear_main_world_prefix(context)
        assert get_main_world_prefix(context) is None

    def test_get_returns_none_when_unconfigured(self) -> None:
        context = _MockBrowserContext()
        assert get_main_world_prefix(context) is None
