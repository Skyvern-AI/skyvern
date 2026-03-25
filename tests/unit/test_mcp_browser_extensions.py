"""Tests for PR3 MCP browser extensions: drag, file_upload, evaluate async IIFE."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.cli.mcp_tools.browser import _wrap_async_iife

# -- Helpers --


def _fake_page(raw: MagicMock | None = None) -> SimpleNamespace:
    if raw is None:
        raw = MagicMock()
    return SimpleNamespace(page=raw, click=AsyncMock(), evaluate=AsyncMock())


def _patch_get_page(monkeypatch: pytest.MonkeyPatch, page=None, ctx=None):
    if page is None:
        page = _fake_page()
    if ctx is None:
        ctx = BrowserContext(mode="local")

    async def fake_get_page(**kwargs):
        return page, ctx

    monkeypatch.setattr("skyvern.cli.mcp_tools.browser.get_page", fake_get_page)
    return page, ctx


# -- _wrap_async_iife --


class TestWrapAsyncIIFE:
    @pytest.mark.parametrize(
        "expr,expected",
        [
            ("document.title", "document.title"),
            ("1 + 2", "1 + 2"),
            (
                "await fetch('/api')",
                "(async () => { return await fetch('/api') })()",
            ),
            (
                "await a\nawait b",
                "(async () => { await a\nawait b })()",
            ),
            (
                "await a\nreturn await b",
                "(async () => { await a\nreturn await b })()",
            ),
            (
                "// await is cool\n1+1",
                "// await is cool\n1+1",
            ),
            (
                "(async () => { return await x })()",
                "(async () => { return await x })()",
            ),
        ],
        ids=[
            "no-await-simple",
            "no-await-arithmetic",
            "single-line-await",
            "multi-line-await",
            "multi-line-explicit-return",
            "await-in-comment-only",
            "already-wrapped",
        ],
    )
    def test_wrapping(self, expr: str, expected: str) -> None:
        assert _wrap_async_iife(expr) == expected


# -- skyvern_evaluate async wrapping --


class TestEvaluateAsyncWrapping:
    @pytest.mark.asyncio
    async def test_plain_expression_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page, _ = _patch_get_page(monkeypatch)
        page.evaluate = AsyncMock(return_value="hello")
        result = await mcp_browser.skyvern_evaluate(expression="document.title")
        assert result["ok"] is True
        page.evaluate.assert_awaited_once_with("document.title")

    @pytest.mark.asyncio
    async def test_await_expression_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page, _ = _patch_get_page(monkeypatch)
        page.evaluate = AsyncMock(return_value={"ok": True})
        result = await mcp_browser.skyvern_evaluate(expression="await fetch('/api')")
        assert result["ok"] is True
        page.evaluate.assert_awaited_once_with("(async () => { return await fetch('/api') })()")


# -- skyvern_drag --


class TestDrag:
    @pytest.mark.asyncio
    async def test_selector_only_calls_drag_and_drop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        raw = MagicMock()
        raw.drag_and_drop = AsyncMock()
        page = _fake_page(raw)
        _patch_get_page(monkeypatch, page=page)

        result = await mcp_browser.skyvern_drag(source_selector="#src", target_selector="#tgt")
        assert result["ok"] is True
        assert result["data"]["mode"] == "selector"
        raw.drag_and_drop.assert_awaited_once_with("#src", "#tgt", timeout=30000)

    @pytest.mark.asyncio
    async def test_intent_mode_calls_do_act(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page, _ = _patch_get_page(monkeypatch)
        do_act = AsyncMock(return_value=SimpleNamespace(prompt="", completed=True))
        monkeypatch.setattr("skyvern.cli.mcp_tools.browser.do_act", do_act)

        result = await mcp_browser.skyvern_drag(source_intent="the task card", target_intent="the Done column")
        assert result["ok"] is True
        assert result["data"]["mode"] == "ai"
        do_act.assert_awaited_once()
        prompt = do_act.await_args[0][1]
        assert "task card" in prompt
        assert "Done column" in prompt

    @pytest.mark.asyncio
    async def test_missing_source_returns_error(self) -> None:
        result = await mcp_browser.skyvern_drag(target_selector="#tgt")
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_missing_target_returns_error(self) -> None:
        result = await mcp_browser.skyvern_drag(source_selector="#src")
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_no_browser_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.core.session_manager import BrowserNotAvailableError

        async def raise_err(**kw):
            raise BrowserNotAvailableError()

        monkeypatch.setattr("skyvern.cli.mcp_tools.browser.get_page", raise_err)
        result = await mcp_browser.skyvern_drag(source_selector="#src", target_selector="#tgt")
        assert result["ok"] is False


# -- skyvern_file_upload --


class TestFileUpload:
    @pytest.mark.asyncio
    async def test_local_path_uses_set_input_files(self, monkeypatch: pytest.MonkeyPatch) -> None:
        raw = MagicMock()
        mock_locator = MagicMock()
        mock_locator.first = mock_locator
        mock_locator.set_input_files = AsyncMock()
        raw.locator = MagicMock(return_value=mock_locator)
        page = _fake_page(raw)
        _patch_get_page(monkeypatch, page=page)

        result = await mcp_browser.skyvern_file_upload(
            file_paths=["/tmp/test.txt"],
            selector="input[type=file]",
        )
        assert result["ok"] is True
        assert result["data"]["files_count"] == 1
        raw.locator.assert_called_once_with("input[type=file]")
        # set_input_files receives a list with the single file
        mock_locator.set_input_files.assert_awaited_once_with(["/tmp/test.txt"], timeout=30000)

    @pytest.mark.asyncio
    async def test_intent_only_local_file_uses_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Intent-only + local file should use page.upload_file (AI resolution), not crash."""
        page, _ = _patch_get_page(monkeypatch)
        page.upload_file = AsyncMock(return_value="ok")
        result = await mcp_browser.skyvern_file_upload(
            file_paths=["/tmp/resume.pdf"],
            intent="the upload button",
        )
        assert result["ok"] is True
        page.upload_file.assert_awaited_once_with(
            selector=None,
            files="/tmp/resume.pdf",
            prompt="the upload button",
            ai="proactive",
            timeout=30000,
        )

    @pytest.mark.asyncio
    async def test_url_uses_sdk_upload_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page, _ = _patch_get_page(monkeypatch)
        page.upload_file = AsyncMock(return_value="ok")
        result = await mcp_browser.skyvern_file_upload(
            file_paths=["https://example.com/file.pdf"],
            selector="input[type=file]",
        )
        assert result["ok"] is True
        page.upload_file.assert_awaited_once_with(
            selector="input[type=file]",
            files="https://example.com/file.pdf",
            timeout=30000,
        )

    @pytest.mark.asyncio
    async def test_multiple_local_files_set_together(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Multiple local files should be passed to set_input_files as a single list, not one at a time."""
        raw = MagicMock()
        mock_locator = MagicMock()
        mock_locator.first = mock_locator
        mock_locator.set_input_files = AsyncMock()
        raw.locator = MagicMock(return_value=mock_locator)
        page = _fake_page(raw)
        _patch_get_page(monkeypatch, page=page)

        result = await mcp_browser.skyvern_file_upload(
            file_paths=["/tmp/a.txt", "/tmp/b.txt"],
            selector="input[type=file]",
        )
        assert result["ok"] is True
        assert result["data"]["files_count"] == 2
        # Single call with both files, not two separate calls
        mock_locator.set_input_files.assert_awaited_once_with(["/tmp/a.txt", "/tmp/b.txt"], timeout=30000)

    @pytest.mark.asyncio
    async def test_multi_url_returns_error(self) -> None:
        result = await mcp_browser.skyvern_file_upload(
            file_paths=["https://example.com/a.pdf", "https://example.com/b.pdf"],
            selector="input[type=file]",
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_mixed_local_and_url_returns_error(self) -> None:
        result = await mcp_browser.skyvern_file_upload(
            file_paths=["/tmp/local.txt", "https://example.com/remote.pdf"],
            selector="input[type=file]",
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
        assert "mix" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_multi_file_intent_only_returns_error(self) -> None:
        """Multi-file + intent-only is not supported (can't resolve element AND set multiple files)."""
        result = await mcp_browser.skyvern_file_upload(
            file_paths=["/tmp/a.txt", "/tmp/b.txt"],
            intent="the upload button",
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_empty_file_paths_returns_error(self) -> None:
        result = await mcp_browser.skyvern_file_upload(file_paths=[], selector="input[type=file]")
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_no_trigger_element_returns_error(self) -> None:
        result = await mcp_browser.skyvern_file_upload(file_paths=["/tmp/test.txt"])
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_no_browser_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.core.session_manager import BrowserNotAvailableError

        async def raise_err(**kw):
            raise BrowserNotAvailableError()

        monkeypatch.setattr("skyvern.cli.mcp_tools.browser.get_page", raise_err)
        result = await mcp_browser.skyvern_file_upload(file_paths=["/tmp/test.txt"], selector="input[type=file]")
        assert result["ok"] is False
