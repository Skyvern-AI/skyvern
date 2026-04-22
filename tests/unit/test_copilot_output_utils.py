"""Tests for truncate_output and sanitize_tool_result_for_llm."""

from __future__ import annotations

from unittest.mock import MagicMock

from skyvern.forge.sdk.copilot.output_utils import (
    sanitize_tool_result_for_llm,
    summarize_tool_result,
    truncate_output,
)


def test_truncate_output_none() -> None:
    assert truncate_output(None) is None


def test_truncate_output_short_string() -> None:
    assert truncate_output("ok") == "ok"


def test_truncate_output_long_string_truncates() -> None:
    text = "x" * 2100
    result = truncate_output(text, max_chars=2000)

    assert result is not None
    assert result.startswith("x" * 2000)
    assert result.endswith("\n... [truncated]")


def test_truncate_output_serializes_dict() -> None:
    result = truncate_output({"a": 1, "b": True})
    assert result == '{"a": 1, "b": true}'


def test_truncate_output_falls_back_to_str_on_json_error() -> None:
    circular: dict[str, object] = {}
    circular["self"] = circular

    result = truncate_output(circular)
    assert result is not None
    assert "self" in result


def test_sanitize_get_run_results_scrubs_nested_block_screenshots() -> None:
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_123",
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "open_page",
                    "status": "completed",
                    "screenshot_b64": "iVBORw0KGgoAAAANSUhEUgAAA" + "A" * 500,
                },
                {
                    "label": "extract_data",
                    "status": "failed",
                    "failure_reason": "timeout",
                    "screenshot_b64": "iVBORw0KGgo" + "B" * 800,
                },
            ],
        },
    }

    sanitized = sanitize_tool_result_for_llm("get_run_results", result)
    blocks = sanitized["data"]["blocks"]

    assert blocks[0]["screenshot_b64"] == "[base64 image omitted — screenshot was taken successfully]"
    assert blocks[1]["screenshot_b64"] == "[base64 image omitted — screenshot was taken successfully]"
    assert blocks[1]["failure_reason"] == "timeout"
    assert blocks[0]["status"] == "completed"


def test_sanitize_does_not_mutate_original_blocks() -> None:
    original_screenshot = "iVBORw0KGgo" + "B" * 500
    result = {
        "ok": True,
        "data": {
            "blocks": [{"label": "extract", "screenshot_b64": original_screenshot}],
        },
    }
    original_block = result["data"]["blocks"][0]

    sanitized = sanitize_tool_result_for_llm("get_run_results", result)

    assert original_block["screenshot_b64"] == original_screenshot
    assert sanitized["data"]["blocks"][0]["screenshot_b64"].startswith("[base64 image omitted")
    assert sanitized["data"]["blocks"][0] is not original_block


def test_sanitize_run_blocks_debug_does_not_mutate_extracted_data() -> None:
    original_extracted = [{"price": 19.99, "name": "widget"}]
    result = {
        "ok": True,
        "data": {
            "blocks": [{"label": "extract", "extracted_data": original_extracted}],
        },
    }
    original_block = result["data"]["blocks"][0]

    sanitize_tool_result_for_llm("run_blocks_and_collect_debug", result)

    assert original_block["extracted_data"] is original_extracted


def test_sanitize_other_tools_do_not_touch_block_screenshot_b64() -> None:
    # `run_blocks_and_collect_debug` does not attach nested `screenshot_b64`;
    # if one somehow shows up there, leave it alone so behavior is scoped.
    result = {
        "ok": True,
        "data": {
            "overall_status": "completed",
            "blocks": [
                {
                    "label": "a",
                    "status": "completed",
                    "screenshot_b64": "stays_here",
                }
            ],
        },
    }
    sanitized = sanitize_tool_result_for_llm("run_blocks_and_collect_debug", result)
    assert sanitized["data"]["blocks"][0]["screenshot_b64"] == "stays_here"


