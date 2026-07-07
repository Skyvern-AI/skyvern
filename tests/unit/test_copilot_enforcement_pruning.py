"""Tests for enforcement pruning and null-data handling.

These cover three regressions observed in trace 019d7b5c884dff0ff648680b9f31f715:
  1. Extraction returning all-null fields was treated as success.
  2. Context grew linearly because old tool outputs kept full content.
  3. No escalation when the agent looped on the same null-data failure.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents import RunConfig

from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal, stash_blocker_signal
from skyvern.forge.sdk.copilot.build_test_outcome import (
    RecordedBuildTestOutcome,
    author_time_reject_missing_output_paths,
    recorded_outcome_from_author_time_reject,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import SynthesizedCodeBlock
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.config import SYNTHESIZED_OFFER_REFRESH_STEP_THRESHOLD, BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext
from skyvern.forge.sdk.copilot.enforcement import (
    KEEP_RECENT_TOOL_OUTPUTS,
    SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE,
    CopilotGoalSatisfied,
    _canonical_output_path,
    _check_enforcement,
    _maybe_synthesized_block_offer_msg,
    _needs_suspicious_success_nudge,
    _prune_input_list,
    _should_block_mutating_tool_after_synthesized_offer,
    _should_force_advisory_run_dispatch,
    _should_force_synthesized_block_persistence,
    _summarize_tool_output,
    _uncovered_output_reject_admits_evaluate,
    consume_uncovered_output_reopen_event,
    record_scouted_output_coverage,
    run_with_enforcement,
    synthesized_block_persistence_signal,
    synthesized_persistence_reopened,
    synthesized_persistence_reopened_after_failed_run,
    synthesized_trajectory_is_goal_complete,
    uncovered_output_reject_scout_steer_signal,
    uncovered_requested_output_paths,
)
from skyvern.forge.sdk.copilot.mcp_adapter import (
    _POST_HOOK_CONTEXT_ROLLBACK_FIELDS,
    _restore_post_hook_context,
    _snapshot_post_hook_context,
)
from skyvern.forge.sdk.copilot.output_contracts import (
    OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE,
    OutputContractAdvisoryState,
)
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion
from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool
from skyvern.forge.sdk.copilot.tools import (
    _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
    _analyze_run_blocks,
    _is_meaningful_extracted_data,
    _record_run_blocks_result,
    _record_workflow_update_result,
)
from skyvern.forge.sdk.copilot.turn_halt import stash_turn_halt_from_blocker_signal
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentAuthority, TurnIntentMode
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from tests.unit.conftest import make_copilot_context


class _Ctx:
    """Minimal stand-in for CopilotContext used in enforcement checks.

    Keep this in sync with ``AgentContext`` enforcement-state fields — missing
    attributes would show up as AttributeError in the branches that use bare
    access rather than ``getattr``.
    """

    def __init__(self) -> None:
        self.navigate_called = False
        self.observation_after_navigate = False
        self.navigate_enforcement_done = False
        self.update_workflow_called = False
        self.test_after_update_done = False
        self.post_update_nudge_count = 0
        self.coverage_nudge_count = 0
        self.format_nudge_count = 0
        self.user_message = ""
        self.last_update_block_count = None
        self.last_test_ok = None
        self.last_test_failure_reason = None
        self.last_test_suspicious_success = False
        self.last_test_anti_bot = None
        self.last_failure_category_top = None
        self.failed_test_nudge_count = 0
        self.explore_without_workflow_nudge_count = 0
        self.repeated_failure_streak_count = 0
        self.repeated_failure_nudge_emitted_at_streak = 0
        self.verified_terminal_proposal_ready = False
        self.completion_verification_result = None
        self.last_artifact_health_blocker_reason = None
        self.latest_diagnosis_repair_contract = None
        self.last_code_authoring_repair_context = None
        self.synthesized_block_reopened_after_failed_run = False
        self.synthesized_block_reopened_for_output_coverage = False
        self.scouted_output_covered_paths: set[str] = set()
        self.uncovered_output_rescout_context_key = None
        self.uncovered_output_rescout_steer_key = None
        self.latest_recorded_build_test_outcome = None
        self.last_run_blocks_workflow_run_id = None
        self.completion_criteria_turn_state = None
        self.reached_download_target: ReachedDownloadTarget | None = None


class TestSynthesizedOfferPersistenceGate:
    @staticmethod
    def _unsatisfied_verification() -> CompletionVerificationResult:
        return CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["fallback"],
            verdicts=[
                CriterionVerdict(
                    criterion_id="fallback",
                    state="unsatisfied",
                    reason_code="evidence_contradicts",
                )
            ],
        )

    def _authoring_ctx(
        self,
        *,
        trajectory: list[dict[str, object]],
        download_target: ReachedDownloadTarget | None,
    ) -> _Ctx:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = len(trajectory)
        ctx.scout_trajectory = trajectory
        ctx.reached_download_target = download_target
        ctx.synthesized_block_offered_goal_complete = synthesized_trajectory_is_goal_complete(ctx)
        return ctx

    @pytest.mark.asyncio
    async def test_offer_retry_forces_update_and_run_blocks_tool_choice(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "input[name='q']",
                "source_url": "https://example.test/start",
                "accessible_name": "Search",
            },
            {
                "tool_name": "click",
                "selector": "button[data-action='search']",
                "accessible_name": "Search",
            },
        ]
        ctx.synthesized_block_offered = False
        ctx.synthesized_block_offered_trajectory_len = 0
        ctx.reached_download_target = None
        stream = MagicMock()
        stream.is_disconnected = AsyncMock(return_value=False)

        fake_result = MagicMock()
        fake_result.final_output = None
        fake_result.new_items = []
        fake_result.to_input_list.return_value = []
        run_configs: list[RunConfig | None] = []

        def fake_run_streamed(*args: Any, **kwargs: Any) -> Any:
            run_configs.append(kwargs.get("run_config"))
            return fake_result

        async def fake_stream_to_sse(result: Any, s: Any, c: Any) -> None:
            if len(run_configs) >= 2:
                c.update_workflow_called = True
                c.test_after_update_done = True

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement.synthesize_code_block",
            lambda *args, **kwargs: SynthesizedCodeBlock(code="await page.click('button')"),
        )
        monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", fake_run_streamed)
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
            fake_stream_to_sse,
        )

        returned = await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
            run_config=RunConfig(),
        )

        assert returned is fake_result
        assert len(run_configs) == 2
        assert run_configs[0].model_settings is None
        assert run_configs[1].model_settings is not None
        assert run_configs[1].model_settings.tool_choice == "update_and_run_blocks"

    @pytest.mark.asyncio
    async def test_diagnose_offer_retry_does_not_force_update_and_run_blocks_tool_choice(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.DIAGNOSE,
            authority=TurnIntentAuthority(may_read_run_context=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.scout_trajectory = [
            {
                "tool_name": "click",
                "selector": "button[data-action='continue']",
                "source_url": "https://example.test/start",
            }
        ]
        ctx.synthesized_block_offered = False
        ctx.synthesized_block_offered_trajectory_len = 0
        ctx.reached_download_target = None
        stream = MagicMock()
        stream.is_disconnected = AsyncMock(return_value=False)

        fake_result = MagicMock()
        fake_result.final_output = None
        fake_result.new_items = []
        fake_result.to_input_list.return_value = []
        run_configs: list[RunConfig | None] = []

        def fake_run_streamed(*args: Any, **kwargs: Any) -> Any:
            run_configs.append(kwargs.get("run_config"))
            return fake_result

        async def fake_stream_to_sse(result: Any, s: Any, c: Any) -> None:
            if len(run_configs) >= 2:
                c.update_workflow_called = True
                c.test_after_update_done = True

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement.synthesize_code_block",
            lambda *args, **kwargs: SynthesizedCodeBlock(code="await page.click('button')"),
        )
        monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", fake_run_streamed)
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
            fake_stream_to_sse,
        )

        returned = await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
            run_config=RunConfig(),
        )

        assert returned is fake_result
        assert len(run_configs) == 2
        assert run_configs[0].model_settings is None
        assert run_configs[1].model_settings is None

    def test_authoring_offer_blocks_non_persistence_tool_until_update_and_run_blocks(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 2
        ctx.synthesized_block_offered_goal_complete = True
        ctx.scout_trajectory = [
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
            {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
        ]

        signal = synthesized_block_persistence_signal(ctx, "evaluate")

        assert isinstance(signal, CopilotToolBlockerSignal)
        assert signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE
        assert signal.blocked_tool == "evaluate"
        assert signal.cleared_by_tools == frozenset({"update_and_run_blocks"})
        assert signal.recovery_hint == "retry_with_different_tool"
        assert signal.renders_final_reply is False
        assert synthesized_block_persistence_signal(ctx, "update_and_run_blocks") is None

    def test_authoring_offer_blocks_page_mutating_tool_before_loop_detection(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.synthesized_block_offered_goal_complete = False
        ctx.scout_trajectory = [{"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"}]

        signal = synthesized_block_persistence_signal(ctx, "click")

        assert isinstance(signal, CopilotToolBlockerSignal)
        assert signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE
        assert signal.blocked_tool == "click"
        assert signal.cleared_by_tools == frozenset({"update_and_run_blocks"})
        assert signal.renders_final_reply is False

    def test_prerun_ambiguous_bare_selector_repair_allows_one_evaluate(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.synthesized_block_offered_goal_complete = True
        ctx.scout_trajectory = [{"tool_name": "click", "selector": "button"}]
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="lookup",
            reason_code="ambiguous_bare_selector",
            selector="button",
            source_url="https://example.com",
        )

        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    def test_repeated_ambiguous_bare_selector_context_blocks_second_evaluate(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.synthesized_block_offered_goal_complete = True
        ctx.scout_trajectory = [{"tool_name": "click", "selector": "button"}]
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="lookup",
            reason_code="ambiguous_bare_selector",
            selector="button",
            source_url="https://example.com",
        )

        assert synthesized_block_persistence_signal(ctx, "evaluate") is None
        signal = synthesized_block_persistence_signal(ctx, "evaluate")

        assert isinstance(signal, CopilotToolBlockerSignal)
        assert signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE

    def test_ambiguous_bare_selector_with_stable_alternative_still_requires_persistence(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.synthesized_block_offered_goal_complete = True
        ctx.scout_trajectory = [{"tool_name": "click", "selector": "button"}]
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="lookup",
            reason_code="ambiguous_bare_selector",
            selector="button",
            source_url="https://example.com",
            selector_alternatives=[{"tool_name": "click", "selector": 'role=button[name="Search"]'}],
        )

        signal = synthesized_block_persistence_signal(ctx, "evaluate")

        assert isinstance(signal, CopilotToolBlockerSignal)
        assert signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE

    @pytest.mark.parametrize(
        "tool_name",
        ["update_and_run_blocks", "update_workflow", "fill_credential_field"],
    )
    def test_authoring_offer_keeps_allowed_tools_unblocked(self, tool_name: str) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.synthesized_block_offered_goal_complete = False
        ctx.scout_trajectory = [{"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"}]

        assert synthesized_block_persistence_signal(ctx, tool_name) is None

    def test_authoring_offer_does_not_block_mutating_tool_when_trajectory_changed(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.synthesized_block_offered_goal_complete = False
        ctx.scout_trajectory = [
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
            {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
        ]

        assert synthesized_block_persistence_signal(ctx, "click") is None

    def test_authoring_offer_does_not_block_mutating_tool_without_offer(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = False
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.scout_trajectory = [{"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"}]

        assert synthesized_block_persistence_signal(ctx, "click") is None

    def test_unresolved_recorded_outcome_blocks_page_mutating_tool_until_update_and_run_blocks(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.completion_verification_result = self._unsatisfied_verification()
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            authored_structure_signature="authored:partial",
        )

        signal = synthesized_block_persistence_signal(ctx, "click")

        assert isinstance(signal, CopilotToolBlockerSignal)
        assert signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE
        assert signal.blocked_tool == "click"
        assert signal.cleared_by_tools == frozenset({"update_and_run_blocks"})
        assert "last recorded test outcome" in signal.agent_steering_text
        update_signal = synthesized_block_persistence_signal(ctx, "update_workflow")
        assert isinstance(update_signal, CopilotToolBlockerSignal)
        assert update_signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE
        assert update_signal.blocked_tool == "update_workflow"
        assert update_signal.cleared_by_tools == frozenset({"update_and_run_blocks"})
        assert synthesized_block_persistence_signal(ctx, "update_and_run_blocks") is None
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    @pytest.mark.parametrize(
        "ctx_attrs",
        [
            {"synthesized_block_offered": False, "synthesized_block_offered_trajectory_len": 0},
            {"synthesized_block_offered": True, "synthesized_block_offered_trajectory_len": 0},
            {
                "synthesized_block_offered": True,
                "synthesized_block_offered_trajectory_len": 4,
                "update_workflow_called": True,
            },
        ],
        ids=["no_offer", "zero_trajectory", "update_called"],
    )
    def test_non_persistence_tool_unaffected_without_active_offer_or_after_update(
        self,
        ctx_attrs: dict[str, object],
    ) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        for key, value in ctx_attrs.items():
            setattr(ctx, key, value)

        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    def test_recorded_workflow_update_clears_synthesized_persistence_gate(self) -> None:
        ctx = make_copilot_context(workflow_yaml="title: Updated")
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 2
        ctx.scout_trajectory = [
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
            {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
        ]
        ctx.synthesized_block_offered_goal_complete = True

        _record_workflow_update_result(
            ctx,
            {
                "ok": True,
                "data": {"block_count": 1},
                "_workflow": SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()])),
            },
        )

        assert ctx.update_workflow_called is True
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    def test_recorded_workflow_update_clears_reopened_synthesized_persistence_latch(self) -> None:
        ctx = make_copilot_context(workflow_yaml="title: Updated")
        ctx.synthesized_block_reopened_after_failed_run = True

        _record_workflow_update_result(
            ctx,
            {
                "ok": True,
                "data": {"block_count": 1},
                "_workflow": SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()])),
            },
        )

        assert ctx.synthesized_block_reopened_after_failed_run is False

    def test_stale_shorter_offer_does_not_force_current_goal_complete_trajectory(self) -> None:
        previous_trajectory = [
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
        ]
        trajectory = [
            *previous_trajectory,
            {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)
        ctx.synthesized_block_offered_trajectory_len = len(previous_trajectory)
        ctx.synthesized_block_offered_goal_complete = False

        assert synthesized_trajectory_is_goal_complete(ctx) is True
        assert _should_force_synthesized_block_persistence(ctx) is False
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    def test_refreshed_goal_complete_offer_forces_current_trajectory(self) -> None:
        trajectory = [
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
            {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)

        assert ctx.synthesized_block_offered_trajectory_len == len(trajectory)
        assert ctx.synthesized_block_offered_goal_complete is True
        assert _should_force_synthesized_block_persistence(ctx) is True
        assert synthesized_block_persistence_signal(ctx, "evaluate") is not None

    def test_failed_verified_run_with_new_commit_reopens_synthesized_persistence_gate(self) -> None:
        previous_trajectory = [
            {
                "tool_name": "fill_credential_field",
                "selector": "#username",
                "credential_id": "cred_1",
                "credential_field": "username",
            },
            {"tool_name": "click", "selector": "button[data-action='login']", "accessible_name": "Log in"},
        ]
        trajectory = [
            *previous_trajectory,
            {"tool_name": "click", "selector": "button[data-action='businessToggle']", "accessible_name": "Business"},
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)
        ctx.update_workflow_called = True
        ctx.test_after_update_done = True
        ctx.last_test_ok = False
        ctx.completion_verification_result = self._unsatisfied_verification()
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = len(previous_trajectory)
        ctx.synthesized_block_offered_goal_complete = True

        assert synthesized_persistence_reopened_after_failed_run(ctx) is True
        assert _should_force_synthesized_block_persistence(ctx) is False
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

        assert _maybe_synthesized_block_offer_msg(ctx) is not None
        assert ctx.synthesized_block_offered_trajectory_len == len(trajectory)
        assert _should_force_synthesized_block_persistence(ctx) is True
        assert synthesized_block_persistence_signal(ctx, "evaluate") is not None

    def test_failed_verified_run_without_new_commit_keeps_synthesized_persistence_gate_clear(self) -> None:
        previous_trajectory = [
            {
                "tool_name": "fill_credential_field",
                "selector": "#username",
                "credential_id": "cred_1",
                "credential_field": "username",
            },
            {"tool_name": "click", "selector": "button[data-action='login']", "accessible_name": "Log in"},
        ]
        trajectory = [
            *previous_trajectory,
            {
                "tool_name": "fill_credential_field",
                "selector": "#password",
                "credential_id": "cred_1",
                "credential_field": "password",
            },
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)
        ctx.update_workflow_called = True
        ctx.test_after_update_done = True
        ctx.last_test_ok = False
        ctx.completion_verification_result = self._unsatisfied_verification()
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = len(previous_trajectory)
        ctx.synthesized_block_offered_goal_complete = True

        assert synthesized_persistence_reopened_after_failed_run(ctx) is False
        assert _should_force_synthesized_block_persistence(ctx) is False
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    def test_diagnose_offer_does_not_block_non_persistence_tool(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.DIAGNOSE,
            authority=TurnIntentAuthority(may_read_run_context=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 4

        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    @pytest.mark.parametrize(
        "trajectory, download_target, expected_complete",
        [
            ([], None, False),
            ([{"tool_name": "click", "selector": "a.home", "accessible_name": "Home"}], None, False),
            ([{"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"}], None, False),
            (
                [
                    {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
                    {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
                ],
                None,
                True,
            ),
            (
                [
                    {"tool_name": "select_option", "selector": "select#state", "value": "CA"},
                    {"tool_name": "click", "selector": "button[type='submit']", "accessible_name": "Continue"},
                ],
                None,
                True,
            ),
            (
                [
                    {"tool_name": "select_option", "selector": "select#state", "value": ""},
                    {"tool_name": "click", "selector": "button[type='submit']", "accessible_name": "Continue"},
                ],
                None,
                False,
            ),
            (
                [
                    {"tool_name": "fill_credential_field", "credential_field": "username"},
                    {"tool_name": "click", "selector": "button[type='submit']", "accessible_name": "Log in"},
                ],
                None,
                False,
            ),
            (
                [
                    {"tool_name": "fill_credential_field", "credential_id": "cred", "credential_field": "username"},
                    {"tool_name": "fill_credential_field", "credential_id": "cred", "credential_field": "password"},
                    {"tool_name": "click", "selector": "button[type='submit']", "accessible_name": "Log in"},
                ],
                None,
                True,
            ),
            (
                [
                    {"tool_name": "fill_credential_field", "credential_id": "cred", "credential_field": "username"},
                    {"tool_name": "click", "selector": "button"},
                ],
                None,
                False,
            ),
            (
                [
                    {"tool_name": "fill_credential_field", "credential_id": "cred", "credential_field": "username"},
                    {"tool_name": "fill_credential_field", "credential_id": "cred", "credential_field": "password"},
                    {"tool_name": "click", "selector": "button[type='submit']", "accessible_name": "Log in"},
                    {"tool_name": "type_text", "selector": "input[name='amount']", "accessible_name": "Amount"},
                    {"tool_name": "click", "selector": "button[data-action='pay']", "accessible_name": "Pay"},
                ],
                None,
                True,
            ),
            (
                [{"tool_name": "click", "selector": "a.report", "accessible_name": "Report"}],
                ReachedDownloadTarget(
                    selector="a.report",
                    affordance_text="Report",
                    download_kind="extension",
                    source_step="trajectory_recency",
                    already_registered=False,
                ),
                True,
            ),
        ],
        ids=[
            "empty",
            "navigate_only",
            "type_text_only",
            "type_text_then_commit",
            "select_with_value_then_commit",
            "select_empty_value_dropped",
            "credential_missing_id_dropped",
            "valid_login_fill_and_submit",
            "valid_fill_generic_opener_only",
            "post_login_durable_entry_then_commit",
            "reached_download_target",
        ],
    )
    def test_goal_completeness_drives_force_and_blocker_in_lockstep(
        self,
        trajectory: list[dict[str, object]],
        download_target: ReachedDownloadTarget | None,
        expected_complete: bool,
    ) -> None:
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=download_target)

        assert synthesized_trajectory_is_goal_complete(ctx) is expected_complete
        assert _should_force_synthesized_block_persistence(ctx) is expected_complete
        blocked = synthesized_block_persistence_signal(ctx, "evaluate") is not None
        assert blocked is expected_complete

    def test_extract_shaped_lookup_submit_is_goal_complete_without_extract_step(self) -> None:
        trajectory = [
            {"tool_name": "type_text", "selector": "input[name='reference']", "accessible_name": "Reference"},
            {"tool_name": "click", "selector": "button[data-action='lookup']", "accessible_name": "Look up"},
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)

        assert not any(
            str(item.get("tool_name") or "") in {"extract", "get_run_results", "evaluate"} for item in trajectory
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is True
        assert _should_force_synthesized_block_persistence(ctx) is True

    @pytest.mark.parametrize(
        "guard_attrs",
        [
            {"update_workflow_called": True},
            {"synthesized_block_offered": False},
            {"block_authoring_policy": BlockAuthoringPolicy.STANDARD},
        ],
        ids=["already_authored", "not_offered", "policy_not_code_only"],
    )
    def test_early_guards_never_force_even_when_goal_complete(self, guard_attrs: dict[str, object]) -> None:
        trajectory = [
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
            {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)
        for key, value in guard_attrs.items():
            setattr(ctx, key, value)

        assert _should_force_synthesized_block_persistence(ctx) is False
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    def test_diagnose_turn_intent_never_forces_even_when_goal_complete(self) -> None:
        trajectory = [
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
            {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.DIAGNOSE,
            authority=TurnIntentAuthority(may_read_run_context=True),
        )

        assert _should_force_synthesized_block_persistence(ctx) is False
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    def test_update_only_turn_intent_never_forces_update_and_run_blocks_even_when_goal_complete(self) -> None:
        trajectory = [
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
            {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.DRAFT_ONLY,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=False),
        )

        assert synthesized_trajectory_is_goal_complete(ctx) is True
        assert _should_force_synthesized_block_persistence(ctx) is False
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    def test_goal_complete_commit_refreshes_offer_below_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement.synthesize_code_block",
            lambda *args, **kwargs: SynthesizedCodeBlock(code="await page.click('button')"),
        )
        trajectory = [
            {"tool_name": "click", "selector": "a.home", "accessible_name": "Home"},
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
            {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 2
        ctx.synthesized_block_offered_goal_complete = False
        assert len(trajectory) < 2 + SYNTHESIZED_OFFER_REFRESH_STEP_THRESHOLD

        message = _maybe_synthesized_block_offer_msg(ctx)

        assert message is not None
        assert ctx.synthesized_block_offered_trajectory_len == len(trajectory)
        assert ctx.synthesized_block_offered_goal_complete is True

    def test_goal_complete_offer_refresh_suppresses_near_duplicate_followup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement.synthesize_code_block",
            lambda *args, **kwargs: SynthesizedCodeBlock(code="await page.click('button')"),
        )
        trajectory = [
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
            {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
            {"tool_name": "click", "selector": "button[data-action='open']", "accessible_name": "Open"},
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 2
        ctx.synthesized_block_offered_goal_complete = True
        assert len(trajectory) < 2 + SYNTHESIZED_OFFER_REFRESH_STEP_THRESHOLD

        message = _maybe_synthesized_block_offer_msg(ctx)

        assert message is None
        assert ctx.synthesized_block_offered_trajectory_len == 2

    def test_failed_verified_run_with_new_commit_refreshes_offer_below_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement.synthesize_code_block",
            lambda *args, **kwargs: SynthesizedCodeBlock(code="await page.click('button')"),
        )
        previous_trajectory = [
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
            {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
        ]
        trajectory = [
            *previous_trajectory,
            {"tool_name": "click", "selector": "button[data-action='details']", "accessible_name": "Details"},
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)
        ctx.update_workflow_called = True
        ctx.test_after_update_done = True
        ctx.last_test_ok = False
        ctx.completion_verification_result = self._unsatisfied_verification()
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = len(previous_trajectory)
        ctx.synthesized_block_offered_goal_complete = True
        assert len(trajectory) < len(previous_trajectory) + SYNTHESIZED_OFFER_REFRESH_STEP_THRESHOLD

        message = _maybe_synthesized_block_offer_msg(ctx)

        assert message is not None
        assert ctx.synthesized_block_offered_trajectory_len == len(trajectory)
        assert ctx.synthesized_block_reopened_after_failed_run is True

    def test_sub_threshold_offer_stays_suppressed_when_not_goal_complete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement.synthesize_code_block",
            lambda *args, **kwargs: SynthesizedCodeBlock(code="await page.click('button')"),
        )
        trajectory = [
            {"tool_name": "click", "selector": "a.home", "accessible_name": "Home"},
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        assert len(trajectory) < 1 + SYNTHESIZED_OFFER_REFRESH_STEP_THRESHOLD

        message = _maybe_synthesized_block_offer_msg(ctx)

        assert message is None
        assert ctx.synthesized_block_offered_trajectory_len == 1

    def test_terminal_blocker_replaces_nonterminal_synthesized_persistence_blocker(self) -> None:
        ctx = _Ctx()
        persistence_signal = CopilotToolBlockerSignal(
            blocker_kind="tool_error",
            agent_steering_text="Persist the drafted workflow before more scouting.",
            user_facing_reason="I need to save and test the drafted workflow before scouting more.",
            recovery_hint="retry_with_different_tool",
            cleared_by_tools=frozenset({"update_and_run_blocks"}),
            preserves_workflow_draft=True,
            renders_final_reply=False,
            internal_reason_code=SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE,
            blocked_tool="click",
        )
        terminal_signal = CopilotToolBlockerSignal(
            blocker_kind="loop_detected",
            agent_steering_text="Report that repeated repair attempts did not verify the workflow outcome.",
            user_facing_reason="The workflow was saved and tested, but repeated repairs did not reach the required review page.",
            recovery_hint="report_blocker_to_user",
            cleared_by_tools=frozenset(),
            preserves_workflow_draft=True,
            renders_final_reply=True,
            internal_reason_code="repair_ceiling_reached",
            blocked_tool="update_and_run_blocks",
        )

        stash_blocker_signal(ctx, persistence_signal)
        stash_blocker_signal(ctx, terminal_signal)

        assert ctx.blocker_signal is terminal_signal


# ---------------------------------------------------------------------------
# _is_meaningful_extracted_data
# ---------------------------------------------------------------------------


def test_meaningful_data_none() -> None:
    assert _is_meaningful_extracted_data(None) is False


def test_meaningful_data_empty_dict() -> None:
    assert _is_meaningful_extracted_data({}) is False


def test_meaningful_data_all_null_dict() -> None:
    # The regression: {"price": None} used to count as meaningful because
    # the dict itself is truthy. It must NOT count as meaningful.
    assert _is_meaningful_extracted_data({"price": None}) is False


def test_meaningful_data_nested_all_null() -> None:
    assert _is_meaningful_extracted_data({"a": None, "b": {"c": None}}) is False


def test_meaningful_data_one_real_value() -> None:
    assert _is_meaningful_extracted_data({"price": "260.48", "other": None}) is True


def test_meaningful_data_empty_list() -> None:
    assert _is_meaningful_extracted_data([]) is False


def test_meaningful_data_list_of_nulls() -> None:
    assert _is_meaningful_extracted_data([None, None]) is False


def test_meaningful_data_scalar_zero() -> None:
    # A literal 0 is still meaningful output — it's a value, not absence of data.
    assert _is_meaningful_extracted_data(0) is True


def test_meaningful_data_empty_string() -> None:
    assert _is_meaningful_extracted_data("") is False


def test_meaningful_data_string() -> None:
    assert _is_meaningful_extracted_data("$260.48") is True


def test_unrecoverable_browser_session_error_stops_after_second_failure() -> None:
    from skyvern.forge.sdk.copilot.enforcement import (
        CopilotUnrecoverableToolError,
        _maybe_raise_unrecoverable_tool_error,
    )

    ctx = SimpleNamespace(last_artifact_health_blocker_reason=None, completion_verification_result=None)
    output = {"ok": False, "error": "Browser session not found while taking screenshot (404)."}

    _maybe_raise_unrecoverable_tool_error(ctx, "get_browser_screenshot", output)
    assert ctx.unrecoverable_tool_error_streak_count == 1

    with pytest.raises(CopilotUnrecoverableToolError) as exc_info:
        _maybe_raise_unrecoverable_tool_error(ctx, "get_browser_screenshot", output)

    assert "Browser session not found" in str(exc_info.value)
    assert ctx.unrecoverable_tool_error_streak_count == 2
    contract = ctx.latest_diagnosis_repair_contract
    assert contract.repair_decision.next_action == "stop"
    assert contract.verification_result.remaining_blocker == "Browser session not found while taking screenshot (404)."


def test_unrecoverable_tool_error_ignores_regular_website_404() -> None:
    from skyvern.forge.sdk.copilot.enforcement import _maybe_raise_unrecoverable_tool_error

    ctx = SimpleNamespace()

    _maybe_raise_unrecoverable_tool_error(
        ctx,
        "navigate_browser",
        {"ok": False, "error": "The page returned HTTP 404 page not found."},
    )

    assert getattr(ctx, "unrecoverable_tool_error_streak_count", 0) == 0
    assert getattr(ctx, "latest_diagnosis_repair_contract", None) is None


def test_unrecoverable_contract_stop_preempts_failed_test_nudge() -> None:
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import build_diagnosis_repair_contract
    from skyvern.forge.sdk.copilot.enforcement import CopilotUnrecoverableToolError, _check_enforcement

    ctx = _Ctx()
    ctx.last_test_ok = False
    reason = "Browser session not found while running blocks (404)."
    ctx.latest_diagnosis_repair_contract = build_diagnosis_repair_contract(
        source_tool="update_and_run_blocks",
        result={
            "ok": False,
            "error": reason,
            "data": {
                "overall_status": "aborted",
                "failure_reason": reason,
                "failure_categories": [{"category": "UNRECOVERABLE_TOOL_ERROR"}],
            },
        },
        ctx=ctx,
    )

    with pytest.raises(CopilotUnrecoverableToolError):
        _check_enforcement(ctx)

    assert ctx.failed_test_nudge_count == 0


# ---------------------------------------------------------------------------
# _analyze_run_blocks — envelope-unwrap for EXTRACTION blocks
#
# ExtractionBlock stores TaskOutput.from_task() on block.output. Envelope
# fields (task_id, status, *_screenshot_artifact_ids) are always populated on
# a completed run and would short-circuit _is_meaningful_extracted_data to
# True even when the real payload fields (extracted_information,
# downloaded_files, downloaded_file_urls) are empty. The meaningful-data
# check must judge against the payload slice, not the envelope.
# ---------------------------------------------------------------------------


_EMPTY_EXTRACTION_ENVELOPE: dict[str, Any] = {
    "task_id": "tsk_00000000000000000001",
    "status": "completed",
    "extracted_information": [],
    "failure_reason": None,
    "errors": [],
    "failure_category": None,
    "downloaded_files": [],
    "downloaded_file_urls": None,
    "task_screenshots": None,
    "workflow_screenshots": None,
    "task_screenshot_artifact_ids": ["a_00000000000000000001", "a_00000000000000000002"],
    "workflow_screenshot_artifact_ids": ["a_00000000000000000001", "a_00000000000000000003"],
}


def _run_result(blocks: list[dict[str, Any]], ok: bool = True) -> dict[str, Any]:
    return {"ok": ok, "data": {"blocks": blocks}}


def _envelope(**overrides: Any) -> dict[str, Any]:
    """Return a fresh copy of the empty-extraction envelope with field overrides."""
    return {**_EMPTY_EXTRACTION_ENVELOPE, **overrides}


def _extraction_block(extracted_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": "extract_flights",
        "block_type": "EXTRACTION",
        "status": "completed",
        "extracted_data": extracted_data,
    }


def _text_prompt_block(extracted_data: Any) -> dict[str, Any]:
    return {
        "label": "summarize",
        "block_type": "TEXT_PROMPT",
        "status": "completed",
        "extracted_data": extracted_data,
    }


# Case id -> (envelope overrides, expected empty_data_blocks)
#
# empty_payload_trace_repro: extracted_information=[], downloaded_files=[],
#   downloaded_file_urls=None, envelope metadata populated. Envelope-as-a-whole
#   is truthy; real payload is empty; gate must flip. (SKY-9143 repro.)
# download_only_files / download_only_urls: legitimate extraction success where the
#   block produced files but no structured payload — must NOT flip the gate.
_EXTRACTION_ENVELOPE_CASES: list[tuple[str, dict[str, Any], bool]] = [
    ("empty_payload_trace_repro", {}, True),
    ("real_extraction", {"extracted_information": [{"price": "260.48"}]}, False),
    (
        "nested_code_output_record",
        {
            "extracted_information": [],
            "extract_record_status_info_output": {
                "entity_found": True,
                "entity_name": "Jordan Example",
                "record_number": "1234567890",
                "items": [
                    {
                        "item_name": "Sample Practice",
                        "address": "100 Main St, Example City, ST 12345",
                        "status": "Active",
                    }
                ],
                "overall_status": "Active",
            },
        },
        False,
    ),
    (
        "download_only_files",
        {"downloaded_files": [{"url": "https://example.com/a.pdf", "checksum": "abc123"}]},
        False,
    ),
    (
        "download_only_urls",
        {"extracted_information": None, "downloaded_file_urls": ["https://example.com/a.pdf"]},
        False,
    ),
]


@pytest.mark.parametrize(
    "overrides,expected_empty",
    [(ovr, exp) for _, ovr, exp in _EXTRACTION_ENVELOPE_CASES],
    ids=[case_id for case_id, _, _ in _EXTRACTION_ENVELOPE_CASES],
)
def test_analyze_extraction_envelope(overrides: dict[str, Any], expected_empty: bool) -> None:
    _, empty, _ = _analyze_run_blocks(_run_result([_extraction_block(_envelope(**overrides))]))
    assert empty is expected_empty


def test_analyze_text_prompt_default_schema_is_not_empty() -> None:
    # TEXT_PROMPT blocks return the raw LLM response dict (no Task envelope).
    # Default schema is {"llm_response": "<text>"}.
    _, empty, _ = _analyze_run_blocks(_run_result([_text_prompt_block({"llm_response": "the sentiment is positive"})]))
    assert empty is False


def test_analyze_text_prompt_user_schema_named_extracted_information_is_not_sliced() -> None:
    # Guard against a too-broad unwrap: a user's json_schema may name a
    # top-level field "extracted_information". The helper must not mistake
    # that for an EXTRACTION envelope and discard sibling fields.
    block = _text_prompt_block({"extracted_information": "ignored because this is TEXT_PROMPT", "summary": "x"})
    _, empty, _ = _analyze_run_blocks(_run_result([block]))
    assert empty is False


def test_analyze_text_prompt_all_null_is_empty() -> None:
    # Symmetric to {"price": None} — a text-prompt response with all-null
    # fields counts as no meaningful output.
    _, empty, _ = _analyze_run_blocks(_run_result([_text_prompt_block({"summary": None})]))
    assert empty is True


# ---------------------------------------------------------------------------
# _record_run_blocks_result — end-to-end flip of last_test_ok on empty envelope
# ---------------------------------------------------------------------------


def _fresh_ctx_for_record() -> SimpleNamespace:
    """SimpleNamespace shaped for _record_run_blocks_result + update_repeated_failure_state.

    Mirrors the AgentContext field defaults the function under test reads directly,
    so the stub populates the interesting fields without tripping AttributeError on
    the downstream update_repeated_failure_state call.
    """
    return SimpleNamespace(
        code_artifact_metadata={},
        composition_page_evidence=None,
        unbound_required_parameter_keys=[],
        last_test_ok=True,
        last_test_failure_reason=None,
        last_test_suspicious_success=False,
        last_test_anti_bot=None,
        last_failure_category_top=None,
        last_test_non_retriable_nav_error=None,
        failed_test_nudge_count=0,
        last_failed_workflow_yaml=None,
        last_good_workflow=None,
        last_good_workflow_yaml=None,
        non_retriable_nav_error_last_emitted_signature=None,
        workflow_yaml=None,
        last_workflow=None,
        last_workflow_yaml=None,
        last_frontier_start_label=None,
        last_executed_block_labels=[],
        last_full_workflow_test_ok=False,
        last_unverified_block_labels=[],
        last_failure_signature=None,
        last_frontier_fingerprint=None,
        repeated_failure_streak_count=0,
        repeated_failure_nudge_emitted_at_streak=0,
        pending_action_sequence_fingerprint=None,
        last_action_sequence_fingerprint=None,
        repeated_action_fingerprint_streak_count=0,
        copilot_total_timeout_exceeded=False,
        workflow_verification_evidence=WorkflowVerificationEvidence(),
        output_contract_pending_run_evidence={},
    )


def test_record_run_blocks_result_flips_last_test_ok_on_empty_extraction_envelope() -> None:
    # End-to-end: a run reporting ok=true but whose sole EXTRACTION block
    # produced the empty envelope must push last_test_ok from True to None,
    # so _verified_workflow_or_none blocks the proposal. This is the user-
    # visible SKY-9143 regression.
    ctx = _fresh_ctx_for_record()
    result = _run_result([_extraction_block(_envelope())])
    _record_run_blocks_result(ctx, result)
    assert ctx.last_test_ok is None
    assert ctx.last_test_suspicious_success is True
    assert ctx.last_test_failure_reason is not None


def test_record_run_blocks_result_does_not_promote_partial_frontier_to_full_workflow() -> None:
    from types import SimpleNamespace

    ctx = _fresh_ctx_for_record()
    ctx.last_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label="open"), SimpleNamespace(label="extract")])
    )
    ctx.last_workflow_yaml = "workflow: yaml"
    ctx.verified_prefix_labels = ["open"]

    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_partial",
            "requested_block_labels": ["open"],
            "executed_block_labels": ["open"],
            "blocks": [{"label": "open", "status": "completed"}],
        },
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is True
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_unverified_block_labels == ["extract"]
    assert ctx.last_good_workflow is None
    assert "unverified workflow blocks remain" in (ctx.last_test_failure_reason or "")


def test_record_run_blocks_result_promotes_when_verified_prefix_covers_workflow() -> None:
    from types import SimpleNamespace

    ctx = _fresh_ctx_for_record()
    ctx.last_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label="open"), SimpleNamespace(label="extract")])
    )
    ctx.last_workflow_yaml = "workflow: yaml"
    ctx.verified_prefix_labels = ["open", "extract"]
    ctx.last_unverified_block_labels = ["stale_extract"]

    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_full",
            "requested_block_labels": ["extract"],
            "executed_block_labels": ["extract"],
            "blocks": [{"label": "extract", "status": "completed", "extracted_data": {"value": "ok"}}],
        },
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is True
    assert ctx.last_full_workflow_test_ok is True
    assert ctx.last_unverified_block_labels == []
    assert ctx.last_good_workflow is ctx.last_workflow
    assert ctx.last_good_workflow_yaml == ctx.last_workflow_yaml


def test_record_run_blocks_result_promotes_structured_record_top_level_output_to_terminal_proposal() -> None:
    ctx = _fresh_ctx_for_record()
    ctx.last_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            blocks=[
                SimpleNamespace(label="open_search_search"),
                SimpleNamespace(label="search_and_open_record_details"),
                SimpleNamespace(label="extract_record_status_record"),
            ]
        )
    )
    ctx.last_workflow_yaml = "title: Record lookup"
    ctx.verified_prefix_labels = ["open_search_search"]
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_structured_record",
            "overall_status": "completed",
            "executed_block_labels": ["extract_record_status_record"],
            "blocks": [
                {
                    "label": "extract_record_status_record",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": {"extracted_information": []},
                }
            ],
            "output": {
                "search_and_open_record_details_output": {
                    "found": True,
                    "entity_name": "Jordan Example",
                    "opened_record_details": True,
                    "evidence_text": "Opened Details page for the selected record.",
                },
                "extract_record_status_record_output": {
                    "found": True,
                    "entity_name": "Jordan Example",
                    "record_number": "1234567890",
                    "items": [
                        {
                            "item_label": "Sample Practice",
                            "address": "100 Main St, Example City, ST 12345",
                            "status": "Active",
                        }
                    ],
                    "overall_status": "Active",
                    "evidence_text": "Opened Details page; read Overview/Affiliations items and More Details identifier.",
                },
                "extracted_information": [],
            },
        },
    }
    verification = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=[
            "fallback_record_identity",
            "fallback_record_identifier",
            "fallback_record_groups",
            "fallback_record_status",
        ],
        verdicts=[
            CriterionVerdict(criterion_id=cid, state="satisfied", reason_code="evidence_confirms")
            for cid in (
                "fallback_record_identity",
                "fallback_record_identifier",
                "fallback_record_groups",
                "fallback_record_status",
            )
        ],
    )

    _record_run_blocks_result(ctx, result, completion_verification=verification)

    assert ctx.verified_terminal_proposal_ready is True
    assert ctx.last_test_ok is True
    assert ctx.last_full_workflow_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_failure_reason is None


def test_record_run_blocks_result_resets_stale_verified_terminal_proposal_latch() -> None:
    ctx = _fresh_ctx_for_record()
    ctx.verified_terminal_proposal_ready = True
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_unverified",
            "overall_status": "completed",
            "executed_block_labels": [],
            "blocks": [],
            "output": {},
        },
    }

    _record_run_blocks_result(ctx, result, completion_verification=None)

    assert ctx.verified_terminal_proposal_ready is False


def test_enforcement_stops_after_verified_terminal_proposal() -> None:
    ctx = _Ctx()
    ctx.verified_terminal_proposal_ready = True
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = True
    ctx.completion_verification_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[CriterionVerdict(criterion_id="c0", state="satisfied", reason_code="evidence_confirms")],
    )
    ctx.last_test_suspicious_success = True

    with pytest.raises(CopilotGoalSatisfied):
        _check_enforcement(ctx)


def test_verified_outcome_out_orders_same_turn_involuntary_blocker() -> None:
    ctx = _Ctx()
    ctx.completion_verification_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[CriterionVerdict(criterion_id="c0", state="satisfied", reason_code="evidence_confirms")],
    )
    involuntary = CopilotToolBlockerSignal(
        blocker_kind="loop_detected",
        agent_steering_text="repeated failed step",
        user_facing_reason="I'm stuck retrying the same step.",
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code="loop_detected_repeated_failed_step",
        blocked_tool="update_and_run_blocks",
        extra={},
    )
    ctx.turn_halt = None
    ctx.blocker_signal = involuntary
    ctx.latest_tool_blocker_signal = involuntary

    with pytest.raises(CopilotGoalSatisfied):
        _check_enforcement(ctx)

    assert ctx.turn_halt is None
    assert ctx.blocker_signal is None


def test_record_run_blocks_result_keeps_failure_when_watchdog_cancel_without_timeout() -> None:
    """Stagnation/ceiling cancels mid-session must still set last_test_ok=False
    so the failed-test nudge can fire — only a coincident total timeout softens
    to ``None`` for the unvalidated WIP rescue path."""
    ctx = _fresh_ctx_for_record()
    result = {
        "ok": False,
        "error": "Run ID: wr_stagnation. Stuck.",
        _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY: True,
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is False
    assert ctx.last_test_failure_reason == "Run ID: wr_stagnation. Stuck."


def test_record_run_blocks_result_sets_last_test_ok_none_on_watchdog_cancel_at_timeout() -> None:
    ctx = _fresh_ctx_for_record()
    ctx.copilot_total_timeout_exceeded = True
    result = {
        "ok": False,
        "error": "Run ID: wr_timeout. Outcome is uncertain.",
        _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY: True,
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is None
    assert ctx.last_test_failure_reason == "Run ID: wr_timeout. Outcome is uncertain."


# ---------------------------------------------------------------------------
# Suspicious-success nudge
# ---------------------------------------------------------------------------


def test_suspicious_success_fires_when_flag_set() -> None:
    ctx = _Ctx()
    ctx.last_test_suspicious_success = True
    assert _needs_suspicious_success_nudge(ctx) is True


# ---------------------------------------------------------------------------
# Tool-output pruning
# ---------------------------------------------------------------------------


def _fco(call_id: str, output: str) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def test_recent_outputs_preserved_full() -> None:
    # Build KEEP_RECENT_TOOL_OUTPUTS + 1 items so exactly one is "old".
    items = []
    short = '{"ok":true,"data":{"overall_status":"completed"}}'
    for i in range(KEEP_RECENT_TOOL_OUTPUTS + 1):
        items.append(_fco(f"c{i}", short))

    pruned = _prune_input_list(items)
    # Each recent item is unchanged (they're all short and JSON).
    for i in range(1, KEEP_RECENT_TOOL_OUTPUTS + 1):
        assert pruned[i]["output"] == short


def test_old_large_output_is_summarized() -> None:
    # An older, large JSON tool output gets compressed into a synopsis.
    heavy_payload = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_123",
            "overall_status": "completed",
            "blocks": [
                {
                    "label": "open_quote_page",
                    "status": "completed",
                    "block_type": "GOTO_URL",
                    "extracted_data": None,
                },
                {
                    "label": "extract_stock_price",
                    "status": "completed",
                    "block_type": "EXTRACTION",
                    "extracted_data": {"price": None},
                    "failure_reason": None,
                },
            ],
            "visible_elements_html": "<html>" + ("x" * 4000) + "</html>",
            "screenshot_base64": "[base64 image omitted]",
        },
    }
    heavy_output = json.dumps(heavy_payload)
    assert len(heavy_output) > 4000

    items = [_fco("c_old", heavy_output)]
    # Add enough recent outputs to push the first one out of the recent window.
    for i in range(KEEP_RECENT_TOOL_OUTPUTS):
        items.append(_fco(f"c_new_{i}", '{"ok":true,"data":{"overall_status":"completed"}}'))

    pruned = _prune_input_list(items)
    summarized = pruned[0]["output"]
    # The summary must be drastically shorter than the original.
    assert len(summarized) < 1000
    # It must preserve the key signal fields so the agent can still reason about past calls.
    parsed = json.loads(summarized)
    assert parsed["ok"] is True
    assert parsed["overall_status"] == "completed"
    assert parsed["workflow_run_id"] == "wr_123"
    assert parsed["_summarized"]
    assert len(parsed["blocks"]) == 2
    assert parsed["blocks"][1]["label"] == "extract_stock_price"
    assert parsed["blocks"][1]["status"] == "completed"


def test_summarize_non_json_output_falls_back_to_head_truncation() -> None:
    text = "not-json " * 1000
    result = _summarize_tool_output(text)
    assert len(result) < len(text)
    assert result.startswith("not-json")
    assert "older tool output truncated" in result


def test_summarize_short_output_is_unchanged() -> None:
    assert _summarize_tool_output("small") == "small"


def test_recent_large_output_is_head_truncated_not_summarized() -> None:
    # Big JSON in the most-recent slot should be head-truncated at 2000 chars,
    # NOT replaced with a summary.
    large = '{"ok":true,"data":{"value":"' + ("y" * 3000) + '"}}'
    items = [_fco("c_recent", large)]
    pruned = _prune_input_list(items)
    out = pruned[0]["output"]
    assert out.startswith('{"ok":true,')
    assert out.endswith("\n... [truncated]")
    assert len(out) <= 2020


class TestEnforcement:
    def _make_ctx(self, **overrides: Any) -> Any:
        """Create a mock context with enforcement attributes."""
        ctx = MagicMock()
        ctx.navigate_called = False
        ctx.observation_after_navigate = False
        ctx.navigate_enforcement_done = False
        ctx.update_workflow_called = False
        ctx.test_after_update_done = False
        ctx.post_update_nudge_count = 0
        ctx.coverage_nudge_count = 0
        ctx.format_nudge_count = 0
        ctx.explore_without_workflow_nudge_count = 0
        ctx.last_test_suspicious_success = False
        ctx.last_test_anti_bot = None
        ctx.last_failure_category_top = None
        ctx.per_tool_budget_nudge_count = 0
        for k, v in overrides.items():
            setattr(ctx, k, v)
        return ctx

    @staticmethod
    def _reply_result(user_response: str = "") -> Any:
        """Build a RunResultStreaming-shaped mock whose final_output parses as REPLY."""
        import json

        result = MagicMock()
        result.final_output = json.dumps({"type": "REPLY", "user_response": user_response})
        result.new_items = []
        return result

    @staticmethod
    def _empty_result() -> Any:
        """Build a mock with no final text — triggers the 'not sure how to help' fallback."""
        result = MagicMock()
        result.final_output = None
        result.new_items = []
        return result

    def test_no_enforcement_when_nothing_pending(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx()
        assert _check_enforcement(ctx) is None

    def test_post_navigate_nudge(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(navigate_called=True, observation_after_navigate=False)
        nudge = _check_enforcement(ctx)
        assert nudge is not None
        assert "observe" in nudge.lower() or "inspect" in nudge.lower()
        assert ctx.navigate_enforcement_done is True

    def test_post_navigate_only_fires_once(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            navigate_enforcement_done=True,
        )
        assert _check_enforcement(ctx) is None

    def test_post_update_nudge(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(update_workflow_called=True, test_after_update_done=False)
        nudge = _check_enforcement(ctx)
        assert nudge is not None
        assert "test" in nudge.lower() or "run_blocks" in nudge.lower()

    def test_navigate_takes_priority_over_update(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            update_workflow_called=True,
            test_after_update_done=False,
        )
        nudge = _check_enforcement(ctx)
        assert "observe" in nudge.lower() or "inspect" in nudge.lower()

    def test_intermediate_success_nudge_for_multistep_goal(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=1,
            user_message="Go to france.fr and then download all french regulations",
            coverage_nudge_count=0,
        )
        from skyvern.forge.sdk.copilot.enforcement import POST_INTERMEDIATE_SUCCESS_NUDGE

        # Coverage gate only fires when the model tries to emit a REPLY.
        nudge = _check_enforcement(ctx, self._reply_result("draft response"))
        assert nudge == POST_INTERMEDIATE_SUCCESS_NUDGE
        assert ctx.coverage_nudge_count == 1

    def test_no_intermediate_success_nudge_for_single_step_goal(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=1,
            user_message="Go to france.fr",
            coverage_nudge_count=0,
        )
        assert _check_enforcement(ctx, self._reply_result("done")) is None

    def test_intermediate_success_nudge_fires_for_two_blocks(self) -> None:
        """Key regression: nudge must fire even when block_count > 1."""
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=2,
            user_message="Go to france.fr and then download all french regulations and extract the titles",
            coverage_nudge_count=0,
        )
        from skyvern.forge.sdk.copilot.enforcement import POST_INTERMEDIATE_SUCCESS_NUDGE

        nudge = _check_enforcement(ctx, self._reply_result("two-block draft"))
        assert nudge == POST_INTERMEDIATE_SUCCESS_NUDGE

    def test_intermediate_nudge_respects_global_cap(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import MAX_INTERMEDIATE_NUDGES, _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=2,
            user_message="Go to france.fr and then download all french regulations",
            coverage_nudge_count=MAX_INTERMEDIATE_NUDGES,
        )
        assert _check_enforcement(ctx, self._reply_result("capped")) is None

    def test_intermediate_nudge_does_not_fire_for_ten_plus_blocks(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=10,
            user_message="Go to france.fr and then download all french regulations",
            coverage_nudge_count=0,
        )
        assert _check_enforcement(ctx, self._reply_result("ten blocks")) is None

    def test_ask_question_always_passes_even_with_coverage_gap(self) -> None:
        """Regression guard: ASK_QUESTION must never be blocked by coverage."""
        import json

        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=1,
            user_message="Go to france.fr and then download all french regulations",
            coverage_nudge_count=0,
        )
        ask = MagicMock()
        ask.final_output = json.dumps({"type": "ASK_QUESTION", "user_response": "Which source?"})
        ask.new_items = []
        assert _check_enforcement(ctx, ask) is None

    def test_plain_labeled_ask_question_passes_even_with_coverage_gap(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=1,
            user_message="Go to france.fr and then download all french regulations",
            coverage_nudge_count=0,
        )
        ask = MagicMock()
        ask.final_output = "ASK_QUESTION\nWhich source?"
        ask.new_items = []
        assert _check_enforcement(ctx, ask) is None

    def test_explore_without_workflow_nudge(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE, _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=False,
            test_after_update_done=False,
        )
        nudge = _check_enforcement(ctx)
        assert nudge == POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE
        assert ctx.explore_without_workflow_nudge_count == 1

    def test_explore_without_workflow_not_when_update_called(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import (
            POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE,
            POST_UPDATE_NUDGE,
            _check_enforcement,
        )

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=True,
            test_after_update_done=False,
        )
        nudge = _check_enforcement(ctx)
        assert nudge == POST_UPDATE_NUDGE
        assert nudge != POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE
        assert ctx.explore_without_workflow_nudge_count == 0

    def test_update_without_test_allowed_for_explicit_untested_draft(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            allow_untested_workflow_draft=True,
            update_workflow_called=True,
            test_after_update_done=False,
        )

        assert _check_enforcement(ctx) is None

    def test_explore_without_workflow_not_when_test_done(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE, _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=False,
            test_after_update_done=True,
        )
        nudge = _check_enforcement(ctx)
        assert nudge != POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE

    def test_explore_without_workflow_respects_cap(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import (
            MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES,
            POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE,
            _check_enforcement,
        )

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=False,
            test_after_update_done=False,
            explore_without_workflow_nudge_count=MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES,
        )
        nudge = _check_enforcement(ctx)
        assert nudge != POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE

    def test_explore_without_workflow_not_without_observation(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE, _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            update_workflow_called=False,
            test_after_update_done=False,
        )
        nudge = _check_enforcement(ctx)
        # Should get navigate nudge, not explore-without-workflow
        assert nudge != POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE
        assert ctx.explore_without_workflow_nudge_count == 0

    @pytest.mark.asyncio
    async def test_post_navigate_nudge_does_not_increment_post_update_counter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from skyvern.forge.sdk.copilot.enforcement import run_with_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            update_workflow_called=False,
            post_update_nudge_count=0,
        )
        stream = MagicMock()
        stream.is_disconnected = AsyncMock(return_value=False)

        call_count = {"count": 0}

        # final_output=None + new_items=[] makes extract_final_text return "",
        # which parses to a REPLY fallback — safe for the response-peek path
        # when the state-based branches may or may not short-circuit first.
        fake_result = self._empty_result()
        fake_result.to_input_list.return_value = []

        def fake_run_streamed(*args: Any, **kwargs: Any) -> Any:
            call_count["count"] += 1
            return fake_result

        async def fake_stream_to_sse(result: Any, s: Any, c: Any) -> None:
            # Resolve post-navigate enforcement on second pass.
            if call_count["count"] >= 2:
                c.observation_after_navigate = True

        monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", fake_run_streamed)
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
            fake_stream_to_sse,
        )

        returned = await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
        )
        assert returned is fake_result
        assert ctx.post_update_nudge_count == 0

    @pytest.mark.asyncio
    async def test_post_update_nudge_increments_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.enforcement import run_with_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=False,
            post_update_nudge_count=0,
        )
        stream = MagicMock()
        stream.is_disconnected = AsyncMock(return_value=False)

        call_count = {"count": 0}
        fake_result = self._empty_result()
        fake_result.to_input_list.return_value = []

        def fake_run_streamed(*args: Any, **kwargs: Any) -> Any:
            call_count["count"] += 1
            return fake_result

        async def fake_stream_to_sse(result: Any, s: Any, c: Any) -> None:
            # Resolve post-update enforcement on second pass.
            if call_count["count"] >= 2:
                c.test_after_update_done = True

        monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", fake_run_streamed)
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
            fake_stream_to_sse,
        )

        returned = await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
        )
        assert returned is fake_result
        assert ctx.post_update_nudge_count == 1


class TestGoalLikelyNeedsMoreBlocks:
    @staticmethod
    def _check(user_message: str, block_count: int, completion_contract: str | None = None) -> bool:
        from skyvern.forge.sdk.copilot.enforcement import _goal_likely_needs_more_blocks

        return _goal_likely_needs_more_blocks(user_message, block_count, completion_contract)

    def test_navigate_and_download_needs_two(self) -> None:
        assert self._check("Go to france.fr and then download regulations", 1) is True
        assert self._check("Go to france.fr and then download regulations", 2) is False

    def test_login_search_and_extract_needs_three(self) -> None:
        assert self._check("Login to the site, search for products, and extract prices", 1) is True
        assert self._check("Login to the site, search for products, and extract prices", 2) is True
        assert self._check("Login to the site, search for products, and extract prices", 3) is False

    def test_single_action_does_not_need_more(self) -> None:
        assert self._check("Go to france.fr", 1) is False

    def test_completion_contract_does_not_force_extra_blocks_after_success(self) -> None:
        user_message = "Go to example.com/contact, fill out the form, and submit it."
        assert self._check(user_message, 1, "confirmation banner appears") is False

    def test_completion_contract_still_requires_sequential_blocks(self) -> None:
        user_message = "Go to example.com and then download the report."
        assert self._check(user_message, 1, "download starts") is True
        assert self._check(user_message, 2, "download starts") is False

    def test_sequential_connector_needs_at_least_two(self) -> None:
        assert self._check("Do X and then do Y", 1) is True

    def test_ten_plus_blocks_always_false(self) -> None:
        assert self._check("Go to X and then download Y and extract Z", 10) is False

    def test_non_string_returns_false(self) -> None:
        assert self._check(None, 1) is False  # type: ignore[arg-type]
        assert self._check(123, 1) is False  # type: ignore[arg-type]


def _criterion(output_path: str, outcome: str) -> CompletionCriterion:
    return CompletionCriterion(id=output_path, outcome=outcome, output_path=output_path)


def _turn_state(*criteria: CompletionCriterion) -> SimpleNamespace:
    return SimpleNamespace(decision=SimpleNamespace(criteria=tuple(criteria)))


def _download_target() -> ReachedDownloadTarget:
    return ReachedDownloadTarget(
        selector="a.download",
        affordance_text="Download",
        download_kind="registered",
        source_step="trajectory_recency",
        already_registered=True,
    )


def _entry_commit_trajectory() -> list[dict[str, object]]:
    return [
        {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Order number"},
        {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
    ]


def _author_time_reject_outcome(*output_paths: str) -> RecordedBuildTestOutcome:
    paths = sorted(output_paths)
    return recorded_outcome_from_author_time_reject(
        reason_code="metadata_reject",
        block_labels=["extract_order"],
        structural_payload={
            "reason_code": "recorded_outcome_missing_output_coverage",
            "missing_output_paths": paths,
            "block_labels": ["extract_order"],
            "recorded_reason_code": "outcome_not_demonstrated",
        },
        observed_evidence_summary="missing requested output coverage",
        missing_requested_output_facts=[
            {
                "output_path": path,
                "output_root": path.split(".", 1)[0],
                "reason_code": "recorded_outcome_missing_output_coverage",
                "value_status": "no_typed_value",
            }
            for path in paths
        ],
    )


class TestScoutOutputCoverageGate:
    def _authoring_ctx(self, *criteria: CompletionCriterion) -> _Ctx:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.scout_trajectory = _entry_commit_trajectory()
        ctx.reached_download_target = None
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = len(ctx.scout_trajectory)
        ctx.completion_criteria_turn_state = _turn_state(*criteria)
        ctx.synthesized_block_offered_goal_complete = synthesized_trajectory_is_goal_complete(ctx)
        return ctx

    def test_empty_output_set_falls_through_to_shape_heuristic(self) -> None:
        ctx = self._authoring_ctx()
        assert uncovered_requested_output_paths(ctx) == set()
        assert synthesized_trajectory_is_goal_complete(ctx) is True

    def test_post_run_only_evidence_source_is_exempt_from_pre_run_gate(self) -> None:
        registered = CompletionCriterion(
            id="output.confirmation_number",
            outcome="the confirmation number is registered as a workflow output parameter",
            output_path="output.confirmation_number",
            requested_output_evidence_source="registered_output_parameter",
        )
        ctx = self._authoring_ctx(registered)
        assert uncovered_requested_output_paths(ctx) == set()
        assert synthesized_trajectory_is_goal_complete(ctx) is True

    def test_runtime_output_stays_gated_alongside_exempt_source(self) -> None:
        registered = CompletionCriterion(
            id="output.confirmation_number",
            outcome="the confirmation number is registered as a workflow output parameter",
            output_path="output.confirmation_number",
            requested_output_evidence_source="registered_artifact_content",
        )
        runtime = _criterion("output.document_name", "the order status document name is captured")
        ctx = self._authoring_ctx(registered, runtime)
        assert uncovered_requested_output_paths(ctx) == {"output.document_name"}
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_uncovered_output_keeps_gate_open_and_admits_scout_tools(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        assert uncovered_requested_output_paths(ctx) == {"output.document_name"}
        assert synthesized_trajectory_is_goal_complete(ctx) is False
        assert _should_force_synthesized_block_persistence(ctx) is False
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None
        assert _should_block_mutating_tool_after_synthesized_offer(ctx, "click") is False
        assert synthesized_block_persistence_signal(ctx, "click") is None

    def test_value_bearing_container_covers_path_and_force_fires(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        page_evidence = {
            "result_containers": [
                {"text_excerpt": "Document Name  Resale Certificate 2024 for order 5591"},
            ]
        }
        record_scouted_output_coverage(ctx, page_evidence)
        assert ctx.scouted_output_covered_paths == {"output.document_name"}
        assert uncovered_requested_output_paths(ctx) == set()
        ctx.synthesized_block_offered_goal_complete = synthesized_trajectory_is_goal_complete(ctx)
        assert synthesized_trajectory_is_goal_complete(ctx) is True
        assert _should_force_synthesized_block_persistence(ctx) is True

    def test_empty_shell_selector_tokens_do_not_credit(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        page_evidence = {
            "result_containers": [
                {
                    "selector": "#document-name-table",
                    "row_selector": "tr.document",
                    "text_excerpt": "Search results loaded for widgets",
                }
            ]
        }
        record_scouted_output_coverage(ctx, page_evidence)
        assert uncovered_requested_output_paths(ctx) == {"output.document_name"}

    def test_registered_download_covered_by_reached_download_target(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.downloaded_files", "the downloaded files are captured"))
        ctx.reached_download_target = _download_target()
        assert uncovered_requested_output_paths(ctx) == set()

    def test_download_target_covers_only_registered_download_paths(self) -> None:
        ctx = self._authoring_ctx(
            _criterion("output.downloaded_files", "the downloaded files are captured"),
            _criterion("output.document_name", "the order status document name is captured"),
        )
        ctx.reached_download_target = _download_target()
        assert uncovered_requested_output_paths(ctx) == {"output.document_name"}

    def test_unreachable_output_never_completes_on_long_trajectory(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.scout_trajectory = _entry_commit_trajectory() * 12
        ctx.synthesized_block_offered_trajectory_len = len(ctx.scout_trajectory)
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_none_criteria_source_shapes_are_byte_identical(self) -> None:
        ctx = self._authoring_ctx()
        ctx.completion_criteria_turn_state = None
        assert uncovered_requested_output_paths(ctx) == set()
        ctx.completion_criteria_turn_state = SimpleNamespace(decision=None)
        assert uncovered_requested_output_paths(ctx) == set()

    def test_all_generic_token_path_is_exempt_and_falls_through_to_shape(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.data", "the data is captured"))
        assert uncovered_requested_output_paths(ctx) == set()
        ctx.synthesized_block_offered_goal_complete = synthesized_trajectory_is_goal_complete(ctx)
        assert synthesized_trajectory_is_goal_complete(ctx) is True
        assert _should_force_synthesized_block_persistence(ctx) is True

    def test_generic_path_exemption_keeps_specific_path_gating(self) -> None:
        ctx = self._authoring_ctx(
            _criterion("output.data", "the data is captured"),
            _criterion("output.document_name", "the order status document name is captured"),
        )
        assert uncovered_requested_output_paths(ctx) == {"output.document_name"}

    def test_repair_context_required_goal_value_paths_join_requested_set(self) -> None:
        ctx = self._authoring_ctx()
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="extract_order",
            reason_code="metadata_reject",
            required_goal_value_paths=["document_name"],
        )
        assert uncovered_requested_output_paths(ctx) == {"output.document_name"}
        record_scouted_output_coverage(
            ctx, {"result_containers": [{"text_excerpt": "Document Name  Resale Certificate 2024"}]}
        )
        assert uncovered_requested_output_paths(ctx) == set()

    def test_reopen_fires_after_prior_run_then_author_reject(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.update_workflow_called = True
        ctx.last_run_blocks_workflow_run_id = "wr_prior_run"
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("output.document_name")
        assert _uncovered_output_reject_admits_evaluate(ctx, "evaluate") is True
        assert consume_uncovered_output_reopen_event(ctx) is True
        assert ctx.synthesized_block_reopened_for_output_coverage is True

    def test_stream_recorded_authoring_success_clears_latch_and_steer_key(self) -> None:
        ctx = _Ctx()
        ctx.synthesized_block_reopened_for_output_coverage = True
        ctx.uncovered_output_rescout_steer_key = "steer-key"
        ctx.uncovered_output_rescout_context_key = "context-key"
        _update_enforcement_from_tool(ctx, "update_workflow", {"ok": True, "data": {"block_count": 1}})
        assert ctx.synthesized_block_reopened_for_output_coverage is False
        assert ctx.uncovered_output_rescout_steer_key is None
        assert ctx.uncovered_output_rescout_context_key == "context-key"

    def test_recorded_workflow_update_clears_latch_and_steer_key(self) -> None:
        ctx = make_copilot_context("title: Updated")
        ctx.synthesized_block_reopened_for_output_coverage = True
        ctx.uncovered_output_rescout_steer_key = "steer-key"
        ctx.uncovered_output_rescout_context_key = "context-key"
        _record_workflow_update_result(
            ctx,
            {
                "ok": True,
                "data": {"block_count": 1},
                "_workflow": SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()])),
            },
        )
        assert ctx.synthesized_block_reopened_for_output_coverage is False
        assert ctx.uncovered_output_rescout_steer_key is None
        assert ctx.uncovered_output_rescout_context_key == "context-key"

    def test_accessor_empty_when_no_recorded_outcome(self) -> None:
        assert author_time_reject_missing_output_paths(None) == set()

    def test_fact_paths_canonicalize_into_uncovered_set(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        outcome = _author_time_reject_outcome("output.document_name")
        canonical = {_canonical_output_path(path) for path in author_time_reject_missing_output_paths(outcome)}
        assert canonical & uncovered_requested_output_paths(ctx) == {"output.document_name"}

    def test_admission_allows_evaluate_while_uncovered_output_reject_active(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.update_workflow_called = True
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("output.document_name")
        assert _uncovered_output_reject_admits_evaluate(ctx, "evaluate") is True
        assert _uncovered_output_reject_admits_evaluate(ctx, "evaluate") is True
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    def test_consume_reopen_event_arms_latch_fire_once(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.update_workflow_called = True
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("output.document_name")
        assert consume_uncovered_output_reopen_event(ctx) is True
        assert ctx.synthesized_block_reopened_for_output_coverage is True
        assert synthesized_persistence_reopened(ctx) is True
        assert consume_uncovered_output_reopen_event(ctx) is False

    def test_steer_redirects_reauthor_to_scout_once_then_lets_through(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.update_workflow_called = True
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("output.document_name")
        consume_uncovered_output_reopen_event(ctx)
        steer = uncovered_output_reject_scout_steer_signal(ctx, "update_and_run_blocks")
        assert isinstance(steer, CopilotToolBlockerSignal)
        assert steer.cleared_by_tools == frozenset({"evaluate"})
        assert steer.renders_final_reply is False
        assert "output.document_name" in steer.agent_steering_text
        assert steer.extra["uncovered_output_paths"] == ["output.document_name"]
        assert uncovered_output_reject_scout_steer_signal(ctx, "update_and_run_blocks") is None

    def test_steer_redirects_update_workflow_reauthor_before_clear_consumes_reopen(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.update_workflow_called = True
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("output.document_name")
        consume_uncovered_output_reopen_event(ctx)
        steer = uncovered_output_reject_scout_steer_signal(ctx, "update_workflow")
        assert isinstance(steer, CopilotToolBlockerSignal)
        assert steer.cleared_by_tools == frozenset({"evaluate"})
        assert steer.blocked_tool == "update_workflow"

    def test_steer_inert_without_reopen_latch(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("output.document_name")
        assert uncovered_output_reject_scout_steer_signal(ctx, "update_and_run_blocks") is None

    def test_no_contradictory_blockers_steer_and_force_persist_disjoint(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.update_workflow_called = True
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("output.document_name")
        consume_uncovered_output_reopen_event(ctx)
        assert uncovered_output_reject_scout_steer_signal(ctx, "update_and_run_blocks") is not None
        assert _should_force_synthesized_block_persistence(ctx) is False
        ctx.scouted_output_covered_paths = {"output.document_name"}
        ctx.uncovered_output_rescout_steer_key = None
        assert uncovered_output_reject_scout_steer_signal(ctx, "update_and_run_blocks") is None

    def test_persisted_run_outcome_does_not_trigger_author_reopen(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.completion_verification_result = TestSynthesizedOfferPersistenceGate._unsatisfied_verification()
        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            structural_failure_identity="completion:unsatisfied-output",
            missing_requested_output_facts=[
                {
                    "output_path": "output.document_name",
                    "output_root": "output",
                    "reason_code": "outcome_not_demonstrated",
                    "value_status": "no_typed_value",
                }
            ],
        )
        assert author_time_reject_missing_output_paths(ctx.latest_recorded_build_test_outcome) == set()
        assert _uncovered_output_reject_admits_evaluate(ctx, "evaluate") is False
        assert consume_uncovered_output_reopen_event(ctx) is False
        assert ctx.synthesized_block_reopened_for_output_coverage is False
        assert uncovered_output_reject_scout_steer_signal(ctx, "update_and_run_blocks") is None
        assert isinstance(synthesized_block_persistence_signal(ctx, "click"), CopilotToolBlockerSignal)

    def test_author_reject_inert_when_no_missing_output_facts(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.update_workflow_called = True
        ctx.latest_recorded_build_test_outcome = recorded_outcome_from_author_time_reject(
            reason_code="metadata_reject",
            block_labels=["extract_order"],
            structural_payload={"reason_code": "recorded_outcome_missing_output_coverage"},
        )
        assert _uncovered_output_reject_admits_evaluate(ctx, "evaluate") is False
        assert consume_uncovered_output_reopen_event(ctx) is False
        assert ctx.synthesized_block_reopened_for_output_coverage is False

    def test_author_reject_inert_when_paths_already_covered(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.update_workflow_called = True
        ctx.scouted_output_covered_paths = {"output.document_name"}
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("output.document_name")
        assert _uncovered_output_reject_admits_evaluate(ctx, "evaluate") is False
        assert consume_uncovered_output_reopen_event(ctx) is False

    def test_coverage_reopen_refreshes_synthesized_offer_after_authoring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement.synthesize_code_block",
            lambda *args, **kwargs: SynthesizedCodeBlock(code="await page.click('button')"),
        )
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.update_workflow_called = True
        assert _maybe_synthesized_block_offer_msg(ctx) is None
        ctx.synthesized_block_reopened_for_output_coverage = True
        assert _maybe_synthesized_block_offer_msg(ctx) is not None

    def test_post_hook_failure_rolls_back_coverage_credit(self) -> None:
        assert "scouted_output_covered_paths" in _POST_HOOK_CONTEXT_ROLLBACK_FIELDS
        assert "synthesized_block_reopened_for_output_coverage" in _POST_HOOK_CONTEXT_ROLLBACK_FIELDS
        ctx = _Ctx()
        ctx.scouted_output_covered_paths = {"output.document_name"}
        ctx.synthesized_block_reopened_for_output_coverage = False
        snapshot = _snapshot_post_hook_context(ctx)
        ctx.scouted_output_covered_paths.add("output.leaked")
        ctx.synthesized_block_reopened_for_output_coverage = True
        _restore_post_hook_context(ctx, snapshot)
        assert ctx.scouted_output_covered_paths == {"output.document_name"}
        assert ctx.synthesized_block_reopened_for_output_coverage is False


class TestAdvisoryRunDispatchForceLane:
    """A granted output-contract advisory run is forced onto update_and_run_blocks through the same
    tool_choice forcing lane as the synthesized-persistence force, and releases on consume or terminal."""

    def _granted_ctx(self) -> _Ctx:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.turn_halt = None
        ctx.blocker_signal = None
        ctx.output_contract_actuation_by_signature = {"sig_a": OutputContractAdvisoryState.GRANTED}
        return ctx

    def test_granted_advisory_forces_run_dispatch(self) -> None:
        assert _should_force_advisory_run_dispatch(self._granted_ctx()) is True

    def test_consumed_advisory_releases_the_force(self) -> None:
        ctx = self._granted_ctx()
        ctx.output_contract_actuation_by_signature = {"sig_a": OutputContractAdvisoryState.CONSUMED}
        assert _should_force_advisory_run_dispatch(ctx) is False

    def test_no_grant_does_not_force(self) -> None:
        ctx = self._granted_ctx()
        ctx.output_contract_actuation_by_signature = {}
        assert _should_force_advisory_run_dispatch(ctx) is False

    def test_authority_forbidding_run_never_forces(self) -> None:
        ctx = self._granted_ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=False),
        )
        assert _should_force_advisory_run_dispatch(ctx) is False

    def test_held_genuinely_terminal_blocker_releases_the_force(self) -> None:
        ctx = self._granted_ctx()
        ctx.blocker_signal = CopilotToolBlockerSignal(
            blocker_kind="tool_error",
            agent_steering_text="",
            user_facing_reason="",
            recovery_hint="stop",
            internal_reason_code=OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE,
        )
        assert _should_force_advisory_run_dispatch(ctx) is False

    def test_non_code_only_policy_does_not_force(self) -> None:
        ctx = self._granted_ctx()
        ctx.block_authoring_policy = None
        assert _should_force_advisory_run_dispatch(ctx) is False

    def test_granted_advisory_survives_model_churn_and_stays_force_eligible(self) -> None:
        ctx = self._granted_ctx()
        ctx.latest_tool_blocker_signal = None
        ctx.tool_blocker_signals = []
        ctx.output_contract_actuation_count_by_signature = {}
        ctx.output_contract_run_output_observed_by_signature = {}
        ctx.output_contract_page_extraction_imposed_by_signature = {}
        ctx.output_contract_pending_run_evidence = {"sig_a": ["output.confirmation_number"]}
        churn = CopilotToolBlockerSignal(
            blocker_kind="loop_detected",
            agent_steering_text="",
            user_facing_reason="",
            recovery_hint="stop",
            internal_reason_code="code_authoring_guardrail_churn",
        )
        stash_turn_halt_from_blocker_signal(ctx, churn, source="enforcement_backstop")
        stash_turn_halt_from_blocker_signal(ctx, churn, source="enforcement_backstop")
        assert ctx.turn_halt is None
        assert ctx.output_contract_actuation_by_signature["sig_a"] == OutputContractAdvisoryState.GRANTED
        assert _should_force_advisory_run_dispatch(ctx) is True
