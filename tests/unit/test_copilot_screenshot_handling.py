"""Tests for screenshot validation, enqueueing, consumption, and action-trace attachment."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _install_mock_database(monkeypatch: pytest.MonkeyPatch, mock_db: Any) -> None:
    """Replace `skyvern.forge.sdk.copilot.tools.app` with a stub whose
    DATABASE attribute is the provided mock.

    `app` is an AppHolder that raises RuntimeError on attribute access until
    start_forge_app() runs, so monkeypatching `app.DATABASE` directly fails at
    resolve-time in uninitialized test environments.
    """
    import skyvern.forge.sdk.copilot.tools as tools_module

    class _AppStub:
        DATABASE = mock_db

    monkeypatch.setattr(tools_module, "app", _AppStub())


class TestIsValidPngBase64:
    VALID_PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAAAElFTkSuQmCC"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )

    @staticmethod
    def _check(value: Any) -> bool:
        from skyvern.forge.sdk.copilot.output_utils import is_valid_image_base64

        return is_valid_image_base64(value)

    def test_valid_png_header(self) -> None:
        assert self._check(self.VALID_PNG_B64) is True

    def test_invalid_data(self) -> None:
        assert self._check("not-valid-base64-at-all!!!" + "x" * 100) is False

    def test_empty_string(self) -> None:
        assert self._check("") is False

    def test_none(self) -> None:
        assert self._check(None) is False

    def test_short_string(self) -> None:
        assert self._check("iVBOR") is False

    def test_jpeg_base64_accepted(self) -> None:
        import base64

        # Valid base64 with JFIF JPEG header — now accepted
        jpeg_header = b"\xff\xd8\xff\xe0" + b"\x00" * 80
        b64 = base64.b64encode(jpeg_header).decode()
        assert self._check(b64) is True

    def test_non_image_base64(self) -> None:
        import base64

        # Valid base64 but not PNG or JPEG
        gif_header = b"GIF89a" + b"\x00" * 80
        b64 = base64.b64encode(gif_header).decode()
        assert self._check(b64) is False


class TestEnqueueScreenshot:
    # Real 10x10 pixel PNG that Pillow can decode (>100 chars for validation threshold)
    VALID_PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAIAAAACUFjqAAAAE0lEQVR4nGP8z4APMOGVZRip0gBBLAETee26JgAAAABJRU5ErkJggg=="
    )

    def test_enqueues_valid_screenshot_when_vision(self) -> None:
        from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry, enqueue_screenshot_from_result

        ctx = MagicMock()
        ctx.supports_vision = True
        ctx.pending_screenshots = []
        enqueue_screenshot_from_result(ctx, {"ok": True, "data": {"screenshot_base64": self.VALID_PNG_B64}})
        assert len(ctx.pending_screenshots) == 1
        entry = ctx.pending_screenshots[0]
        assert isinstance(entry, ScreenshotEntry)
        assert entry.mime == "image/jpeg"

    def test_skips_when_no_vision(self) -> None:
        from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result

        ctx = MagicMock()
        ctx.supports_vision = False
        ctx.pending_screenshots = []
        enqueue_screenshot_from_result(ctx, {"ok": True, "data": {"screenshot_base64": self.VALID_PNG_B64}})
        assert len(ctx.pending_screenshots) == 0

    def test_skips_invalid_image(self) -> None:
        from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result

        ctx = MagicMock()
        ctx.supports_vision = True
        ctx.pending_screenshots = []
        enqueue_screenshot_from_result(ctx, {"ok": True, "data": {"screenshot_base64": "not-valid"}})
        assert len(ctx.pending_screenshots) == 0

    def test_skips_corrupt_header_valid_image(self) -> None:
        import base64

        from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result

        ctx = MagicMock()
        ctx.supports_vision = True
        ctx.pending_screenshots = []
        truncated_png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"broken-image-data").decode()
        enqueue_screenshot_from_result(ctx, {"ok": True, "data": {"screenshot_base64": truncated_png + "A" * 100}})
        assert len(ctx.pending_screenshots) == 0

    def test_second_enqueue_replaces_first_pending_entry(self) -> None:
        """Production caps the pending queue at 1 so the latest screenshot wins."""
        from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result

        ctx = MagicMock()
        ctx.supports_vision = True
        ctx.pending_screenshots = []

        enqueue_screenshot_from_result(ctx, {"ok": True, "data": {"screenshot_base64": self.VALID_PNG_B64}})
        first_entry = ctx.pending_screenshots[0]

        enqueue_screenshot_from_result(ctx, {"ok": True, "data": {"screenshot_base64": self.VALID_PNG_B64}})

        assert len(ctx.pending_screenshots) == 1
        assert ctx.pending_screenshots[0] is not first_entry


class TestConsumePendingScreenshots:
    def test_returns_none_when_empty(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _consume_pending_screenshots

        ctx = MagicMock()
        ctx.pending_screenshots = []
        assert _consume_pending_screenshots(ctx) is None

    def test_returns_user_message_with_image(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import SCREENSHOT_SENTINEL, _consume_pending_screenshots
        from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry

        entry = ScreenshotEntry(b64="dGVzdA==", mime="image/jpeg")
        ctx = MagicMock()
        ctx.pending_screenshots = [entry]
        msg = _consume_pending_screenshots(ctx)
        assert msg is not None
        assert msg["role"] == "user"
        content = msg["content"]
        assert len(content) == 2
        assert content[0]["type"] == "input_text"
        assert content[0]["text"].startswith(SCREENSHOT_SENTINEL)
        assert content[1]["type"] == "input_image"
        assert "image/jpeg" in content[1]["image_url"]
        assert content[1]["detail"] == "high"
        # Queue should be drained
        assert ctx.pending_screenshots == []

    def test_handles_multiple_screenshots(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _consume_pending_screenshots
        from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry

        entry1 = ScreenshotEntry(b64="abc=", mime="image/jpeg")
        entry2 = ScreenshotEntry(b64="def=", mime="image/jpeg")
        ctx = MagicMock()
        ctx.pending_screenshots = [entry1, entry2]
        msg = _consume_pending_screenshots(ctx)
        assert msg is not None
        # 1 text + 2 images
        assert len(msg["content"]) == 3
        assert ctx.pending_screenshots == []

    def test_returns_none_when_no_attr(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _consume_pending_screenshots

        ctx = MagicMock(spec=[])
        assert _consume_pending_screenshots(ctx) is None


class TestExtractScreenshotB64:
    @staticmethod
    def _extract(result: dict) -> Any:
        from skyvern.forge.sdk.copilot.output_utils import extract_screenshot_b64

        return extract_screenshot_b64(result)

    def test_extracts_from_data(self) -> None:
        assert self._extract({"data": {"screenshot_base64": "abc"}}) == "abc"

    def test_returns_none_when_no_data(self) -> None:
        assert self._extract({"ok": True}) is None

    def test_returns_none_when_data_not_dict(self) -> None:
        assert self._extract({"data": "string"}) is None

    def test_returns_none_when_no_screenshot_key(self) -> None:
        assert self._extract({"data": {"url": "https://example.com"}}) is None


class TestAttachActionTraces:
    @staticmethod
    def _make_block(task_id: str | None, status: str) -> MagicMock:
        block = MagicMock()
        block.task_id = task_id
        return block

    @staticmethod
    def _make_action(
        task_id: str, action_type: str, status: str, reasoning: str | None, element_id: str | None
    ) -> MagicMock:
        action = MagicMock()
        action.task_id = task_id
        action.action_type = action_type
        action.status = status
        action.reasoning = reasoning
        action.element_id = element_id
        return action

    @pytest.mark.asyncio
    async def test_attach_action_traces_failed_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.tools import _attach_action_traces

        block = self._make_block("task-1", "failed")
        result: dict[str, Any] = {"label": "step1", "status": "failed", "failure_reason": "max retries"}

        long_reasoning = "A" * 500
        actions = [
            self._make_action("task-1", "click", "failed", long_reasoning, "elem-42"),
            self._make_action("task-1", "input_text", "completed", "typed email", "elem-10"),
        ]

        mock_db = MagicMock()
        mock_db.tasks = MagicMock()
        mock_db.tasks.get_recent_actions_for_tasks = AsyncMock(return_value=actions)
        _install_mock_database(monkeypatch, mock_db)

        await _attach_action_traces([block], [result], "org-1")

        assert "action_trace" in result
        trace = result["action_trace"]
        assert len(trace) == 2
        assert trace[0]["action"] == "click"
        assert trace[0]["status"] == "failed"
        assert 0 < len(trace[0]["reasoning"]) < len(long_reasoning)
        assert trace[0]["reasoning"] == long_reasoning[: len(trace[0]["reasoning"])]
        assert trace[0]["element"] == "elem-42"
        assert trace[1]["reasoning"] == "typed email"

    @pytest.mark.asyncio
    async def test_attach_action_traces_skips_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.tools import _attach_action_traces

        block = self._make_block("task-1", "completed")
        result: dict[str, Any] = {"label": "step1", "status": "completed"}

        mock_db = MagicMock()
        mock_db.tasks = MagicMock()
        mock_db.tasks.get_recent_actions_for_tasks = AsyncMock(return_value=[])
        _install_mock_database(monkeypatch, mock_db)

        await _attach_action_traces([block], [result], "org-1")

        assert "action_trace" not in result
        mock_db.tasks.get_recent_actions_for_tasks.assert_not_called()

    @pytest.mark.asyncio
    async def test_attach_action_traces_no_task_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.tools import _attach_action_traces

        block = self._make_block(None, "failed")
        result: dict[str, Any] = {"label": "step1", "status": "failed"}

        mock_db = MagicMock()
        mock_db.tasks = MagicMock()
        mock_db.tasks.get_recent_actions_for_tasks = AsyncMock(return_value=[])
        _install_mock_database(monkeypatch, mock_db)

        await _attach_action_traces([block], [result], "org-1")

        assert "action_trace" not in result
        mock_db.tasks.get_recent_actions_for_tasks.assert_not_called()

    @pytest.mark.asyncio
    async def test_attach_action_traces_includes_all_failure_statuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.tools import _FAILED_BLOCK_STATUSES, _attach_action_traces

        blocks = []
        results: list[dict[str, Any]] = []
        for i, status in enumerate(sorted(_FAILED_BLOCK_STATUSES)):
            blocks.append(self._make_block(f"task-{i}", status))
            results.append({"label": f"step{i}", "status": status})

        actions = [self._make_action(f"task-{i}", "click", "failed", None, None) for i in range(len(blocks))]

        mock_db = MagicMock()
        mock_db.tasks = MagicMock()
        mock_db.tasks.get_recent_actions_for_tasks = AsyncMock(return_value=actions)
        _install_mock_database(monkeypatch, mock_db)

        await _attach_action_traces(blocks, results, "org-1")

        for r in results:
            assert "action_trace" in r, f"Missing action_trace for status={r['status']}"


class TestSyntheticScreenshotPlaceholders:
    def test_placeholder_counts_as_synthetic_user_message(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import SCREENSHOT_PLACEHOLDER, is_synthetic_user_message

        assert is_synthetic_user_message({"role": "user", "content": SCREENSHOT_PLACEHOLDER}) is True

    def test_real_user_boundary_ignores_screenshot_placeholders(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import SCREENSHOT_PLACEHOLDER
        from skyvern.forge.sdk.copilot.session_factory import _find_real_user_boundary

        items = [
            {"role": "user", "content": "original user request"},
            {"role": "assistant", "content": "assistant reply"},
            {"role": "user", "content": SCREENSHOT_PLACEHOLDER},
            {"role": "assistant", "content": "more assistant output"},
            {"role": "user", "content": "latest real user request"},
        ]

        assert _find_real_user_boundary(items, recent_turns=2) == 0

    def test_real_user_boundary_with_more_real_turns_than_requested(self) -> None:
        """Common production shape: more real user turns than ``recent_turns``.

        Helper walks backward and returns the index of the N-th-most-recent
        real user turn. In this list there are 3 real users and we ask for
        the last 2 — answer is the index of the 2nd-most-recent real user
        message (index 4, the 'second real user turn').
        """
        from skyvern.forge.sdk.copilot.session_factory import _find_real_user_boundary

        items = [
            {"role": "user", "content": "first real user turn"},
            {"role": "assistant", "content": "reply 1"},
            {"role": "user", "content": "second real user turn"},
            {"role": "assistant", "content": "reply 2"},
            {"role": "user", "content": "third real user turn"},
            {"role": "assistant", "content": "reply 3"},
        ]

        assert _find_real_user_boundary(items, recent_turns=2) == 2
