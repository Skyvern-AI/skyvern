"""Tests for screenshot validation, enqueueing, consumption, and action-trace attachment."""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _install_mock_database(monkeypatch: pytest.MonkeyPatch, mock_db: Any) -> None:
    """Replace `skyvern.forge.sdk.copilot.tools.run_execution.app` with a stub whose
    DATABASE attribute is the provided mock.

    `app` is an AppHolder that raises RuntimeError on attribute access until
    start_forge_app() runs, so monkeypatching `app.DATABASE` directly fails at
    resolve-time in uninitialized test environments.
    """
    import skyvern.forge.sdk.copilot.tools.run_execution as run_execution_module

    class _AppStub:
        DATABASE = mock_db

    monkeypatch.setattr(run_execution_module, "app", _AppStub())


class TestIsValidPngBase64:
    VALID_PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAAAElFTkSuQmCC"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )

    JPEG_B64 = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 80).decode()
    GIF_B64 = base64.b64encode(b"GIF89a" + b"\x00" * 80).decode()

    @staticmethod
    def _check(value: Any) -> bool:
        from skyvern.forge.sdk.copilot.output_utils import is_valid_image_base64

        return is_valid_image_base64(value)

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param(VALID_PNG_B64, True, id="png_header"),
            pytest.param(JPEG_B64, True, id="jpeg_header"),
            pytest.param(GIF_B64, False, id="gif_rejected"),
            pytest.param("", False, id="empty"),
            pytest.param(None, False, id="none"),
            pytest.param("iVBOR", False, id="short"),
            pytest.param("not-valid-base64-at-all!!!" + "x" * 100, False, id="garbage"),
        ],
    )
    def test_is_valid_image_base64(self, payload: Any, expected: bool) -> None:
        assert self._check(payload) is expected


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

    @pytest.mark.parametrize(
        ("result_dict", "expected"),
        [
            pytest.param({"data": {"screenshot_base64": "abc"}}, "abc", id="present"),
            pytest.param({"ok": True}, None, id="no_data"),
            pytest.param({"data": "string"}, None, id="data_not_dict"),
            pytest.param({"data": {"url": "https://example.com"}}, None, id="no_screenshot_key"),
        ],
    )
    def test_extract_screenshot_b64(self, result_dict: dict, expected: Any) -> None:
        assert self._extract(result_dict) == expected


class TestAttachActionTraces:
    @staticmethod
    def _make_block(task_id: str | None, status: str) -> MagicMock:
        block = MagicMock()
        block.task_id = task_id
        return block

    @staticmethod
    def _make_action(
        task_id: str,
        action_type: str,
        status: str,
        reasoning: str | None,
        element_id: str | None,
        *,
        description: str | None = None,
        output: dict[str, Any] | list | str | None = None,
    ) -> MagicMock:
        action = MagicMock()
        action.task_id = task_id
        action.action_type = action_type
        action.status = status
        action.reasoning = reasoning
        action.element_id = element_id
        action.description = description
        action.output = output
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
    async def test_attach_action_traces_projects_only_valid_code_failure_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.forge.sdk.copilot.tools import _attach_action_traces

        block = self._make_block("task-1", "failed")
        result: dict[str, Any] = {"label": "step1", "status": "failed"}
        actions = [
            self._make_action(
                "task-1",
                "null_action",
                "failed",
                None,
                None,
                description="code error at line 18",
                output={"code_line": 18, "arbitrary": "must-not-survive"},
            ),
            self._make_action(
                "task-1",
                "click",
                "failed",
                None,
                "button-1",
                description="ordinary action description must not survive",
                output={"code_line": 17},
            ),
            self._make_action(
                "task-1",
                "click",
                "failed",
                None,
                "button-2",
                description="bool is not an integer line",
                output={"code_line": True},
            ),
            self._make_action(
                "task-1",
                "input_text",
                "failed",
                None,
                "input-1",
                description="string is not an integer line",
                output={"code_line": "7"},
            ),
            self._make_action(
                "task-1",
                "input_text",
                "failed",
                None,
                "input-2",
                description="non-dict output",
                output=[{"code_line": 7}],
            ),
        ]

        mock_db = MagicMock()
        mock_db.tasks = MagicMock()
        mock_db.tasks.get_recent_actions_for_tasks = AsyncMock(return_value=actions)
        _install_mock_database(monkeypatch, mock_db)

        await _attach_action_traces([block], [result], "org-1")

        trace = result["action_trace"]
        assert [entry["action"] for entry in trace] == [
            "null_action",
            "click",
            "click",
            "input_text",
            "input_text",
        ]
        assert trace[0]["description"] == "code error at line 18"
        assert trace[0]["code_line"] == 18
        assert "output" not in trace[0]
        assert "arbitrary" not in trace[0]
        assert "description" not in trace[1]
        assert "code_line" not in trace[1]
        assert "description" not in trace[2]
        assert "code_line" not in trace[2]
        assert "description" not in trace[3]
        assert "code_line" not in trace[3]
        assert "description" not in trace[4]
        assert "code_line" not in trace[4]

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


