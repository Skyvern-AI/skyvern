"""Tests for truncate_output and sanitize_tool_result_for_llm."""

from __future__ import annotations

from unittest.mock import MagicMock

from skyvern.forge.sdk.copilot.output_utils import (
    _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
    format_tool_result_for_user,
    parse_final_response,
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

    def test_click_success_returns_empty_summary(self) -> None:
        summary = self._format(
            "click",
            {"ok": True, "data": {"selector": "input[name='ackStatus']"}},
        )
        assert summary == ""

    def test_type_text_success_returns_empty_summary(self) -> None:
        summary = self._format(
            "type_text",
            {"ok": True, "data": {"selector": "#last_name", "typed_length": 5}},
        )
        assert summary == ""

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

    def test_select_option_success_returns_empty_summary(self) -> None:
        summary = self._format(
            "select_option",
            {"ok": True, "data": {"value": "option-1"}},
        )
        assert summary == ""

    def test_navigate_browser_success_falls_through_to_summarize(self) -> None:
        summary = self._format(
            "navigate_browser",
            {"ok": True, "url": "https://example.com"},
        )
        assert summary == "Navigated to https://example.com"

    def test_update_workflow_success_falls_through_to_summarize(self) -> None:
        summary = self._format(
            "update_workflow",
            {"ok": True, "data": {"block_count": 3}},
        )
        assert "3" in summary

    def test_press_key_success_falls_through_to_summarize(self) -> None:
        summary = self._format(
            "press_key",
            {"ok": True, "data": {"key": "Enter"}},
        )
        assert summary == "Pressed 'Enter'"

    def test_summarize_tool_result_unchanged_for_click_success(self) -> None:
        agent_summary = summarize_tool_result(
            "click",
            {"ok": True, "data": {"selector": "#submit"}},
        )
        assert agent_summary == "Clicked '#submit'"


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

    def test_strips_leading_reply_label_before_parse(self) -> None:
        envelope = 'REPLY\n{"type": "REPLY", "user_response": "ok"}'
        parsed = parse_final_response(envelope)
        assert parsed["type"] == "REPLY"
        assert parsed["user_response"] == "ok"

    def test_strips_leading_ask_question_label_with_colon(self) -> None:
        envelope = 'ASK_QUESTION:\n{"type": "ASK_QUESTION", "user_response": "what date?"}'
        parsed = parse_final_response(envelope)
        assert parsed["type"] == "ASK_QUESTION"
        assert parsed["user_response"] == "what date?"

    def test_strips_leading_replace_workflow_label(self) -> None:
        envelope = (
            'REPLACE_WORKFLOW {"type": "REPLACE_WORKFLOW", "user_response": "updated", "workflow_yaml": "title: x"}'
        )
        parsed = parse_final_response(envelope)
        assert parsed["type"] == "REPLACE_WORKFLOW"
        assert parsed["workflow_yaml"] == "title: x"

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
