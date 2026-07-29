"""Tests for truncate_output and sanitize_tool_result_for_llm."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.output_utils import (
    _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
    _sanitize_failure_text,
    build_run_blocks_response,
    format_tool_result_for_user,
    looks_like_workflow_yaml_in_chat,
    parse_final_response,
    sanitize_tool_result_for_llm,
    summarize_tool_result,
    summarize_tool_result_detail,
    truncate_output,
    user_facing_success,
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


def test_sanitize_strips_internal_watchdog_cancel_marker() -> None:
    result = {
        "ok": False,
        "error": "Run ID: wr_timeout. Outcome is uncertain.",
        _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY: True,
    }

    sanitized = sanitize_tool_result_for_llm("run_blocks_and_collect_debug", result)

    assert _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY not in sanitized
    assert sanitized["error"] == "Run ID: wr_timeout. Outcome is uncertain."


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

    def test_update_and_run_blocks_with_scalar_data_does_not_crash(self) -> None:
        summary = self._summarize(
            "update_and_run_blocks",
            {
                "ok": True,
                "data": "workflow_run_skipped: verified_goal_already_satisfied",
            },
        )
        assert summary == "OK"

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

    def test_update_and_run_blocks_success_reports_run_status(self) -> None:
        # The non-skip result is run-blocks-shaped (overall_status, executed_block_labels);
        # it never carries block_count, so the summary must not fabricate a count.
        summary = self._summarize(
            "update_and_run_blocks",
            {"ok": True, "data": {"overall_status": "completed", "executed_block_labels": ["step_1"]}},
        )
        assert summary == "Updated the workflow and ran it: completed"

    def test_update_and_run_blocks_success_without_status(self) -> None:
        summary = self._summarize(
            "update_and_run_blocks",
            {"ok": True, "data": {"executed_block_labels": ["step_1"]}},
        )
        assert summary == "Updated the workflow and ran it"

    def test_update_and_run_blocks_skipped_run_still_reported(self) -> None:
        summary = self._summarize(
            "update_and_run_blocks",
            {"ok": True, "data": {"block_count": 3, "skipped_run": True}},
        )
        assert summary == "Workflow updated (3 blocks); browser run skipped"

    def test_discover_workflow_entrypoint_found(self) -> None:
        summary = self._summarize(
            "discover_workflow_entrypoint",
            {"ok": True, "data": {"candidate_url": "https://example.com/apply"}},
        )
        assert summary == "Found the entry page: https://example.com/apply"

    def test_discover_workflow_entrypoint_not_found(self) -> None:
        summary = self._summarize(
            "discover_workflow_entrypoint",
            {"ok": True, "data": {"candidate_url": None, "failure_reason": "no_candidate"}},
        )
        assert summary == "No entry page found"

    def test_inspect_page_for_composition_reports_field_count(self) -> None:
        summary = self._summarize(
            "inspect_page_for_composition",
            {"ok": True, "data": {"forms": [{"fields": [{}, {}]}, {"fields": [{}]}]}},
        )
        assert summary == "Inspected the page (3 form field(s))"

    def test_inspect_page_for_composition_no_forms(self) -> None:
        summary = self._summarize(
            "inspect_page_for_composition",
            {"ok": True, "data": {"forms": []}},
        )
        assert summary == "Inspected the page"

    def test_evaluate_does_not_dump_raw_list(self) -> None:
        # The activity bullet must describe shape only — JS return values
        # (which are page-controlled) must never reach the SSE payload.
        summary = self._summarize(
            "evaluate",
            {
                "ok": True,
                "data": {
                    "result": [
                        {"text": "Tickets", "href": "https://example.com/tickets/"},
                        {"text": "Hospitality", "href": "https://example.com/hospitality/"},
                    ]
                },
            },
        )
        assert "Tickets" not in summary
        assert "Hospitality" not in summary
        assert "example.com" not in summary
        assert "list" in summary
        assert "2" in summary

    def test_evaluate_dict_returns_structural_summary(self) -> None:
        summary = self._summarize(
            "evaluate",
            {
                "ok": True,
                "data": {"result": {"title": "Official Site", "url": "https://example.com/"}},
            },
        )
        assert "Official Site" not in summary
        assert "example.com" not in summary
        assert "title" in summary  # key names describe shape, not values
        assert "url" in summary

    def test_evaluate_none_returns_plain_label(self) -> None:
        summary = self._summarize(
            "evaluate",
            {"ok": True, "data": {"result": None}},
        )
        assert summary == "Evaluated JavaScript"

    def test_failure_strips_http_headers_blob(self) -> None:
        # Failure summaries must never embed an HTTP-response-headers dict.
        summary = self._summarize(
            "click",
            {
                "ok": False,
                "error": (
                    "headers: {'date': 'Mon, 27 Apr 2026 05:03:27 GMT', "
                    "'content-type': 'application/json', 'content-length': '43', "
                    "'connection': 'keep-alive'}"
                ),
            },
        )
        assert "'date'" not in summary
        assert "'content-type'" not in summary
        assert "keep-alive" not in summary
        assert summary.startswith("Failed:")
        assert len(summary) <= 128  # "Failed: " + ≤120 sanitized body

    def test_failure_caps_at_120_chars(self) -> None:
        long_message = "An unexpected error happened while doing the thing. " * 10
        assert len(long_message) > 200
        summary = self._summarize(
            "click",
            {"ok": False, "error": long_message},
        )
        body = summary[len("Failed: ") :]
        assert len(body) <= 120

    def test_screenshot_without_url_no_empty_parens(self) -> None:
        summary = self._summarize(
            "get_browser_screenshot",
            {"ok": True, "data": {}},
        )
        assert summary == "Screenshot taken"


class TestFormatToolResultForUser:
    @staticmethod
    def _format(tool_name: str, result: dict) -> str:
        return format_tool_result_for_user(tool_name, result)

    def test_blocker_signal_overrides_activity_summary_and_detail(self) -> None:
        from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal

        signal = CopilotToolBlockerSignal(
            blocker_kind="tool_error",
            agent_steering_text=(
                "Less than 90 seconds remain in this Copilot turn. "
                "Do NOT start another block-running tool call; reply from gathered progress."
            ),
            user_facing_reason="I'm running out of time on this turn. I'll wrap up with what I have so far.",
            recovery_hint="stop",
            renders_final_reply=False,
            internal_reason_code="tool_error_late_block_running",
            blocked_tool="update_and_run_blocks",
        )
        result = {"ok": False, "error": signal.agent_steering_text}

        summary = format_tool_result_for_user("update_and_run_blocks", result, blocker_signal=signal)
        detail = summarize_tool_result_detail(result, blocker_signal=signal)

        assert summary == signal.user_facing_reason
        assert detail == signal.user_facing_reason
        assert "Do NOT" not in summary
        assert "Do NOT" not in detail
        assert "update_and_run_blocks" not in summary
        assert "tool_error_late_block_running" not in summary
        agent_summary = summarize_tool_result("update_and_run_blocks", result)
        assert "Do NOT start another block-running tool call" in agent_summary

    def test_blocker_signal_does_not_reverse_match_unrelated_short_error(self) -> None:
        from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal

        signal = CopilotToolBlockerSignal(
            blocker_kind="tool_error",
            agent_steering_text="A long, specific blocker for an unrelated timeout.",
            user_facing_reason="A specific timeout summary.",
            recovery_hint="stop",
            internal_reason_code="tool_error_specific_timeout",
            blocked_tool="update_and_run_blocks",
        )
        result = {"ok": False, "error": "timeout"}

        summary = format_tool_result_for_user("update_and_run_blocks", result, blocker_signal=signal)

        assert summary != signal.user_facing_reason
        assert summary == "Failed: timeout"

    def test_active_terminal_blocker_matches_structured_failure_category(self) -> None:
        from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
        from skyvern.forge.sdk.copilot.failure_tracking import (
            ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY,
            ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE,
        )

        signal = CopilotToolBlockerSignal(
            blocker_kind="tool_error",
            agent_steering_text="The prior active workflow run emitted typed terminal evidence.",
            user_facing_reason="I reached the requested browser state, but the workflow still needs review.",
            recovery_hint="report_blocker_to_user",
            internal_reason_code=ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE,
            blocked_tool="update_and_run_blocks",
        )
        result = {
            "ok": False,
            "error": "The active run reached the requested browser state.",
            "data": {
                "failure_categories": [
                    {"category": ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY, "confidence_float": 1.0}
                ]
            },
        }

        summary = format_tool_result_for_user("update_and_run_blocks", result, blocker_signal=signal)
        detail = summarize_tool_result_detail(result, blocker_signal=signal)

        assert summary == signal.user_facing_reason
        assert detail == signal.user_facing_reason

    def test_watchdog_control_signal_summary_overrides_raw_detail(self) -> None:
        result = {
            "ok": False,
            "error": (
                "The run has not made progress. Run ID: wr_stalled. Outcome is uncertain. "
                "Do NOT re-invoke block-running tools without first calling get_run_results."
            ),
            "data": {
                "failure_reason": (
                    "The run stopped after no observable progress for 120s. Run ID: wr_stalled. Outcome is uncertain."
                ),
                "control_signal": {
                    "kind": "watchdog_stagnation",
                    "user_facing_summary": "The run stopped after no observable progress for 120s.",
                },
                "user_facing_summary": "The run stopped after no observable progress for 120s.",
            },
        }

        summary = self._format("run_blocks_and_collect_debug", result)
        detail = summarize_tool_result_detail(result, tool_name="run_blocks_and_collect_debug")

        assert summary == "The run stopped after no observable progress for 120s."
        assert detail == summary
        assert "wr_stalled" not in summary
        assert "get_run_results" not in detail
        assert "Do NOT" not in detail

    def test_unsafe_structured_summary_falls_back_for_summary_and_detail(self) -> None:
        result = {
            "ok": False,
            "error": "STOP - do NOT respond to the user yet.",
            "data": {
                "user_facing_summary": "The update_and_run_blocks tool could not continue.",
            },
        }

        summary = self._format("update_and_run_blocks", result)
        detail = summarize_tool_result_detail(result, tool_name="update_and_run_blocks")

        assert summary == "Couldn't complete that step."
        assert detail == "Couldn't complete that step."
        assert "update_and_run_blocks" not in summary
        assert "STOP" not in detail

    def test_loop_detected_failure_drops_use_a_different_tool_tail(self) -> None:
        summary = self._format(
            "click",
            {
                "ok": False,
                "error": (
                    "LOOP DETECTED: 'click' has been called 3 times consecutively. "
                    "This tool will not run again. Use a DIFFERENT tool to continue."
                ),
            },
        )
        assert summary == "The agent got stuck retrying the same step — moving on."
        assert "DIFFERENT tool" not in summary
        assert "click" not in summary

    def test_jinja_template_failure_translates_to_parameter_phrasing(self) -> None:
        summary = self._format(
            "update_and_run_blocks",
            {
                "ok": False,
                "error": (
                    "navigation block failed. failure reason: Failed to format jinja "
                    "template: Failed to format Jinja style parameter 'AchievementType'."
                ),
            },
        )
        assert summary == "A workflow parameter could not be filled in."
        assert "AchievementType" not in summary
        assert "Jinja" not in summary

    def test_jinja_style_parameter_marker_alone_is_enough(self) -> None:
        summary = self._format(
            "update_and_run_blocks",
            {"ok": False, "error": "Jinja style parameter 'foo' could not be resolved"},
        )
        assert summary == "A workflow parameter could not be filled in."

    def test_invalid_selector_failure_replaces_engine_instruction_text(self) -> None:
        summary = self._format(
            "click",
            {
                "ok": False,
                "error": (
                    "Invalid selector: 'div:contains(Submit)'. jQuery pseudo-selectors "
                    "like :contains(), :eq(), :first, :visible are NOT valid CSS. "
                    "Use standard CSS selectors instead."
                ),
            },
        )
        assert summary == "Couldn't complete that step."
        assert "div:contains" not in summary
        assert "jQuery" not in summary
        assert "CSS" not in summary

    def test_use_the_x_tool_failure_replaces_engine_instruction_text(self) -> None:
        summary = self._format(
            "evaluate",
            {
                "ok": False,
                "error": "Do not use evaluate to click elements. Use the 'click' tool with a CSS selector instead.",
            },
        )
        assert summary == "Couldn't complete that step."
        assert "click" not in summary
        assert "evaluate" not in summary

    def test_use_the_tool_with_double_quotes_is_caught(self) -> None:
        summary = self._format(
            "click",
            {"ok": False, "error": 'Do not click via JS. Use the "evaluate" tool instead.'},
        )
        assert summary == "Couldn't complete that step."

    def test_use_the_tool_unquoted_is_caught(self) -> None:
        summary = self._format(
            "click",
            {"ok": False, "error": "Use the click tool with a CSS selector."},
        )
        assert summary == "Couldn't complete that step."

    def test_loop_detected_marker_in_middle_of_message_is_caught(self) -> None:
        summary = self._format(
            "click",
            {
                "ok": False,
                "error": (
                    "Tool execution failed. LOOP DETECTED: 'click' has been called 3 times "
                    "consecutively. This tool will not run again."
                ),
            },
        )
        assert summary == "The agent got stuck retrying the same step — moving on."

    def test_playwright_locator_timeout_failure_replaces_selector_dump(self) -> None:
        summary = self._format(
            "click",
            {
                "ok": False,
                "error": (
                    "Locator.click: Timeout 30000ms exceeded. "
                    'Call log: - waiting for locator("#btnSubmit").first - locator resolved to <input ...>'
                ),
            },
        )
        assert summary == "Couldn't complete that step."
        assert "btnSubmit" not in summary
        assert "Locator" not in summary
        assert "Call log" not in summary

    def test_unknown_error_sentinel_replaced_with_generic_phrasing(self) -> None:
        summary = self._format(
            "run_blocks_and_collect_debug",
            {"ok": False, "data": {"blocks": []}},
        )
        assert summary == "Couldn't complete that step."
        assert "Unknown error" not in summary
        assert "Failed:" not in summary

    def test_genuinely_user_relevant_failure_preserves_short_technical_token(self) -> None:
        summary = self._format(
            "navigate_browser",
            {
                "ok": False,
                "error": (
                    "Failed to navigate to url https://example.invalid. Error message: net::ERR_NAME_NOT_RESOLVED"
                ),
            },
        )
        assert summary.startswith("Failed:")
        assert "ERR_NAME_NOT_RESOLVED" in summary

    @pytest.mark.parametrize(
        ("tool_name", "result", "expected"),
        [
            pytest.param(
                "click",
                {"ok": True, "data": {"selector": "input[name='ackStatus']"}},
                "",
                id="click-suppressed",
            ),
            pytest.param(
                "type_text",
                {"ok": True, "data": {"selector": "#last_name", "typed_length": 5}},
                "",
                id="type_text-suppressed",
            ),
            pytest.param(
                "select_option",
                {"ok": True, "data": {"value": "option-1"}},
                "",
                id="select_option-suppressed",
            ),
            pytest.param(
                "navigate_browser",
                {"ok": True, "url": "https://example.com"},
                "Navigated to https://example.com",
                id="navigate_browser-fallthrough",
            ),
            pytest.param(
                "update_workflow",
                {"ok": True, "data": {"block_count": 3}},
                "Workflow updated (3 blocks)",
                id="update_workflow-fallthrough",
            ),
            pytest.param(
                "press_key",
                {"ok": True, "data": {"key": "Enter"}},
                "Pressed 'Enter'",
                id="press_key-fallthrough",
            ),
        ],
    )
    def test_success_summary_routing(self, tool_name: str, result: dict, expected: str) -> None:
        assert self._format(tool_name, result) == expected

    def test_evaluate_success_returns_empty_summary_dropping_shape_suffix(self) -> None:
        summary = self._format(
            "evaluate",
            {
                "ok": True,
                "data": {
                    "result": {
                        "bodyText": "...",
                        "rows": [],
                        "tableText": "",
                        "title": "Page",
                        "url": "https://example.com/",
                    },
                },
            },
        )
        assert summary == ""
        assert "object with keys" not in summary

    def test_summarize_tool_result_unchanged_for_click_success(self) -> None:
        agent_summary = summarize_tool_result(
            "click",
            {"ok": True, "data": {"selector": "#submit"}},
        )
        assert agent_summary == "Clicked '#submit'"

    def test_summarize_tool_result_uses_effective_click_target(self) -> None:
        agent_summary = summarize_tool_result(
            "click",
            {"ok": True, "data": {"selector": "", "effective_target": "xpath=//button[normalize-space(.)='Accept']"}},
        )
        assert agent_summary == "Clicked 'xpath=//button[normalize-space(.)='Accept']'"

    def test_summarize_tool_result_falls_back_to_resolved_selector(self) -> None:
        agent_summary = summarize_tool_result(
            "click",
            {"ok": True, "data": {"selector": None, "resolved_selector": "xpath=//button[2]"}},
        )
        assert agent_summary == "Clicked 'xpath=//button[2]'"


class TestUserFacingSuccess:
    @staticmethod
    def _blocker(blocker_kind: str, *, steering: str = "internal steering text"):
        from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal

        return CopilotToolBlockerSignal(
            blocker_kind=blocker_kind,  # type: ignore[arg-type]
            agent_steering_text=steering,
            user_facing_reason="I need more information before I can continue.",
            recovery_hint="ask_user_clarifying",
            internal_reason_code="test_reason_code",
            blocked_tool="evaluate",
        )

    def test_true_for_ok_result(self) -> None:
        assert user_facing_success({"ok": True, "data": {}}) is True

    def test_false_for_unclassified_failure(self) -> None:
        assert user_facing_success({"ok": False, "error": "plain failure"}) is False

    @pytest.mark.parametrize("blocker_kind", ["phase_gated", "missing_required_context", "authority_denied"])
    def test_true_for_precondition_style_blockers(self, blocker_kind: str) -> None:
        signal = self._blocker(blocker_kind)
        result = {"ok": False, "error": signal.agent_steering_text}
        assert user_facing_success(result, blocker_signal=signal) is True

    @pytest.mark.parametrize("blocker_kind", ["tool_error", "loop_detected"])
    def test_false_for_genuine_failure_blockers(self, blocker_kind: str) -> None:
        """Regression guard: real tool errors and loop-detection halts keep failure affect."""
        signal = self._blocker(blocker_kind)
        result = {"ok": False, "error": signal.agent_steering_text}
        assert user_facing_success(result, blocker_signal=signal) is False

    def test_false_when_blocker_signal_does_not_match_result(self) -> None:
        signal = self._blocker("phase_gated", steering="unrelated steering text")
        result = {"ok": False, "error": "a totally different failure"}
        assert user_facing_success(result, blocker_signal=signal) is False


def test_format_tool_result_for_user_reframes_internal_validation_failure() -> None:
    """Pins the SKY-11971 forensic leak: an unclassified internal validator reject must
    never surface its raw agent-steering text (block labels, field names) to the user."""
    raw_error = (
        "Workflow validation failed: corrected block metadata still appears stale. "
        "When changing a user's requested subject, URL, or action, rename affected block "
        "labels and titles to match the revised goal. Stale metadata: extract_step: label mismatch"
    )
    summary = format_tool_result_for_user("update_workflow", {"ok": False, "error": raw_error})
    assert summary == "Couldn't complete that step."
    assert "stale" not in summary
    assert "block" not in summary.lower()


class TestParseFinalResponse:
    """parse_final_response is the last mile between model output and the frontend.

    A parse failure falls back to `{"type": "REPLY", "user_response": text}`,
    which means the raw JSON object is rendered in the chat bubble. Real model
    outputs sometimes embed literal newlines inside string values (strict
    `json.loads` rejects those) — seen in SKY-9189 test-2 where the full
    refusal envelope landed in the user bubble instead of just user_response.
    """

    def test_parses_clean_json_envelope(self) -> None:
        envelope = '{"type": "ASK_QUESTION", "user_response": "hi"}'
        parsed = parse_final_response(envelope)
        assert parsed == {"type": "ASK_QUESTION", "user_response": "hi"}

    def test_strips_json_code_fence(self) -> None:
        envelope = '```json\n{"type": "REPLY", "user_response": "ok"}\n```'
        assert parse_final_response(envelope)["type"] == "REPLY"

    def test_tolerates_literal_newline_inside_string_value(self) -> None:
        # Real model output shape: a multi-line user_response split across
        # actual newlines instead of \n escapes. strict=True rejects this,
        # strict=False accepts it. Without the fallback, the whole JSON blob
        # gets shown to the user.
        envelope = '{"type": "ASK_QUESTION", "user_response": "line one\nline two"}'
        parsed = parse_final_response(envelope)
        assert parsed["type"] == "ASK_QUESTION"
        assert parsed["user_response"] == "line one\nline two"

    def test_unparseable_text_falls_back_to_reply(self) -> None:
        # Genuinely broken output still degrades gracefully.
        parsed = parse_final_response("not json at all")
        assert parsed == {"type": "REPLY", "user_response": "not json at all"}

    def test_non_dict_json_falls_back_to_reply(self) -> None:
        # A JSON array at top level is valid JSON but not a valid envelope.
        parsed = parse_final_response("[1, 2, 3]")
        assert parsed == {"type": "REPLY", "user_response": "[1, 2, 3]"}

    @pytest.mark.parametrize(
        ("envelope", "expected_type", "expected_fields"),
        [
            pytest.param(
                'REPLY\n{"type": "REPLY", "user_response": "ok"}',
                "REPLY",
                {"user_response": "ok"},
                id="plain-label",
            ),
            pytest.param(
                'ASK_QUESTION:\n{"type": "ASK_QUESTION", "user_response": "what date?"}',
                "ASK_QUESTION",
                {"user_response": "what date?"},
                id="colon-suffixed-label",
            ),
            pytest.param(
                'REPLACE_WORKFLOW {"type": "REPLACE_WORKFLOW", "user_response": "updated", "workflow_yaml": "title: x"}',
                "REPLACE_WORKFLOW",
                {"workflow_yaml": "title: x"},
                id="replace-workflow-label",
            ),
            pytest.param(
                'ask_question {"type": "ASK_QUESTION", "user_response": "which account?"}',
                "ASK_QUESTION",
                {"user_response": "which account?"},
                id="mixed-case-label",
            ),
            pytest.param(
                "REPLACE_WORKFLOW\n```json\n"
                '{"type": "REPLACE_WORKFLOW", "user_response": "updated", "workflow_yaml": "title: x"}\n'
                "```",
                "REPLACE_WORKFLOW",
                {"workflow_yaml": "title: x"},
                id="label-before-json-fence",
            ),
        ],
    )
    def test_strips_leading_response_type_label(self, envelope: str, expected_type: str, expected_fields: dict) -> None:
        parsed = parse_final_response(envelope)
        assert parsed["type"] == expected_type
        for key, value in expected_fields.items():
            assert parsed[key] == value

    def test_plain_leading_label_falls_through_for_output_policy(self) -> None:
        text = "ASK_QUESTION\nWhich account should I use?"
        parsed = parse_final_response(text)
        assert parsed == {"type": "REPLY", "user_response": text}

    def test_sentence_starting_with_reply_is_not_stripped(self) -> None:
        text = "Reply with the invoice number from the page."
        parsed = parse_final_response(text)
        assert parsed == {"type": "REPLY", "user_response": text}

    def test_extracts_json_after_prose_preamble(self) -> None:
        envelope = 'Here\'s my response: {"type": "REPLY", "user_response": "ok"}'
        parsed = parse_final_response(envelope)
        assert parsed["type"] == "REPLY"
        assert parsed["user_response"] == "ok"

    def test_pass_b_rejects_non_envelope_dict_in_prose(self) -> None:
        text = 'I cannot help with {"foo": "bar"}'
        parsed = parse_final_response(text)
        assert parsed == {"type": "REPLY", "user_response": text}

    def test_pass_b_rejects_dict_with_unrecognized_type(self) -> None:
        text = 'I cannot help with {"type": "object"}'
        parsed = parse_final_response(text)
        assert parsed == {"type": "REPLY", "user_response": text}

    def test_recovery_tier_skipped_when_text_only_mentions_user_response(self) -> None:
        text = 'I cannot find the "user_response" field in your input.'
        parsed = parse_final_response(text)
        assert parsed == {"type": "REPLY", "user_response": text}

    def test_recovery_tier_skipped_when_prose_quotes_both_markers(self) -> None:
        # Prose discussing the envelope format (both quoted `"type": "REPLY"`
        # and `"user_response"` substrings present, no leading `{`) must not
        # degrade to "Done." — the user's actual prose has to survive.
        text = 'I see "type": "REPLY" mentioned, but cannot find "user_response" anywhere.'
        parsed = parse_final_response(text)
        assert parsed == {"type": "REPLY", "user_response": text}

    def test_recovers_user_response_when_global_llm_context_malformed(self) -> None:
        envelope = '{"type": "REPLY", "user_response": "the real answer", "global_llm_context": {"user_goal": "x",}}'
        parsed = parse_final_response(envelope)
        assert parsed["user_response"] == "the real answer"
        assert parsed["type"] == "REPLY"

    def test_recovers_user_response_with_escaped_quotes(self) -> None:
        envelope = '{"type": "REPLY", "user_response": "she said \\"hi\\"", "global_llm_context": {bad}}'
        parsed = parse_final_response(envelope)
        assert parsed["user_response"] == 'she said "hi"'

    def test_regex_recovery_tolerates_literal_newline_in_user_response_value(self) -> None:
        envelope = '{"type": "REPLY", "user_response": "line one\nline two", "global_llm_context": {bad}}'
        parsed = parse_final_response(envelope)
        assert parsed["user_response"] == "line one\nline two"

    def test_recovers_ask_question_type_when_recovering_user_response(self) -> None:
        envelope = '{"type": "ASK_QUESTION", "user_response": "which account?", "global_llm_context": {bad}}'
        parsed = parse_final_response(envelope)
        assert parsed["type"] == "ASK_QUESTION"
        assert parsed["user_response"] == "which account?"

    def test_recovery_demotes_malformed_replace_workflow_to_reply(self) -> None:
        # Recovery cannot extract workflow_yaml, so REPLACE_WORKFLOW would be
        # unverified — demote to REPLY.
        envelope = '{"type": "REPLACE_WORKFLOW", "user_response": "updated your workflow", "global_llm_context": {bad}}'
        parsed = parse_final_response(envelope)
        assert parsed["type"] == "REPLY"
        assert parsed["user_response"] == "updated your workflow"

    def test_envelope_shaped_unparseable_with_no_recoverable_user_response_returns_done(self) -> None:
        envelope = '{"type": "REPLY", "user_response": "broken'
        parsed = parse_final_response(envelope)
        assert parsed["user_response"] == "Done."
        assert parsed["type"] == "REPLY"
        assert "broken" not in parsed["user_response"]

    def test_non_envelope_unparseable_text_still_falls_back_to_text(self) -> None:
        text = "I'm not sure how to help with that."
        parsed = parse_final_response(text)
        assert parsed == {"type": "REPLY", "user_response": text}


class TestLooksLikeWorkflowYamlInChat:
    def test_detects_block_yaml_with_navigation_goal(self) -> None:
        text = (
            "Here's how the block now looks:\n\n"
            "    - label: fill_form\n"
            "      block_type: navigation\n"
            "      navigation_goal: Fill the abuse form.\n"
            "      url: https://example.test/abuse\n"
            "      parameter_keys:\n"
            "        - name\n"
        )
        assert looks_like_workflow_yaml_in_chat(text) is True

    def test_detects_block_yaml_inside_fenced_code(self) -> None:
        text = (
            "I've drafted the change:\n\n"
            "```yaml\n"
            "block_type: extraction\n"
            "data_extraction_goal: Pull the table.\n"
            "label: extract_data\n"
            "```\n"
        )
        assert looks_like_workflow_yaml_in_chat(text) is True

    def test_detects_full_workflow_definition_paste(self) -> None:
        text = (
            "workflow_definition:\n"
            "  parameters: []\n"
            "  blocks:\n"
            "    - block_type: validation\n"
            "      complete_criterion: The page shows a thank-you message.\n"
        )
        assert looks_like_workflow_yaml_in_chat(text) is True

    def test_does_not_flag_inline_block_type_mention(self) -> None:
        text = (
            "I'll use a navigation block to fill the form. The block_type field on a "
            "navigation block accepts goals like a navigation_goal string — but the user "
            "doesn't need to see the YAML directly."
        )
        assert looks_like_workflow_yaml_in_chat(text) is False

    def test_does_not_flag_short_prose(self) -> None:
        assert looks_like_workflow_yaml_in_chat("Sure, I can do that.") is False

    def test_does_not_flag_empty_or_non_string(self) -> None:
        assert looks_like_workflow_yaml_in_chat("") is False
        assert looks_like_workflow_yaml_in_chat(None) is False
        assert looks_like_workflow_yaml_in_chat(12345) is False

    def test_detects_bare_block_type_line(self) -> None:
        text = "Here's a small snippet:\n\n    - block_type: navigation\n      label: open_page\n"
        assert looks_like_workflow_yaml_in_chat(text) is True

    def test_unknown_block_type_value_does_not_trip(self) -> None:
        text = "Diagnostic note:\n\n    block_type: experimental_thing\n    detail: not a real block\n"
        assert looks_like_workflow_yaml_in_chat(text) is False

    def test_detects_json_shape_block_paste(self) -> None:
        text = (
            "Here is the block as JSON:\n\n"
            "```json\n"
            "{\n"
            '  "block_type": "navigation",\n'
            '  "navigation_goal": "Fill the form.",\n'
            '  "parameter_keys": ["name"]\n'
            "}\n"
            "```\n"
        )
        assert looks_like_workflow_yaml_in_chat(text) is True

    def test_inline_field_mention_does_not_trip(self) -> None:
        text = (
            "When the navigation_goal field is unset and the block_type is wrong, the block "
            "will fail validation — those fields need to come from the user."
        )
        assert looks_like_workflow_yaml_in_chat(text) is False


def test_summarize_tool_result_detail_returns_none_on_success() -> None:
    assert summarize_tool_result_detail({"ok": True, "data": {"block_count": 2}}) is None


def test_summarize_tool_result_detail_omits_detail_for_reclassified_neutral_redirect() -> None:
    """Regression guard (Codex, PR #13274): a phase/authority redirect reclassified to
    success=True by user_facing_success must not still carry a non-None `detail` — the
    schema documents `detail` as None on success, and this row renders without failure
    affect. Without passing the reclassified `success` through, the raw `ok: false`
    still drives a non-None structured detail here."""
    from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal

    signal = CopilotToolBlockerSignal(
        blocker_kind="phase_gated",
        agent_steering_text="internal steering text",
        user_facing_reason="I need to know what page to inspect first.",
        recovery_hint="ask_user_clarifying",
        internal_reason_code="test_reason_code",
        blocked_tool="evaluate",
    )
    result = {"ok": False, "error": signal.agent_steering_text}
    reclassified_success = user_facing_success(result, blocker_signal=signal)
    assert reclassified_success is True

    assert summarize_tool_result_detail(result, blocker_signal=signal) is not None
    assert summarize_tool_result_detail(result, blocker_signal=signal, success=reclassified_success) is None


def test_summarize_tool_result_detail_caps_at_max_chars() -> None:
    long_error = "Element lookup failed: " + ("missing field 'foo'; " * 200)
    detail = summarize_tool_result_detail({"ok": False, "error": long_error}, max_chars=400)
    assert detail is not None
    assert len(detail) <= 400
    assert detail.endswith("...")


def test_summarize_tool_result_detail_preserves_short_full_message() -> None:
    detail = summarize_tool_result_detail(
        {"ok": False, "error": "Element lookup failed: title field required"},
    )
    assert detail == "Element lookup failed: title field required"


def test_summarize_tool_result_detail_reframes_internal_validation_failure() -> None:
    """Tooltip-grade detail must not leak raw internal validator text either."""
    detail = summarize_tool_result_detail(
        {"ok": False, "error": "Workflow validation failed: title field required"},
    )
    assert detail == "Couldn't complete that step."


def test_summarize_tool_result_detail_strips_header_blobs() -> None:
    text = "Failure with headers: {'host': 'x', 'authorization': 'Bearer abc'} please retry"
    detail = summarize_tool_result_detail({"ok": False, "error": text})
    assert detail is not None
    assert "authorization" not in detail
    assert "Bearer" not in detail


def test_sanitize_failure_text_default_cap_unchanged() -> None:
    sanitized = _sanitize_failure_text("x" * 200)
    assert len(sanitized) == 120
    assert sanitized.endswith("...")


def test_sanitize_failure_text_respects_max_chars() -> None:
    sanitized = _sanitize_failure_text("x" * 1000, max_chars=500)
    assert len(sanitized) == 500
    assert sanitized.endswith("...")


def test_sanitize_tool_result_for_llm_passes_through_failure_dict() -> None:
    failure = {"ok": False, "error": "Workflow validation failed: title required"}
    sanitized = sanitize_tool_result_for_llm("update_workflow", failure)
    assert sanitized["ok"] is False
    assert sanitized["error"] == "Workflow validation failed: title required"


def test_build_run_blocks_response_success_passes_through() -> None:
    response = build_run_blocks_response(True, {"workflow_run_id": "wr_test", "blocks": []})
    assert response == {"ok": True, "data": {"workflow_run_id": "wr_test", "blocks": []}}


def test_build_run_blocks_response_promotes_run_level_failure_reason() -> None:
    response = build_run_blocks_response(
        False,
        {
            "workflow_run_id": "wr_test",
            "overall_status": "failed",
            "failure_reason": "Navigation timed out after 60s",
            "blocks": [],
        },
    )
    assert response["ok"] is False
    assert response["error"] == "Navigation timed out after 60s"


def test_build_run_blocks_response_falls_back_when_no_failure_reason() -> None:
    response = build_run_blocks_response(False, {"workflow_run_id": "wr_test"})
    assert response["error"] == "Unknown error (no failure reason provided)"