class TestSanitization:
    def test_screenshot_sanitization(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

        result = {
            "ok": True,
            "data": {
                "screenshot_base64": "iVBOR...",
                "url": "https://example.com",
            },
        }
        sanitized = sanitize_tool_result_for_llm("get_browser_screenshot", result)
        expected = "[base64 image omitted — screenshot was taken successfully]"
        assert sanitized["data"]["screenshot_base64"] == expected
        assert sanitized["data"]["url"] == "https://example.com"

    def test_mcp_fields_stripped(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

        result = {
            "ok": True,
            "action": "skyvern_navigate",
            "browser_context": {"mode": "cloud_session"},
            "timing_ms": {"total": 500},
            "artifacts": [],
            "data": {
                "url": "https://example.com",
                "sdk_equivalent": "await page.goto(...)",
            },
        }
        sanitized = sanitize_tool_result_for_llm("navigate_browser", result)
        assert "action" not in sanitized
        assert "browser_context" not in sanitized
        assert "timing_ms" not in sanitized
        assert "artifacts" not in sanitized
        assert "sdk_equivalent" not in sanitized.get("data", {})

    def test_workflow_key_stripped(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

        result = {
            "ok": True,
            "data": {"block_count": 2},
            "_workflow": MagicMock(),
        }
        sanitized = sanitize_tool_result_for_llm("update_workflow", result)
        assert "_workflow" not in sanitized

    def test_large_schema_truncated(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

        big_schema = {f"field_{i}": {"type": "string"} for i in range(200)}
        result = {
            "ok": True,
            "data": {"schema": big_schema},
        }
        sanitized = sanitize_tool_result_for_llm("get_block_schema", result)
        assert sanitized["data"]["schema"]["_truncated"] is True

    def test_run_blocks_sanitizer_preserves_compact_packet_fields(self) -> None:
        from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

        # visible_elements_html is no longer in the default run-blocks payload
        # (it moved to the heavier get_run_results / direct browser path). The
        # sanitizer should leave the compact packet fields intact.
        result = {
            "ok": False,
            "data": {
                "workflow_run_id": "wr_1",
                "overall_status": "failed",
                "requested_block_labels": ["a", "b"],
                "executed_block_labels": ["b"],
                "frontier_start_label": "b",
                "current_url": "https://example.test",
                "page_title": "Example",
                "action_trace_summary": ["click #submit failed"],
                "blocks": [{"label": "b", "block_type": "EXTRACTION", "status": "failed"}],
            },
        }
        sanitized = sanitize_tool_result_for_llm("run_blocks_and_collect_debug", result)
        data = sanitized["data"]
        assert "visible_elements_html" not in data
        assert data["requested_block_labels"] == ["a", "b"]
        assert data["executed_block_labels"] == ["b"]
        assert data["frontier_start_label"] == "b"
        assert data["action_trace_summary"] == ["click #submit failed"]
        assert data["current_url"] == "https://example.test"


class TestSummarizeToolResult:
    @staticmethod
    def _summarize(tool_name: str, result: dict) -> str:
        return summarize_tool_result(tool_name, result)

    def test_error_result(self) -> None:
        summary = self._summarize("any_tool", {"ok": False, "error": "oops"})
        assert "Failed" in summary
        assert "oops" in summary

    def test_failed_run_surfaces_block_failure_reason_when_error_absent(self) -> None:
        summary = self._summarize(
            "run_blocks_and_collect_debug",
            {
                "ok": False,
                "data": {
                    "overall_status": "failed",
                    "blocks": [
                        {
                            "label": "navigate",
                            "status": "failed",
                            "failure_reason": (
                                "Failed to navigate to url https://example.invalid. "
                                "Error message: net::ERR_NAME_NOT_RESOLVED"
                            ),
                        }
                    ],
                },
            },
        )
        assert "ERR_NAME_NOT_RESOLVED" in summary
        assert "Unknown error" not in summary

    def test_failed_run_prefers_top_level_error_over_nested(self) -> None:
        summary = self._summarize(
            "run_blocks_and_collect_debug",
            {
                "ok": False,
                "error": "top-level message",
                "data": {"blocks": [{"failure_reason": "nested message"}]},
            },
        )
        assert "top-level message" in summary
        assert "nested message" not in summary

    def test_failed_run_prefers_data_failure_reason_over_block_failure_reason(self) -> None:
        summary = self._summarize(
            "run_blocks_and_collect_debug",
            {
                "ok": False,
                "data": {
                    "failure_reason": "run-level",
                    "blocks": [{"failure_reason": "block-level"}],
                },
            },
        )
        assert "run-level" in summary
        assert "block-level" not in summary

    def test_failed_run_falls_back_to_unknown_error_when_nothing_present(self) -> None:
        summary = self._summarize(
            "run_blocks_and_collect_debug",
            {"ok": False, "data": {"blocks": []}},
        )
        assert "Unknown error" in summary

    def test_update_workflow(self) -> None:
        summary = self._summarize(
            "update_workflow",
            {
                "ok": True,
                "data": {"block_count": 3},
            },
        )
        assert "3" in summary

    def test_navigate_browser(self) -> None:
        summary = self._summarize(
            "navigate_browser",
            {
                "ok": True,
                "url": "https://example.com",
            },
        )
        assert summary == "Navigated to https://example.com"

    def test_type_text_typed_length(self) -> None:
        summary = self._summarize(
            "type_text",
            {
                "ok": True,
                "data": {"selector": "#email", "typed_length": 10},
            },
        )
        assert "10" in summary

    def test_type_text_text_length(self) -> None:
        summary = self._summarize(
            "type_text",
            {
                "ok": True,
                "data": {"selector": "#email", "text_length": 20},
            },
        )
        assert "20" in summary

    def test_unknown_tool_returns_ok(self) -> None:
        summary = self._summarize("unknown_tool", {"ok": True})
        assert summary == "OK"
