"""Tests for context.py: StructuredContext caps and CopilotContext dataclass shape."""

from __future__ import annotations

from unittest.mock import MagicMock

from skyvern.forge.sdk.copilot.context import ObservedPage, StructuredContext, _merge_observed_acted_pages


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


def test_merge_turn_summary_records_resolved_credential_ids() -> None:
    ctx = StructuredContext()
    activity = [
        {
            "tool": "list_credentials",
            "summary": "Found 2 credential(s)",
            "credentials": [
                {"credential_id": "cred_amazon", "name": "Amazon"},
                {"credential_id": "cred_quicken", "name": "Quicken Classic"},
            ],
        }
    ]
    ctx.merge_turn_summary(activity)

    by_id = {check.credential_id: check for check in ctx.credentials_checked}
    assert set(by_id) == {"cred_amazon", "cred_quicken"}
    assert all(check.found for check in ctx.credentials_checked)
    assert by_id["cred_amazon"].credential_name == "Amazon"


def test_resolved_credential_ids_survive_context_roundtrip() -> None:
    ctx = StructuredContext()
    ctx.merge_turn_summary(
        [
            {
                "tool": "list_credentials",
                "summary": "Found 1 credential(s)",
                "credentials": [{"credential_id": "cred_amazon", "name": "Amazon"}],
            }
        ]
    )

    rehydrated = StructuredContext.from_json_str(ctx.to_json_str())

    assert [check.credential_id for check in rehydrated.credentials_checked] == ["cred_amazon"]


def test_merge_turn_summary_falls_back_to_summary_without_structured_credentials() -> None:
    ctx = StructuredContext()
    ctx.merge_turn_summary([{"tool": "list_credentials", "summary": "Found 0 credential(s)"}])

    assert len(ctx.credentials_checked) == 1
    assert ctx.credentials_checked[0].credential_id is None
    assert ctx.credentials_checked[0].found is False


def test_merge_observed_acted_pages_uses_nested_evidence_url() -> None:
    pages = _merge_observed_acted_pages(
        [ObservedPage(url="https://example.com/old", had_bounded_schema=True, reached_via="navigate")],
        [
            {
                "evidence": {
                    "current_url": "https://example.com/cart",
                    "inspected_url": "https://example.com/cart",
                },
                "had_bounded_schema": True,
                "reached_via": "interaction",
                "step": 3,
            }
        ],
    )

    by_url = {page.url: page for page in pages}
    assert by_url["https://example.com/cart"].had_bounded_schema is True
    assert by_url["https://example.com/cart"].reached_via == "interaction"


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
            "failed_tool_step_tracker",
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
        assert ctx.failed_tool_step_tracker == {}
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
            "verified_prefix_current_url",
            "last_run_blocks_workflow_run_id",
            "last_requested_block_labels",
            "last_executed_block_labels",
            "last_full_workflow_test_ok",
            "last_unverified_block_labels",
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
        assert ctx.verified_prefix_current_url is None
        assert ctx.last_run_blocks_workflow_run_id is None
        assert ctx.last_requested_block_labels == []
        assert ctx.last_executed_block_labels == []
        assert ctx.last_full_workflow_test_ok is False
        assert ctx.last_unverified_block_labels == []
        assert ctx.last_frontier_start_label is None
        assert ctx.last_frontier_fingerprint is None
        assert ctx.last_failure_signature is None
        assert ctx.repeated_failure_streak_count == 0
        assert ctx.repeated_failure_nudge_emitted_at_streak == 0
