"""Terminal-output invariant and observed-facts halt narrative tests.

OSS-synced: only example.* / RFC-2606 placeholder targets and synthetic labels.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.agent import (
    _build_wip_exit_result,
    _clean_recorded_failure_text,
    _observed_facts_halt_reply,
    _recorded_failure_reply,
)
from skyvern.forge.sdk.copilot.blocker_signal import contains_internal_machinery_leak
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.failure_tracking import PER_TOOL_BUDGET_FAILURE_CATEGORY
from skyvern.forge.sdk.copilot.output_policy import (
    CopilotOutputKind,
    OutputPolicyReason,
    evaluate_output_policy,
)

_ANTI_RERUN_GUARD_TEXT = (
    "The prior PER_TOOL_BUDGET run for run wr_538488327000571062 advanced the live browser at "
    "https://www.example.com/. Before another block-running tool, inspect the current browser page with "
    'inspect_page_for_composition(target_url="current_page").'
)

_PACING_GUARD_TEXT = "The run exceeded the 3s per-tool-call budget while still making progress."


def _ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )


class TestInternalMachineryLeakPredicate:
    @pytest.mark.parametrize(
        "text",
        [
            _ANTI_RERUN_GUARD_TEXT,
            _PACING_GUARD_TEXT,
            "Outcome is uncertain for run wr_538438176486379954.",
            "Browser session pbs_538476723643067648 is live.",
            "Before another block-running tool, inspect the current browser page.",
            "Call update_and_run_blocks with the smaller chain.",
            "Use inspect_page_for_composition to confirm the page state.",
        ],
    )
    def test_internal_machinery_is_detected(self, text: str) -> None:
        assert contains_internal_machinery_leak(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "I created and tested the workflow successfully.",
            "I built a 2-block draft and was still testing it when the turn ran out of time.",
            "The site's verification page was still blocking the search control.",
            "Send me the registry URL you want to search.",
            "",
        ],
    )
    def test_product_language_is_clean(self, text: str) -> None:
        assert contains_internal_machinery_leak(text) is False

    def test_output_policy_hard_blocks_guard_text(self) -> None:
        verdict = evaluate_output_policy(
            request_policy=None,
            response_type="REPLY",
            user_response=_ANTI_RERUN_GUARD_TEXT,
            output_kind=CopilotOutputKind.INFORMATIONAL_ANSWER,
        )
        assert OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK in verdict.reason_codes


class TestRecordedFailureReply:
    def test_guard_text_renders_observed_facts_not_test_failed(self) -> None:
        ctx = _ctx()
        ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[object()]))
        ctx.last_update_block_count = 1
        ctx.last_test_ok = False
        ctx.last_test_failure_reason = _ANTI_RERUN_GUARD_TEXT
        ctx.workflow_verification_evidence.current_url = "https://www.example.com/registry"

        reply = _recorded_failure_reply(ctx)

        assert reply is not None
        assert "wr_" not in reply
        assert "PER_TOOL_BUDGET" not in reply
        assert "per-tool-call budget" not in reply
        assert "block-running tool" not in reply
        assert "the test failed" not in reply
        assert "ran out of time" in reply
        assert "1-block draft" in reply
        assert "https://www.example.com/registry" in reply

    def test_budget_paced_run_does_not_render_as_test_failed(self) -> None:
        ctx = _ctx()
        ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[object()]))
        ctx.last_update_block_count = 1
        ctx.last_test_ok = False
        ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
        ctx.last_test_failure_reason = "The run was canceled while still making progress."

        reply = _recorded_failure_reply(ctx)

        assert reply is not None
        assert "the test failed" not in reply
        assert "ran out of time" in reply

    def test_fragment_cleaner_substitutes_guard_text(self) -> None:
        cleaned = _clean_recorded_failure_text(_ANTI_RERUN_GUARD_TEXT)
        assert "wr_" not in cleaned
        assert "PER_TOOL_BUDGET" not in cleaned

    def test_observed_facts_reply_without_draft_states_timeout(self) -> None:
        ctx = _ctx()
        reply = _observed_facts_halt_reply(ctx)
        assert "ran out of time" in reply
        assert not contains_internal_machinery_leak(reply)


class TestWipExitTerminalInvariant:
    def test_timeout_exit_with_guard_failure_reason_renders_clean_terminal(self) -> None:
        ctx = _ctx()
        ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[object()]))
        ctx.last_workflow_yaml = "title: wf\nworkflow_definition:\n  blocks: []\n"
        ctx.last_update_block_count = 1
        ctx.last_test_ok = False
        ctx.last_test_failure_reason = _ANTI_RERUN_GUARD_TEXT
        ctx.workflow_verification_evidence.current_url = "https://www.example.com/registry"

        result = _build_wip_exit_result(
            ctx,
            None,
            default_reply="I ran out of time processing your request.",
            unvalidated_reply="I ran out of time; the draft is untested.",
            tested_reply="I ran out of time, but I have a tested draft for you.",
            terminal_reason="timeout",
        )

        assert not contains_internal_machinery_leak(result.user_response)
        assert "the test failed" not in result.user_response
        # The halt surfaces the draft for review instead of discarding it.
        assert result.updated_workflow is ctx.last_workflow