class TestSummarizeActionTrace:
    def test_selects_six_newest_entries_then_renders_them_chronologically(self) -> None:
        from skyvern.forge.sdk.copilot.tools.run_execution import _summarize_action_trace

        newest_first = [
            {"action": "click", "status": "completed", "element": f"newest-{index}"} for index in range(8, 0, -1)
        ]

        summary = _summarize_action_trace(newest_first)

        assert summary == [
            "click newest-3 completed",
            "click newest-4 completed",
            "click newest-5 completed",
            "click newest-6 completed",
            "click newest-7 completed",
            "click newest-8 completed",
        ]

    def test_retains_projected_failure_description_and_code_line(self) -> None:
        from skyvern.forge.sdk.copilot.tools.run_execution import _summarize_action_trace

        summary = _summarize_action_trace(
            [
                {
                    "action": "goto_url",
                    "status": "failed",
                    "element": None,
                    "description": "code error at line 9",
                    "code_line": 9,
                }
            ]
        )

        assert summary == ["goto_url failed description=code error at line 9 code_line=9"]


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


class TestAttachFailedBlockScreenshots:
    """A failed CODE block must surface its at-failure page, not just exception text (SKY-13250)."""

    PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    @staticmethod
    def _make_block(
        *,
        workflow_run_block_id: str | None,
        task_id: str | None,
        final_url: str | None = None,
    ) -> MagicMock:
        block = MagicMock()
        block.workflow_run_block_id = workflow_run_block_id
        block.task_id = task_id
        block.final_url = final_url
        return block

    def _install_app(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        run_block_artifact: object | None,
        task_v2_artifacts: list[object],
    ) -> None:
        import skyvern.forge.sdk.copilot.tools.run_execution as run_execution_module

        artifacts = MagicMock()
        artifacts.get_artifact_by_entity_id = AsyncMock(return_value=run_block_artifact)
        artifacts.get_artifacts_for_task_v2 = AsyncMock(return_value=task_v2_artifacts)

        class _AppStub:
            DATABASE = MagicMock(artifacts=artifacts)
            ARTIFACT_MANAGER = MagicMock(retrieve_artifact=AsyncMock(return_value=self.PNG_BYTES))

        monkeypatch.setattr(run_execution_module, "app", _AppStub())

    @pytest.mark.asyncio
    async def test_failed_code_block_carries_screenshot_and_final_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The regression: a code block's screenshot lives on the workflow_run_block.

        Its container task is a task v1, so the task_v2 lookup returns nothing — before this
        fix the failed block's result carried neither the screenshot nor the URL.
        """
        from skyvern.forge.sdk.copilot.tools.run_execution import _attach_failed_block_screenshots

        block = self._make_block(
            workflow_run_block_id="wrb-1",
            task_id="tsk-v1-container",
            final_url="https://auth.example.com/otp",
        )
        result: dict[str, Any] = {"label": "login", "status": "failed", "failure_reason": "Timeout 30000ms"}

        self._install_app(monkeypatch, run_block_artifact=MagicMock(), task_v2_artifacts=[])

        await _attach_failed_block_screenshots([block], [result], "org-1")

        assert result["screenshot_b64"] == base64.b64encode(self.PNG_BYTES).decode("utf-8")
        assert result["final_url"] == "https://auth.example.com/otp"

    @pytest.mark.asyncio
    async def test_legacy_task_v2_lookup_still_resolves_when_run_block_has_no_artifact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.forge.sdk.copilot.tools.run_execution import _attach_failed_block_screenshots

        block = self._make_block(workflow_run_block_id="wrb-2", task_id="tsk-v2")
        result: dict[str, Any] = {"label": "extract", "status": "failed"}

        self._install_app(monkeypatch, run_block_artifact=None, task_v2_artifacts=[MagicMock()])

        await _attach_failed_block_screenshots([block], [result], "org-1")

        assert result["screenshot_b64"] == base64.b64encode(self.PNG_BYTES).decode("utf-8")
        assert "final_url" not in result

    @pytest.mark.asyncio
    async def test_successful_block_gets_no_evidence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.tools.run_execution import _attach_failed_block_screenshots

        block = self._make_block(
            workflow_run_block_id="wrb-3",
            task_id="tsk-3",
            final_url="https://example.com/done",
        )
        result: dict[str, Any] = {"label": "login", "status": "completed"}

        self._install_app(monkeypatch, run_block_artifact=MagicMock(), task_v2_artifacts=[MagicMock()])

        await _attach_failed_block_screenshots([block], [result], "org-1")

        assert "screenshot_b64" not in result
        assert "final_url" not in result


class TestRunScreenshotResolution:
    """Only data.screenshot_base64 becomes a model-visible image, so choosing it correctly
    decides what the repair loop actually sees (SKY-13250)."""

    VALID_PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAIAAAACUFjqAAAAE0lEQVR4nGP8z4APMOGVZRip0gBBLAETee26JgAAAABJRU5ErkJggg=="
    )

    @staticmethod
    def _resolve(live_capture: str | None, results: list[dict[str, Any]], run_ok: bool) -> str | None:
        from skyvern.forge.sdk.copilot.tools.run_execution import _resolve_run_screenshot_b64

        return _resolve_run_screenshot_b64(live_capture=live_capture, results=results, run_ok=run_ok)

    def test_live_capture_wins_when_present(self) -> None:
        results = [{"status": "failed", "screenshot_b64": "block-b64"}]
        assert self._resolve("live-b64", results, run_ok=False) == "live-b64"

    def test_failed_run_promotes_first_failed_block(self) -> None:
        """The dispatched path: no live capture, so the block's at-failure shot is all there is."""
        results = [
            {"label": "start", "status": "completed"},
            {"label": "login", "status": "failed", "screenshot_b64": "first-b64"},
            {"label": "extract", "status": "failed", "screenshot_b64": "second-b64"},
        ]
        assert self._resolve(None, results, run_ok=False) == "first-b64"

    def test_successful_run_never_promotes_a_failure_screenshot(self) -> None:
        """A healed or continue_on_failure block leaves a failure screenshot behind on a run
        that succeeded; showing it would put a stale, contradicted page in front of the model."""
        results = [
            {"label": "login", "status": "failed", "screenshot_b64": "healed-failure-b64"},
            {"label": "extract", "status": "completed"},
        ]
        assert self._resolve(None, results, run_ok=True) is None

    def test_failed_run_with_no_block_screenshot_resolves_none(self) -> None:
        assert self._resolve(None, [{"label": "login", "status": "failed"}], run_ok=False) is None

    def test_promoted_screenshot_reaches_the_image_enqueue_path(self) -> None:
        """End of the chain: the promoted bytes must arrive as a pending image, not as text."""
        from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result

        results = [{"label": "login", "status": "failed", "screenshot_b64": self.VALID_PNG_B64}]
        promoted = self._resolve(None, results, run_ok=False)
        result = {"ok": False, "data": {"screenshot_base64": promoted}}

        ctx = MagicMock()
        ctx.supports_vision = True
        ctx.pending_screenshots = []
        enqueue_screenshot_from_result(ctx, result)

        assert len(ctx.pending_screenshots) == 1


