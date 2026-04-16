"""Tests for context.py: StructuredContext caps and CopilotContext dataclass shape."""

from __future__ import annotations

from unittest.mock import MagicMock

from skyvern.forge.sdk.copilot.context import StructuredContext


def test_merge_turn_summary_caps_urls_visited() -> None:
    ctx = StructuredContext()
    activity = [{"tool": "navigate_browser", "summary": f"Navigated to https://site{i}.test"} for i in range(60)]
    ctx.merge_turn_summary(activity)

    assert len(ctx.urls_visited) == 40
    # Oldest entries trimmed; most recent survive.
    assert ctx.urls_visited[-1].url == "https://site59.test"


def test_merge_turn_summary_caps_fields_filled() -> None:
    ctx = StructuredContext()
    activity = [{"tool": "type_text", "summary": f"Typed into '#field{i}'"} for i in range(60)]
    ctx.merge_turn_summary(activity)

    assert len(ctx.fields_filled) == 40
    assert ctx.fields_filled[-1].selector == "#field59"


def test_merge_turn_summary_caps_credentials_checked() -> None:
    ctx = StructuredContext()
    activity = [{"tool": "list_credentials", "summary": f"Found 1 for site {i}"} for i in range(60)]
    ctx.merge_turn_summary(activity)

    assert len(ctx.credentials_checked) == 40


class TestCopilotContext:
    def test_inherits_agent_context(self) -> None:
        from skyvern.forge.sdk.copilot.context import CopilotContext
        from skyvern.forge.sdk.copilot.runtime import AgentContext

        assert issubclass(CopilotContext, AgentContext)

    def test_has_enforcement_fields(self) -> None:
        import dataclasses

        from skyvern.forge.sdk.copilot.context import CopilotContext

        field_names = {f.name for f in dataclasses.fields(CopilotContext)}
        enforcement_fields = {
            "navigate_called",
            "observation_after_navigate",
            "navigate_enforcement_done",
            "update_workflow_called",
            "test_after_update_done",
            "post_update_nudge_count",
            "coverage_nudge_count",
            "format_nudge_count",
            "explore_without_workflow_nudge_count",
            "user_message",
            "consecutive_tool_tracker",
            "tool_activity",
            "last_workflow",
            "last_workflow_yaml",
            "workflow_persisted",
        }
        missing = enforcement_fields - field_names
        assert not missing, f"Missing fields: {missing}"

    def test_defaults(self) -> None:
        from skyvern.forge.sdk.copilot.context import CopilotContext

        stream = MagicMock()
        ctx = CopilotContext(
            organization_id="org-1",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_yaml="",
            browser_session_id=None,
            stream=stream,
        )
        assert ctx.navigate_called is False
        assert ctx.update_workflow_called is False
        assert ctx.coverage_nudge_count == 0
        assert ctx.format_nudge_count == 0
        assert ctx.explore_without_workflow_nudge_count == 0
        assert ctx.user_message == ""
        assert ctx.consecutive_tool_tracker == []
        assert ctx.tool_activity == []
        assert ctx.last_workflow is None
        assert ctx.workflow_persisted is False

    def test_has_frontier_and_repeated_failure_fields(self) -> None:
        import dataclasses

        from skyvern.forge.sdk.copilot.context import CopilotContext

        field_names = {f.name for f in dataclasses.fields(CopilotContext)}
        frontier_fields = {
            "verified_block_outputs",
            "verified_prefix_labels",
            "last_requested_block_labels",
            "last_executed_block_labels",
            "last_frontier_start_label",
            "last_frontier_fingerprint",
            "last_failure_signature",
            "repeated_failure_streak_count",
            "repeated_failure_nudge_emitted_at_streak",
        }
        missing = frontier_fields - field_names
        assert not missing, f"Missing frontier/failure fields: {missing}"

    def test_frontier_field_defaults(self) -> None:
        from skyvern.forge.sdk.copilot.context import CopilotContext

        stream = MagicMock()
        ctx = CopilotContext(
            organization_id="org-1",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_yaml="",
            browser_session_id=None,
            stream=stream,
        )
        assert ctx.verified_block_outputs == {}
        assert ctx.verified_prefix_labels == []
        assert ctx.last_requested_block_labels == []
        assert ctx.last_executed_block_labels == []
        assert ctx.last_frontier_start_label is None
        assert ctx.last_frontier_fingerprint is None
        assert ctx.last_failure_signature is None
        assert ctx.repeated_failure_streak_count == 0
        assert ctx.repeated_failure_nudge_emitted_at_streak == 0
