"""Tests for PR3 MCP browser extensions: drag, file_upload, evaluate async IIFE."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client

from skyvern.cli.core import session_manager
from skyvern.cli.core.result import BrowserContext, ErrorCode
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools.browser import _wrap_async_iife
from skyvern.config import settings
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from tests.unit._mcp_browser_fakes import StaleScopePage, make_probe_locator

RUN_ID = "wr_mcp_upload_test"


@pytest.fixture()
def run_upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    downloads_dir = tmp_path / "downloads"
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(downloads_dir))
    upload_dir = downloads_dir / RUN_ID
    upload_dir.mkdir(parents=True)
    return upload_dir


# -- Helpers --


def _fake_page(raw: MagicMock | None = None) -> SimpleNamespace:
    if raw is None:
        raw = MagicMock()
    page = SimpleNamespace(page=raw, click=AsyncMock(), evaluate=AsyncMock())
    page.locator_scope = raw
    return page


def _patch_get_page(monkeypatch: pytest.MonkeyPatch, page=None, ctx=None):
    if page is None:
        page = _fake_page()
    if ctx is None:
        ctx = BrowserContext(mode="local")

    async def fake_get_page(**kwargs):
        return page, ctx

    monkeypatch.setattr("skyvern.cli.mcp_tools.browser.get_page", fake_get_page)
    return page, ctx


def _patch_file_input(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    raw = MagicMock()
    locator = MagicMock()
    locator.first = locator
    locator.set_input_files = AsyncMock()
    raw.locator = MagicMock(return_value=locator)
    _patch_get_page(monkeypatch, page=_fake_page(raw))
    return raw, locator


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
        page.locator_scope.evaluate = AsyncMock(return_value="hello")
        result = await mcp_browser.skyvern_evaluate(expression="document.title")
        assert result["ok"] is True
        page.locator_scope.evaluate.assert_awaited_once_with("document.title")

    @pytest.mark.asyncio
    async def test_await_expression_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page, _ = _patch_get_page(monkeypatch)
        page.locator_scope.evaluate = AsyncMock(return_value={"ok": True})
        result = await mcp_browser.skyvern_evaluate(expression="await fetch('/api')")
        assert result["ok"] is True
        page.locator_scope.evaluate.assert_awaited_once_with("(async () => { return await fetch('/api') })()")


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
        raw.drag_and_drop.assert_awaited_once_with("#src", "#tgt", timeout=5000)

    @pytest.mark.asyncio
    async def test_selector_drag_timeout_attributes_hidden_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        locators = {"#src": make_probe_locator(), "#tgt": make_probe_locator(visible=False)}
        raw = MagicMock()
        raw.drag_and_drop = AsyncMock(side_effect=mcp_browser.PlaywrightTimeoutError("Timeout 5000ms exceeded."))
        raw.locator = MagicMock(side_effect=lambda selector: locators[selector])
        page = _fake_page(raw)
        _patch_get_page(monkeypatch, page=page)

        result = await mcp_browser.skyvern_drag(source_selector="#src", target_selector="#tgt")

        assert result["ok"] is False
        assert result["error"]["details"]["element_state"] == "hidden"
        assert result["error"]["details"]["selector"] == "#tgt"
        assert result["error"]["details"]["source_selector"] == "#src"

    @pytest.mark.asyncio
    async def test_selector_drag_interception_attributes_occluded_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        locators = {"#src": make_probe_locator(), "#tgt": make_probe_locator()}
        raw = MagicMock()
        raw.drag_and_drop = AsyncMock(
            side_effect=mcp_browser.PlaywrightTimeoutError("<div class='overlay'></div> intercepts pointer events")
        )
        raw.locator = MagicMock(side_effect=lambda selector: locators[selector])
        page = _fake_page(raw)
        _patch_get_page(monkeypatch, page=page)

        result = await mcp_browser.skyvern_drag(source_selector="#src", target_selector="#tgt")

        assert result["ok"] is False
        assert result["error"]["details"]["element_state"] == "occluded"
        assert result["error"]["details"]["selector"] == "#tgt"
        assert result["error"]["details"]["source_selector"] == "#src"

    @pytest.mark.asyncio
    async def test_selector_drag_timeout_attributes_missing_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        locators = {"#src": make_probe_locator(count=0), "#tgt": make_probe_locator()}
        raw = MagicMock()
        raw.drag_and_drop = AsyncMock(side_effect=mcp_browser.PlaywrightTimeoutError("Timeout 5000ms exceeded."))
        raw.locator = MagicMock(side_effect=lambda selector: locators[selector])
        page = _fake_page(raw)
        _patch_get_page(monkeypatch, page=page)

        result = await mcp_browser.skyvern_drag(source_selector="#src", target_selector="#tgt")

        assert result["ok"] is False
        assert result["error"]["details"]["element_state"] == "not_found"
        assert result["error"]["details"]["selector"] == "#src"
        assert "source_selector" not in result["error"]["details"]

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
    async def test_standalone_stdio_upload_uses_local_download_scope(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run_upload_dir: Path,
    ) -> None:
        standalone_upload_dir = run_upload_dir.parent / str(None)
        standalone_upload_dir.mkdir()
        upload_file = standalone_upload_dir / "test.txt"
        upload_file.write_bytes(b"safe upload")
        _, mock_locator = _patch_file_input(monkeypatch)
        monkeypatch.setattr(session_manager, "_stdio_local_file_access_enabled", True)

        with skyvern_context.scoped(SkyvernContext()):
            async with Client(mcp) as client:
                tool_result = await client.call_tool(
                    "skyvern_file_upload",
                    {"file_paths": [str(upload_file)], "selector": "input[type=file]"},
                )

        result = tool_result.structured_content
        assert result is not None
        assert result["ok"] is True
        mock_locator.set_input_files.assert_awaited_once_with(
            [{"name": "test.txt", "mimeType": "text/plain", "buffer": b"safe upload"}],
            timeout=5000,
        )

    @pytest.mark.asyncio
    async def test_local_path_uses_captured_file_payload(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run_upload_dir: Path,
    ) -> None:
        upload_file = run_upload_dir / "test.txt"
        upload_file.write_bytes(b"safe upload")
        raw, mock_locator = _patch_file_input(monkeypatch)

        with skyvern_context.scoped(SkyvernContext(run_id=RUN_ID)):
            result = await mcp_browser.skyvern_file_upload(
                file_paths=[str(upload_file)],
                selector="input[type=file]",
            )

        assert result["ok"] is True
        assert result["data"]["files_count"] == 1
        raw.locator.assert_called_once_with("input[type=file]")
        mock_locator.set_input_files.assert_awaited_once_with(
            [{"name": "test.txt", "mimeType": "text/plain", "buffer": b"safe upload"}],
            timeout=5000,
        )

    @pytest.mark.asyncio
    async def test_local_upload_resolves_in_page_space_not_the_selected_frame(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run_upload_dir: Path,
    ) -> None:
        upload_file = run_upload_dir / "test.txt"
        upload_file.write_bytes(b"safe upload")
        raw, _ = _patch_file_input(monkeypatch)
        frame = MagicMock()
        _patch_get_page(monkeypatch, page=SimpleNamespace(page=raw, locator_scope=frame))

        with skyvern_context.scoped(SkyvernContext(run_id=RUN_ID)):
            result = await mcp_browser.skyvern_file_upload(
                file_paths=[str(upload_file)],
                selector="input[type=file]",
            )

        assert result["ok"] is True, result.get("error")
        raw.locator.assert_called_once_with("input[type=file]")
        frame.locator.assert_not_called()

    @pytest.mark.asyncio
    async def test_local_upload_refuses_an_unowned_frame_selection(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run_upload_dir: Path,
    ) -> None:
        upload_file = run_upload_dir / "test.txt"
        upload_file.write_bytes(b"safe upload")
        raw, mock_locator = _patch_file_input(monkeypatch)
        _patch_get_page(monkeypatch, page=StaleScopePage(raw))

        with skyvern_context.scoped(SkyvernContext(run_id=RUN_ID)):
            result = await mcp_browser.skyvern_file_upload(
                file_paths=[str(upload_file)],
                selector="input[type=file]",
            )

        assert result["ok"] is False
        assert result["error"]["code"] == ErrorCode.STALE_FRAME_SELECTION
        mock_locator.set_input_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_path_traversal_outside_run_upload_directory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run_upload_dir: Path,
    ) -> None:
        outside_file = run_upload_dir.parent / "service-secret.txt"
        outside_file.write_bytes(b"must not leave host")
        raw, mock_locator = _patch_file_input(monkeypatch)

        with skyvern_context.scoped(SkyvernContext(run_id=RUN_ID)):
            result = await mcp_browser.skyvern_file_upload(
                file_paths=[str(run_upload_dir / ".." / outside_file.name)],
                selector="input[type=file]",
            )

        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
        raw.locator.assert_not_called()
        mock_locator.set_input_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_symlink_escape_from_run_upload_directory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run_upload_dir: Path,
    ) -> None:
        outside_file = run_upload_dir.parent / "service-secret.txt"
        outside_file.write_bytes(b"must not leave host")
        symlink = run_upload_dir / "upload.txt"
        symlink.symlink_to(outside_file)
        raw, mock_locator = _patch_file_input(monkeypatch)

        with skyvern_context.scoped(SkyvernContext(run_id=RUN_ID)):
            result = await mcp_browser.skyvern_file_upload(
                file_paths=[str(symlink)],
                selector="input[type=file]",
            )

        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
        raw.locator.assert_not_called()
        mock_locator.set_input_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_symlink_swap_after_path_resolution(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run_upload_dir: Path,
    ) -> None:
        upload_file = run_upload_dir / "upload.txt"
        upload_file.write_bytes(b"safe upload")
        outside_file = run_upload_dir.parent / "service-secret.txt"
        outside_file.write_bytes(b"must not leave host")
        raw, mock_locator = _patch_file_input(monkeypatch)
        real_open = os.open
        swapped = False

        def swap_before_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal swapped
            if not swapped:
                upload_file.unlink()
                upload_file.symlink_to(outside_file)
                swapped = True
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(mcp_browser.os, "open", swap_before_open)

        with skyvern_context.scoped(SkyvernContext(run_id=RUN_ID)):
            result = await mcp_browser.skyvern_file_upload(
                file_paths=[str(upload_file)],
                selector="input[type=file]",
            )

        assert swapped is True
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
        raw.locator.assert_not_called()
        mock_locator.set_input_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fifo_open_is_nonblocking(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run_upload_dir: Path,
    ) -> None:
        upload_fifo = run_upload_dir / "upload.pipe"
        os.mkfifo(upload_fifo)
        _, mock_locator = _patch_file_input(monkeypatch)
        real_open = os.open

        def assert_nonblocking_open(
            path: Any,
            flags: int,
            *args: Any,
            **kwargs: Any,
        ) -> int:
            if path == upload_fifo.name:
                assert flags & os.O_NONBLOCK
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(mcp_browser.os, "open", assert_nonblocking_open)

        with skyvern_context.scoped(SkyvernContext(run_id=RUN_ID)):
            result = await mcp_browser.skyvern_file_upload(
                file_paths=[str(upload_fifo)],
                selector="input[type=file]",
            )

        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
        mock_locator.set_input_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_local_file_without_run_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run_upload_dir: Path,
    ) -> None:
        upload_file = run_upload_dir / "test.txt"
        upload_file.write_bytes(b"safe upload")
        raw = MagicMock()
        raw.locator = MagicMock()
        _patch_get_page(monkeypatch, page=_fake_page(raw))

        with skyvern_context.scoped(SkyvernContext()):
            result = await mcp_browser.skyvern_file_upload(
                file_paths=[str(upload_file)],
                selector="input[type=file]",
            )

        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
        raw.locator.assert_not_called()

    @pytest.mark.asyncio
    async def test_intent_only_local_file_requires_selector(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run_upload_dir: Path,
    ) -> None:
        upload_file = run_upload_dir / "resume.pdf"
        upload_file.write_bytes(b"safe upload")
        page, _ = _patch_get_page(monkeypatch)
        page.upload_file = AsyncMock(return_value="ok")

        with skyvern_context.scoped(SkyvernContext(run_id=RUN_ID)):
            result = await mcp_browser.skyvern_file_upload(
                file_paths=[str(upload_file)],
                intent="the upload button",
            )

        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
        page.upload_file.assert_not_awaited()

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
            timeout=5000,
        )

    @pytest.mark.asyncio
    async def test_rejects_local_files_above_aggregate_size(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run_upload_dir: Path,
    ) -> None:
        upload_a = run_upload_dir / "a.txt"
        upload_b = run_upload_dir / "b.txt"
        upload_a.write_bytes(b"abc")
        upload_b.write_bytes(b"def")
        raw, mock_locator = _patch_file_input(monkeypatch)
        monkeypatch.setattr(mcp_browser, "_LOCAL_UPLOAD_MAX_BYTES", 5)

        with skyvern_context.scoped(SkyvernContext(run_id=RUN_ID)):
            result = await mcp_browser.skyvern_file_upload(
                file_paths=[str(upload_a), str(upload_b)],
                selector="input[type=file]",
            )

        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
        raw.locator.assert_not_called()
        mock_locator.set_input_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_too_many_local_files(
        self,
        monkeypatch: pytest.MonkeyPatch,
        run_upload_dir: Path,
    ) -> None:
        upload_a = run_upload_dir / "a.txt"
        upload_b = run_upload_dir / "b.txt"
        upload_a.write_bytes(b"a")
        upload_b.write_bytes(b"b")
        raw, mock_locator = _patch_file_input(monkeypatch)
        monkeypatch.setattr(mcp_browser, "_LOCAL_UPLOAD_MAX_FILES", 1)

        with skyvern_context.scoped(SkyvernContext(run_id=RUN_ID)):
            result = await mcp_browser.skyvern_file_upload(
                file_paths=[str(upload_a), str(upload_b)],
                selector="input[type=file]",
            )

        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"
        raw.locator.assert_not_called()
        mock_locator.set_input_files.assert_not_awaited()

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