class TestSanitizerStripsPerBlockScreenshots:
    """Raw base64 in the text channel crowds out the fields beside it. The image reaches the
    model through data.screenshot_base64, so the per-block copy is stripped on every tool that
    carries failed blocks — including the primary repair tool (SKY-13250)."""

    @staticmethod
    def _sanitize(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

        return sanitize_tool_result_for_llm(tool_name, result)

    @pytest.mark.parametrize("tool_name", ["run_blocks_and_collect_debug", "get_run_results"])
    def test_strips_base64_but_keeps_final_url(self, tool_name: str) -> None:
        from skyvern.forge.sdk.copilot.output_utils import _BASE64_IMAGE_OMITTED_MESSAGE

        result = {
            "ok": False,
            "data": {
                "blocks": [
                    {
                        "label": "login",
                        "status": "failed",
                        "screenshot_b64": "A" * 5000,
                        "final_url": "https://portal.example.com/mfa",
                    }
                ]
            },
        }

        block = self._sanitize(tool_name, result)["data"]["blocks"][0]

        assert block["screenshot_b64"] == _BASE64_IMAGE_OMITTED_MESSAGE
        # final_url is the only URL evidence on the dispatched path; it must survive.
        assert block["final_url"] == "https://portal.example.com/mfa"

    def test_leaves_blocks_without_screenshots_untouched(self) -> None:
        result = {"ok": True, "data": {"blocks": [{"label": "login", "status": "completed"}]}}
        assert self._sanitize("run_blocks_and_collect_debug", result)["data"]["blocks"][0] == {
            "label": "login",
            "status": "completed",
        }
