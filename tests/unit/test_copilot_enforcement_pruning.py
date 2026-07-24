"""Tests for enforcement pruning and null-data handling.

These cover three regressions observed in trace 019d7b5c884dff0ff648680b9f31f715:
  1. Extraction returning all-null fields was treated as success.
  2. Context grew linearly because old tool outputs kept full content.
  3. No escalation when the agent looped on the same null-data failure.
"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents import RunConfig
from structlog.testing import capture_logs

from skyvern.config import Settings, settings
from skyvern.forge.sdk.copilot.blocker_signal import (
    UNCOVERED_OUTPUT_RESCOUT_STEER_REASON_CODE,
    CopilotToolBlockerSignal,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.build_test_outcome import (
    PostRunPagePathFailure,
    PostRunPagePathTarget,
    RecordedBuildTestOutcome,
    _post_run_page_path_failure,
    author_time_reject_missing_output_paths,
    bind_post_run_page_path_failure,
    recorded_outcome_from_author_time_reject,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import SynthesizedCodeBlock
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.composition_evidence import parse_composition_html
from skyvern.forge.sdk.copilot.config import (
    SYNTHESIZED_OFFER_REFRESH_STEP_THRESHOLD,
    BlockAuthoringPolicy,
    CopilotConfig,
)
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
    _recover_from_context_overflow,
    _requested_output_paths_for_ctx,
    _should_block_mutating_tool_after_synthesized_offer,
    _should_force_advisory_run_dispatch,
    _should_force_synthesized_block_persistence,
    _summarize_tool_output,
    _uncovered_output_reject_admits_evaluate,
    aggressive_prune,
    arm_credential_scout_reopen,
    consume_uncovered_output_reopen_event,
    mint_scout_observation_contract_for_ctx,
    pre_run_gated_outputs_without_path,
    record_scouted_output_coverage,
    requested_scalar_output_extraction_plan,
    run_with_enforcement,
    synthesized_block_persistence_signal,
    synthesized_goal_completion_landing_pending,
    synthesized_persistence_reopened,
    synthesized_persistence_reopened_after_failed_run,
    synthesized_trajectory_is_goal_complete,
    synthesized_trajectory_reaches_goal,
    uncovered_output_reject_scout_steer_signal,
    uncovered_requested_output_paths,
)
from skyvern.forge.sdk.copilot.mcp_adapter import (
    _POST_HOOK_CONTEXT_ROLLBACK_FIELDS,
    SchemaOverlay,
    SkyvernOverlayMCPServer,
    _restore_post_hook_context,
    _snapshot_post_hook_context,
)
from skyvern.forge.sdk.copilot.output_contracts import (
    OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE,
    OutputContractAdvisoryState,
)
from skyvern.forge.sdk.copilot.output_extraction_plan import ShapeExpectation, ValueCardinality, ValueShape
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.runtime import NeverCapturedObligation
from skyvern.forge.sdk.copilot.streaming_adapter import _update_enforcement_from_tool
from skyvern.forge.sdk.copilot.tools import (
    _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
    _analyze_run_blocks,
    _click_post_hook,
    _is_meaningful_extracted_data,
    _press_key_post_hook,
    _record_run_blocks_result,
    _record_workflow_update_result,
    mcp_hooks,
)
from skyvern.forge.sdk.copilot.tools.page_observation import _record_composition_page_observation
from skyvern.forge.sdk.copilot.tools.scouting import (
    _MAX_SCOUTED_INTERACTIONS,
    _capped_with_eviction_accounting,
    _mark_post_run_page_observed,
    _record_scout_page_observation,
)
from skyvern.forge.sdk.copilot.turn_halt import stash_turn_halt_from_blocker_signal
from skyvern.forge.sdk.copilot.turn_intent import RequiredContextKey, TurnIntent, TurnIntentAuthority, TurnIntentMode
from skyvern.forge.sdk.copilot.turn_ownership import TurnClaimant, current_turn_owner
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from tests.unit.conftest import make_copilot_context


@pytest.fixture(autouse=True)
def _disable_author_time_gate_log_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", False)


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
        self.persisted_draft_browser_calls = None
        self.scouted_spine_checkpoint_fired = False
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
        self.synthesized_block_reopened_for_credential_scout = False
        self.synthesized_block_reopened_for_capture_obligation = False
        self.never_captured_obligation = None
        self.credential_scout_rescout_context_key = None
        self.synthesized_goal_complete_landed = False
        self.impose_synthesized_code_block = False
        self.scouted_output_covered_paths: set[str] = set()
        self.scout_observed_terminal_criterion_ids: set[str] = set()
        self.scout_observation_contract: object | None = None
        self.flow_evidence: list[dict[str, object]] = []
        self.composition_page_evidence = None
        self.copilot_config: CopilotConfig | None = None
        self.uncovered_output_rescout_context_key = None
        self.uncovered_output_rescout_steer_key = None
        self.latest_recorded_build_test_outcome = None
        self.last_run_blocks_workflow_run_id = None
        self.post_run_page_observation_tool = None
        self.post_run_page_observation_url = None
        self.post_run_page_observation_workflow_run_id = None
        self.post_run_page_observation_after_failed_test = False
        self.post_run_page_observation_generation = 0
        self.post_run_page_path_interaction_window = None
        self.workflow_yaml = ""
        self.workflow_verification_evidence = WorkflowVerificationEvidence()
        self.completion_criteria_turn_state = None
        self.reached_download_target: ReachedDownloadTarget | None = None
        self.author_time_gate_log_only_ids: frozenset[str] = frozenset()
        self.author_time_gate_ablation_events = []
        self.request_policy = None
        self.blocker_signal = None
        self.blocker_signal_claimant = None
        self.turn_halt = None
        self.turn_ownership = None
        self.gate_precedence_conflict_events: list[object] = []
        self.output_contract_actuation_by_signature: dict[str, object] = {}
        self.output_contract_actuation_count_by_signature: dict[str, int] = {}


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

    def test_actuation_obligation_admits_required_fill_tool_during_persistence_offer(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
            required_context={RequiredContextKey.BROWSER_STATE},
        )
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="form-submit",
                    outcome="form fields are filled",
                    kind="terminal_action",
                    terminal_action_family="form",
                )
            ],
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.scout_trajectory = [{"tool_name": "click", "selector": "button.start", "accessible_name": "Start"}]

        assert synthesized_block_persistence_signal(ctx, "type_text") is None
        click_signal = synthesized_block_persistence_signal(ctx, "click")
        assert isinstance(click_signal, CopilotToolBlockerSignal)
        assert click_signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE

    def test_actuation_obligation_fill_admission_registers_precedence_claim(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
            required_context={RequiredContextKey.BROWSER_STATE},
        )
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="form-submit",
                    outcome="form fields are filled",
                    kind="terminal_action",
                    terminal_action_family="form",
                )
            ],
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.scout_trajectory = [{"tool_name": "click", "selector": "button.start", "accessible_name": "Start"}]

        assert synthesized_block_persistence_signal(ctx, "type_text") is None

        assert ctx.turn_ownership is not None
        assert TurnClaimant.ACTUATION_OBLIGATION_FILL in ctx.turn_ownership.claims
        assert current_turn_owner(ctx) is None

    def test_actuation_obligation_admits_required_fill_tool_for_method_mandated_run_contract(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
            required_context={RequiredContextKey.BROWSER_STATE},
        )
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="visible-fill",
                    outcome="fields are visibly filled on the live page",
                    method_mandated=True,
                    level="run",
                )
            ],
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.scout_trajectory = [{"tool_name": "click", "selector": "button.start", "accessible_name": "Start"}]

        assert synthesized_block_persistence_signal(ctx, "type_text") is None
        click_signal = synthesized_block_persistence_signal(ctx, "click")
        assert isinstance(click_signal, CopilotToolBlockerSignal)
        assert click_signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE

    def test_actuation_obligation_blocks_type_text_for_definition_method_contract(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
            required_context={RequiredContextKey.BROWSER_STATE},
        )
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="definition-contract",
                    outcome="inputs are reusable",
                    method_mandated=True,
                    level="definition",
                )
            ],
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.scout_trajectory = [{"tool_name": "click", "selector": "button.start", "accessible_name": "Start"}]

        signal = synthesized_block_persistence_signal(ctx, "type_text")

        assert isinstance(signal, CopilotToolBlockerSignal)
        assert signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE

    def test_actuation_obligation_admits_required_fill_tool_after_turn_state_reconcile(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
            required_context={RequiredContextKey.BROWSER_STATE},
        )
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="form-submit",
                    outcome="form fields are filled",
                    kind="terminal_action",
                    terminal_action_family="form",
                )
            ],
        )
        ctx.completion_criteria_turn_state = SimpleNamespace(
            decision=SimpleNamespace(
                criteria=(
                    CompletionCriterion(
                        id="workflow-run",
                        outcome="workflow has been tested",
                        kind="terminal_action",
                        terminal_action_family="workflow_run",
                    ),
                )
            )
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 2
        ctx.scout_trajectory = [
            {"tool_name": "click", "selector": "button.start", "accessible_name": "Start"},
            {"tool_name": "type_text", "selector": "#company", "accessible_name": "Company"},
        ]

        assert synthesized_block_persistence_signal(ctx, "type_text") is None

    def test_persistence_offer_blocks_type_text_without_actuation_obligation(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
            required_context={RequiredContextKey.BROWSER_STATE},
        )
        ctx.request_policy = RequestPolicy()
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.scout_trajectory = [{"tool_name": "click", "selector": "button.start", "accessible_name": "Start"}]

        signal = synthesized_block_persistence_signal(ctx, "type_text")

        assert isinstance(signal, CopilotToolBlockerSignal)
        assert signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE
        assert signal.blocked_tool == "type_text"

    def test_actuation_obligation_admits_required_fill_tool_from_fill_trajectory(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
            required_context={RequiredContextKey.BROWSER_STATE},
        )
        ctx.request_policy = RequestPolicy()
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.scout_trajectory = [
            {
                "tool_name": "type_text",
                "selector": "#company",
                "typed_value": "Example Realty Labs Inc",
                "accessible_name": "Company",
            }
        ]

        assert synthesized_block_persistence_signal(ctx, "type_text") is None

    def test_fill_less_trajectory_does_not_arm_actuation_obligation(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
            required_context={RequiredContextKey.BROWSER_STATE},
        )
        ctx.request_policy = RequestPolicy()
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.scout_trajectory = [{"tool_name": "click", "selector": "button.start", "accessible_name": "Start"}]

        signal = synthesized_block_persistence_signal(ctx, "type_text")

        assert isinstance(signal, CopilotToolBlockerSignal)
        assert signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE

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

    def test_never_captured_obligation_admits_only_its_expected_scout_tool(self) -> None:
        ctx = _Ctx()
        ctx.turn_id = "turn-a"
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.synthesized_block_offered_goal_complete = True
        ctx.scout_trajectory = [{"tool_name": "click", "selector": "#existing"}]
        ctx.never_captured_obligation = NeverCapturedObligation(
            identity_digest="identity",
            turn_id=ctx.turn_id,
            draft_fingerprint="draft",
            block_label="submit",
            site="whole_trajectory",
            method="click",
            normalized_receiver="page.locator('#submit')",
            call_shape_digest="shape",
            expected_tool_name="click",
            armed_after_trajectory_index=0,
        )

        assert synthesized_block_persistence_signal(ctx, "click", {"selector": "#submit"}) is None
        assert isinstance(
            synthesized_block_persistence_signal(ctx, "click", {"selector": "#other"}),
            CopilotToolBlockerSignal,
        )
        assert isinstance(synthesized_block_persistence_signal(ctx, "type_text"), CopilotToolBlockerSignal)

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

    def _post_run_page_path_ctx(
        self,
        *,
        workflow_run_id: str = "wr_129160000000000001",
        structural_failure_identity: str = "completion:page-path",
        trajectory: list[dict[str, object]] | None = None,
        page_path_failure: PostRunPagePathFailure | None = None,
    ) -> _Ctx:
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
            workflow_run_id=workflow_run_id,
            structural_failure_identity=structural_failure_identity,
            page_path_failure=page_path_failure
            or PostRunPagePathFailure(
                kind="challenge",
                workflow_run_id=workflow_run_id,
                current_url="https://example.test/challenge",
                continuation_targets=[
                    PostRunPagePathTarget(kind="challenge", selector="#continue"),
                    PostRunPagePathTarget(kind="challenge", selector="#token"),
                    PostRunPagePathTarget(kind="challenge", selector="#missing"),
                ],
                enter_allowed=True,
            ),
        )
        ctx.last_run_blocks_workflow_run_id = workflow_run_id
        ctx.post_run_page_observation_tool = "evaluate"
        ctx.post_run_page_observation_url = "https://example.test/challenge"
        ctx.post_run_page_observation_workflow_run_id = workflow_run_id
        ctx.post_run_page_observation_after_failed_test = True
        ctx.post_run_page_observation_generation = 1
        ctx.scout_trajectory = trajectory or []
        return ctx

    @pytest.mark.asyncio
    async def test_post_run_page_path_admission_uses_existing_hooks_to_record_click_and_enter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def no_role_name(*_args: object, **_kwargs: object) -> tuple[str, str]:
            return "", ""

        async def no_observation(*_args: object, **_kwargs: object) -> tuple[None, None]:
            return None, None

        monkeypatch.setattr(mcp_hooks, "_resolve_scout_role_name", no_role_name)
        monkeypatch.setattr(mcp_hooks, "_register_scout_interaction_observation", no_observation)
        ctx = make_copilot_context()
        qualified = self._post_run_page_path_ctx()
        for field in (
            "turn_intent",
            "block_authoring_policy",
            "completion_verification_result",
            "latest_recorded_build_test_outcome",
            "last_run_blocks_workflow_run_id",
            "post_run_page_observation_tool",
            "post_run_page_observation_url",
            "post_run_page_observation_workflow_run_id",
            "post_run_page_observation_after_failed_test",
        ):
            setattr(ctx, field, getattr(qualified, field))

        assert synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"}) is None
        ctx.pending_scout_source_url = "https://example.test/challenge"
        await _click_post_hook(
            {"ok": True, "data": {"selector": "#continue"}},
            {"browser_context": {"url": "https://example.test/mfa", "title": "MFA"}},
            ctx,
        )
        assert [(item["tool_name"], item["trajectory_index"]) for item in ctx.scout_trajectory] == [("click", 0)]

        enter_ctx = make_copilot_context()
        qualified = self._post_run_page_path_ctx()
        for field in (
            "turn_intent",
            "block_authoring_policy",
            "completion_verification_result",
            "latest_recorded_build_test_outcome",
            "last_run_blocks_workflow_run_id",
            "post_run_page_observation_tool",
            "post_run_page_observation_url",
            "post_run_page_observation_workflow_run_id",
            "post_run_page_observation_after_failed_test",
            "post_run_page_observation_generation",
        ):
            setattr(enter_ctx, field, getattr(qualified, field))
        assert (
            synthesized_block_persistence_signal(
                enter_ctx,
                "press_key",
                {"key": "Enter", "selector": "#token"},
            )
            is None
        )
        enter_ctx.pending_scout_source_url = "https://example.test/challenge"
        await _press_key_post_hook(
            {"ok": True, "data": {"selector": "#token", "key": "Enter"}},
            {"browser_context": {"url": "https://example.test/dashboard", "title": "Dashboard"}},
            enter_ctx,
        )
        assert [(item["tool_name"], item["trajectory_index"]) for item in enter_ctx.scout_trajectory] == [
            ("press_key", 0)
        ]
        assert ctx.turn_ownership is not None
        assert TurnClaimant.POST_RUN_PAGE_PATH_INTERACTION in ctx.turn_ownership.claims
        assert enter_ctx.turn_ownership is not None
        assert TurnClaimant.POST_RUN_PAGE_PATH_INTERACTION in enter_ctx.turn_ownership.claims
        assert isinstance(
            synthesized_block_persistence_signal(ctx, "click", {"selector": "#unrelated"}),
            CopilotToolBlockerSignal,
        )

    @pytest.mark.asyncio
    async def test_post_run_page_path_admission_precedes_only_matching_current_page_challenge_action(
        self,
    ) -> None:
        class RawResult:
            structured_content = {"ok": True, "data": {"selector": "#continue"}}
            is_error = False
            content: list[object] = []

        class RecordingClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def call_tool(
                self,
                name: str,
                arguments: dict[str, object],
                raise_on_error: bool = False,
            ) -> RawResult:
                self.calls.append((name, arguments))
                return RawResult()

        def challenge_ctx() -> Any:
            ctx = make_copilot_context()
            qualified = self._post_run_page_path_ctx()
            for field in (
                "turn_intent",
                "block_authoring_policy",
                "completion_verification_result",
                "latest_recorded_build_test_outcome",
                "last_run_blocks_workflow_run_id",
                "post_run_page_observation_tool",
                "post_run_page_observation_url",
                "post_run_page_observation_workflow_run_id",
                "post_run_page_observation_after_failed_test",
                "post_run_page_observation_generation",
            ):
                setattr(ctx, field, getattr(qualified, field))
            ctx.composition_page_evidence = {
                "observed_after_workflow_run": True,
                "workflow_run_id": "wr_129160000000000001",
                "challenge_state": {
                    "detected": True,
                    "kind": "verification",
                    "requires_human_verification": True,
                    "gates_submit_controls": True,
                },
                "challenge_controls": [{"selector": "#continue", "interactive": True}],
            }
            return ctx

        admitted_ctx = challenge_ctx()
        admitted_client = RecordingClient()
        admitted_server = SkyvernOverlayMCPServer(
            transport=MagicMock(),
            overlays={"click": SchemaOverlay()},
            alias_map={},
            allowlist=frozenset(),
            context_provider=lambda: admitted_ctx,
        )
        admitted_server._client = admitted_client

        admitted = await admitted_server.call_tool("click", {"selector": "#continue"})

        assert admitted.isError is False
        assert admitted_client.calls == [("click", {"selector": "#continue"})]
        assert admitted_ctx.turn_halt is None

        blocked_ctx = challenge_ctx()
        blocked_client = RecordingClient()
        blocked_server = SkyvernOverlayMCPServer(
            transport=MagicMock(),
            overlays={"click": SchemaOverlay()},
            alias_map={},
            allowlist=frozenset(),
            context_provider=lambda: blocked_ctx,
        )
        blocked_server._client = blocked_client

        blocked = await blocked_server.call_tool("click", {"selector": "#unrelated"})

        assert blocked.isError is True
        assert blocked_client.calls == []
        assert blocked_ctx.turn_halt is not None

        terminal_ctx = challenge_ctx()
        terminal_signal = CopilotToolBlockerSignal(
            blocker_kind="tool_error",
            agent_steering_text="The current page is a terminal challenge.",
            user_facing_reason="I could not continue past the site challenge.",
            recovery_hint="report_blocker_to_user",
            cleared_by_tools=frozenset(),
            preserves_workflow_draft=True,
            renders_final_reply=True,
            internal_reason_code="probable_site_block_stop",
            blocked_tool="click",
        )
        assert stash_turn_halt_from_blocker_signal(terminal_ctx, terminal_signal, source="test") is not None
        terminal_client = RecordingClient()
        terminal_server = SkyvernOverlayMCPServer(
            transport=MagicMock(),
            overlays={"click": SchemaOverlay()},
            alias_map={},
            allowlist=frozenset(),
            context_provider=lambda: terminal_ctx,
        )
        terminal_server._client = terminal_client

        terminal = await terminal_server.call_tool("click", {"selector": "#continue"})

        assert terminal.isError is True
        assert terminal_client.calls == []
        assert terminal_ctx.turn_halt.blocker_signal == terminal_signal
        assert terminal_ctx.post_run_page_path_interaction_window is None

    @pytest.mark.asyncio
    async def test_post_run_page_path_pre_hook_rejection_does_not_spend_admission_budget(self) -> None:
        class RecordingClient:
            calls: list[tuple[str, dict[str, object]]] = []

            async def call_tool(
                self,
                name: str,
                arguments: dict[str, object],
                raise_on_error: bool = False,
            ) -> None:
                self.calls.append((name, arguments))

        async def reject_before_dispatch(
            _arguments: dict[str, Any],
            _ctx: Any,
        ) -> dict[str, object]:
            return {"ok": False, "error": "pre-dispatch rejection"}

        ctx = make_copilot_context()
        qualified = self._post_run_page_path_ctx()
        for field in (
            "turn_intent",
            "block_authoring_policy",
            "completion_verification_result",
            "latest_recorded_build_test_outcome",
            "last_run_blocks_workflow_run_id",
            "post_run_page_observation_tool",
            "post_run_page_observation_url",
            "post_run_page_observation_workflow_run_id",
            "post_run_page_observation_after_failed_test",
            "post_run_page_observation_generation",
        ):
            setattr(ctx, field, getattr(qualified, field))
        client = RecordingClient()
        server = SkyvernOverlayMCPServer(
            transport=MagicMock(),
            overlays={"click": SchemaOverlay(pre_hook=reject_before_dispatch)},
            alias_map={},
            allowlist=frozenset(),
            context_provider=lambda: ctx,
        )
        server._client = client

        result = await server.call_tool("click", {"selector": "#continue"})

        assert result.isError is True
        assert client.calls == []
        assert ctx.post_run_page_path_interaction_window is None

    def test_post_run_page_path_admission_requires_typed_page_path_failure_contract(self) -> None:
        ctx = self._post_run_page_path_ctx(
            page_path_failure=PostRunPagePathFailure(
                kind="non_page_outcome",
                workflow_run_id="wr_129160000000000001",
                current_url="https://example.test/challenge",
                continuation_targets=[],
                enter_allowed=False,
            )
        )
        expected_ctx = self._post_run_page_path_ctx()
        expected_ctx.post_run_page_observation_after_failed_test = False
        expected = synthesized_block_persistence_signal(expected_ctx, "click", {"selector": "#continue"})

        blocked = synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"})

        assert isinstance(expected, CopilotToolBlockerSignal)
        assert isinstance(blocked, CopilotToolBlockerSignal)
        assert blocked.model_dump() == expected.model_dump()
        assert ctx.post_run_page_path_interaction_window is None

    def test_post_run_page_path_contract_mints_only_structured_current_page_continuations(self) -> None:
        run_id = "wr_129160000000000001"
        base_evidence = {
            "workflow_run_id": run_id,
            "observed_after_workflow_run": True,
            "current_url": "https://example.test/login",
            "forms": [
                {
                    "fields": [{"type": "password", "selector": "#password"}],
                    "submit_controls": [{"type": "submit", "selector": "#continue"}],
                }
            ],
        }

        page_path = _post_run_page_path_failure(base_evidence, run_id)
        non_page = _post_run_page_path_failure(
            {
                **base_evidence,
                "forms": [],
                "navigation_targets": [{"selector": "#settings"}],
                "result_containers": [{"selector": "#results"}],
            },
            run_id,
        )

        assert page_path is not None
        assert page_path.kind == "login"
        assert page_path.continuation_targets == (PostRunPagePathTarget(kind="form_submit", selector="#continue"),)
        assert page_path.enter_allowed is True
        assert non_page is not None
        assert non_page.kind == "non_page_outcome"
        assert non_page.continuation_targets == ()

    def test_post_run_page_path_contract_mints_only_structural_password_form_submits(self) -> None:
        run_id = "wr_129160000000000001"
        condition = _post_run_page_path_failure(
            {
                "workflow_run_id": run_id,
                "observed_after_workflow_run": True,
                "current_url": "https://example.test/login",
                "forms": [
                    {
                        "fields": [{"type": "password", "selector": "#password"}],
                        "submit_controls": [
                            {"type": "submit", "text": "Sign in", "selector": "#sign-in"},
                            {"type": "button", "text": "Delete account", "selector": "#delete-account"},
                            {"type": "button", "text": "Cancel", "selector": "#cancel"},
                        ],
                    }
                ],
            },
            run_id,
        )

        assert condition is not None
        assert condition.kind == "login"
        assert condition.continuation_targets == (PostRunPagePathTarget(kind="form_submit", selector="#sign-in"),)
        assert condition.enter_allowed is True

    def test_post_run_page_path_contract_requires_explicit_navigation_and_challenge_association(self) -> None:
        run_id = "wr_129160000000000001"
        base_evidence = {
            "workflow_run_id": run_id,
            "observed_after_workflow_run": True,
            "current_url": "https://example.test/interstitial",
            "forms": [
                {
                    "fields": [{"type": "search", "selector": "#query"}],
                    "submit_controls": [{"selector": "#delete"}],
                }
            ],
            "clickable_controls": [{"selector": "#delete"}],
            "navigation_targets": [
                {"selector": "#settings", "href": "https://example.test/settings"},
                {"selector": "#continue", "href": "https://example.test/report"},
            ],
            "challenge_state": {
                "detected": True,
                "gates_submit_controls": False,
                "gated_submit_controls": [],
            },
        }

        unrelated = _post_run_page_path_failure(base_evidence, run_id)
        navigation = _post_run_page_path_failure(
            base_evidence,
            run_id,
            required_target_url="https://example.test/report",
        )

        assert unrelated is not None
        assert unrelated.kind == "non_page_outcome"
        assert unrelated.continuation_targets == ()
        assert navigation is not None
        assert navigation.kind == "incomplete_navigation"
        assert navigation.continuation_targets == (PostRunPagePathTarget(kind="navigation", selector="#continue"),)

    def test_post_run_page_path_contract_distinguishes_hash_route_navigation_targets(self) -> None:
        run_id = "wr_129160000000000001"
        condition = _post_run_page_path_failure(
            {
                "workflow_run_id": run_id,
                "observed_after_workflow_run": True,
                "current_url": "https://example.test/app#/login",
                "navigation_targets": [
                    {"selector": "#settings", "href": "https://example.test/app#/settings"},
                    {"selector": "#delete", "href": "https://example.test/app#/delete"},
                ],
            },
            run_id,
            required_target_url="https://example.test/app#/settings",
        )

        assert condition is not None
        assert condition.kind == "incomplete_navigation"
        assert condition.continuation_targets == (PostRunPagePathTarget(kind="navigation", selector="#settings"),)

    def test_post_run_page_path_contract_excludes_unrelated_form_submit_from_challenge(self) -> None:
        run_id = "wr_129160000000000001"
        condition = _post_run_page_path_failure(
            {
                "workflow_run_id": run_id,
                "observed_after_workflow_run": True,
                "current_url": "https://example.test/challenge",
                "forms": [{"submit_controls": [{"selector": "#newsletter"}]}],
                "challenge_state": {
                    "detected": True,
                    "gates_submit_controls": True,
                    "gated_submit_controls": [{"selector": "#continue"}],
                },
            },
            run_id,
        )

        assert condition is not None
        assert condition.kind == "challenge"
        assert condition.continuation_targets == (PostRunPagePathTarget(kind="challenge", selector="#continue"),)

    def test_post_run_page_path_contract_does_not_bind_selectorless_label_to_form_control(self) -> None:
        run_id = "wr_129160000000000001"
        condition = _post_run_page_path_failure(
            {
                "workflow_run_id": run_id,
                "observed_after_workflow_run": True,
                "current_url": "https://example.test/challenge",
                "forms": [
                    {
                        "submit_controls": [
                            {"text": "Delete account", "selector": "#delete-account"},
                            {"text": "Subscribe", "selector": "#newsletter"},
                        ]
                    }
                ],
                "challenge_state": {
                    "detected": True,
                    "gates_submit_controls": True,
                    "gated_submit_controls": [{"text": "Delete account", "disabled": True}],
                },
            },
            run_id,
        )

        assert condition is not None
        assert condition.kind == "non_page_outcome"
        assert condition.continuation_targets == ()

    def test_post_run_page_path_contract_keeps_structurally_proven_challenge_descendants_only(self) -> None:
        run_id = "wr_129160000000000001"
        condition = _post_run_page_path_failure(
            {
                "workflow_run_id": run_id,
                "observed_after_workflow_run": True,
                "current_url": "https://example.test/challenge",
                "challenge_controls": [
                    {"tag": "div", "selector": "div", "text": "Login confirmation challenge"},
                    {"tag": "input", "type": "checkbox", "selector": "#notRobot", "checked": False},
                    {"tag": "input", "type": "checkbox", "selector": "#alreadyChecked", "checked": True},
                    {"tag": "button", "type": "submit", "selector": "button.btn-primary", "text": "Continue"},
                    {"tag": "button", "selector": "button.goback", "text": "Go back to login"},
                    {"tag": "button", "selector": "#delete", "text": "Delete account"},
                    {"tag": "button", "selector": "#disabled", "text": "Verify", "disabled": True},
                    {"tag": "a", "selector": "#privacy", "text": "Privacy policy"},
                    {"tag": "textarea", "selector": "#notes", "text": "Notes"},
                ],
                "challenge_state": {
                    "detected": True,
                    "gates_submit_controls": False,
                    "gated_submit_controls": [],
                },
            },
            run_id,
        )

        assert condition is not None
        assert condition.kind == "challenge"
        assert condition.continuation_targets == (
            PostRunPagePathTarget(kind="challenge", selector="#notRobot"),
            PostRunPagePathTarget(kind="challenge", selector="button.btn-primary"),
        )

    def test_post_run_page_path_contract_does_not_admit_lone_destructive_challenge_control(self) -> None:
        run_id = "wr_129160000000000001"
        condition = _post_run_page_path_failure(
            {
                "workflow_run_id": run_id,
                "observed_after_workflow_run": True,
                "current_url": "https://example.test/challenge",
                "challenge_controls": [
                    {"tag": "div", "selector": "#challenge-carrier"},
                    {"tag": "button", "selector": "#zurueck", "text": "Zurück zur Anmeldung"},
                ],
                "challenge_state": {
                    "detected": True,
                    "gates_submit_controls": False,
                    "gated_submit_controls": [],
                },
            },
            run_id,
        )

        assert condition is not None
        assert condition.kind == "non_page_outcome"
        assert condition.continuation_targets == ()

    def test_post_run_page_path_contract_rejects_ambiguous_loose_challenge_buttons(self) -> None:
        run_id = "wr_129160000000000001"
        condition = _post_run_page_path_failure(
            {
                "workflow_run_id": run_id,
                "observed_after_workflow_run": True,
                "current_url": "https://example.test/challenge",
                "challenge_controls": [
                    {"tag": "div", "selector": "#challenge-carrier"},
                    {"tag": "button", "selector": "#weiter", "text": "Weiter"},
                    {"tag": "button", "selector": "#bestaetigen", "text": "Bestätigen"},
                ],
                "challenge_state": {
                    "detected": True,
                    "gates_submit_controls": False,
                    "gated_submit_controls": [],
                },
            },
            run_id,
        )

        assert condition is not None
        assert condition.kind == "non_page_outcome"
        assert condition.continuation_targets == ()

    def test_post_run_page_path_contract_does_not_change_structural_identity_across_runs(self) -> None:
        def outcome(run_id: str) -> RecordedBuildTestOutcome:
            return RecordedBuildTestOutcome(
                phase="persisted_block_run",
                attempted_tool="update_and_run_blocks",
                verdict="repairable_failure",
                reason_code="outcome_not_demonstrated",
                workflow_run_id=run_id,
                structural_failure_identity="completion:page-path",
                page_path_failure=PostRunPagePathFailure(
                    kind="challenge",
                    workflow_run_id=run_id,
                    current_url=f"https://example.test/challenge?run={run_id}",
                    continuation_targets=[PostRunPagePathTarget(kind="challenge", selector="#continue")],
                ),
            )

        assert outcome("wr_129160000000000001").structural_key == outcome("wr_129160000000000002").structural_key

    def test_post_run_observation_binds_typed_failure_to_existing_authoritative_outcome(self) -> None:
        ctx = self._post_run_page_path_ctx()
        ctx.latest_recorded_build_test_outcome = ctx.latest_recorded_build_test_outcome.model_copy(
            update={"page_path_failure": None}
        )
        ctx.last_test_ok = True
        ctx.post_run_page_observation_generation = 0
        page_evidence = {
            "workflow_run_id": "wr_129160000000000001",
            "observed_after_workflow_run": True,
            "current_url": "https://example.test/challenge",
            "challenge_state": {
                "detected": True,
                "gates_submit_controls": True,
                "gated_submit_controls": [{"selector": "#continue"}],
            },
        }

        _mark_post_run_page_observed(
            ctx,
            source_tool="inspect_page_for_composition",
            url="https://example.test/challenge",
            page_evidence=page_evidence,
        )

        assert ctx.post_run_page_observation_generation == 1
        assert ctx.latest_recorded_build_test_outcome.page_path_failure == PostRunPagePathFailure(
            kind="challenge",
            workflow_run_id="wr_129160000000000001",
            current_url="https://example.test/challenge",
            continuation_targets=[PostRunPagePathTarget(kind="challenge", selector="#continue")],
            enter_allowed=True,
        )
        assert ctx.post_run_page_observation_after_failed_test is True

    def test_post_run_page_path_binding_replaces_stale_target_with_fresh_page_contract(self) -> None:
        ctx = self._post_run_page_path_ctx()

        bind_post_run_page_path_failure(
            ctx,
            {
                "workflow_run_id": "wr_129160000000000001",
                "observed_after_workflow_run": True,
                "current_url": "https://example.test/mfa",
                "forms": [
                    {
                        "fields": [{"type": "password", "selector": "#token"}],
                        "submit_controls": [{"type": "submit", "selector": "#verify"}],
                    }
                ],
            },
        )

        condition = ctx.latest_recorded_build_test_outcome.page_path_failure
        assert condition is not None
        assert condition.current_url == "https://example.test/mfa"
        assert condition.continuation_targets == (PostRunPagePathTarget(kind="form_submit", selector="#verify"),)

    def test_schema_empty_screenshot_does_not_replace_post_run_page_path_contract(self) -> None:
        ctx = self._post_run_page_path_ctx()
        original = ctx.latest_recorded_build_test_outcome.page_path_failure

        _record_composition_page_observation(
            ctx,
            source_tool="get_browser_screenshot",
            url="https://example.test/challenge",
            title="Challenge",
        )

        assert ctx.latest_recorded_build_test_outcome.page_path_failure == original
        assert ctx.post_run_page_observation_generation == 1

    def test_post_run_page_path_admission_rejects_non_page_verification_failure(self) -> None:
        ctx = self._post_run_page_path_ctx(page_path_failure=None)
        ctx.latest_recorded_build_test_outcome = ctx.latest_recorded_build_test_outcome.model_copy(
            update={"page_path_failure": None}
        )

        blocked = synthesized_block_persistence_signal(ctx, "press_key", {"key": "Enter", "selector": "#continue"})

        assert isinstance(blocked, CopilotToolBlockerSignal)
        assert blocked.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE
        assert ctx.post_run_page_path_interaction_window is None

    def test_post_run_page_path_admission_requires_current_page_contract_url(self) -> None:
        ctx = self._post_run_page_path_ctx()
        ctx.post_run_page_observation_url = "https://example.test/other"

        blocked = synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"})

        assert isinstance(blocked, CopilotToolBlockerSignal)
        assert blocked.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE
        assert ctx.post_run_page_path_interaction_window is None

    def test_post_run_page_path_admission_rejects_click_outside_recorded_continuation(self) -> None:
        ctx = self._post_run_page_path_ctx()

        for arguments in (
            None,
            {},
            {"selector": ""},
            {"selector": "#unrelated"},
            {"selector": "button:contains('Continue')"},
        ):
            blocked = synthesized_block_persistence_signal(ctx, "click", arguments)
            assert isinstance(blocked, CopilotToolBlockerSignal)

        assert ctx.post_run_page_path_interaction_window is None

    def test_post_run_page_path_admission_rejects_blast_radius_sibling_without_contract(self) -> None:
        ctx = self._post_run_page_path_ctx()
        ctx.latest_recorded_build_test_outcome = ctx.latest_recorded_build_test_outcome.model_copy(
            update={"page_path_failure": None}
        )

        for tool_name, arguments in (("click", {"selector": "#continue"}), ("press_key", {"key": "Enter"})):
            blocked = synthesized_block_persistence_signal(ctx, tool_name, arguments)
            assert isinstance(blocked, CopilotToolBlockerSignal)
            assert blocked.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE

    def test_post_run_page_path_invalid_click_does_not_spend_admission_budget(self) -> None:
        ctx = self._post_run_page_path_ctx()

        blocked = synthesized_block_persistence_signal(ctx, "click", {"selector": "#not-recorded"})

        assert isinstance(blocked, CopilotToolBlockerSignal)
        assert ctx.post_run_page_path_interaction_window is None
        assert synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"}) is None
        assert ctx.post_run_page_path_interaction_window.admitted_attempts == 1

    def test_post_run_page_path_admission_is_same_run_and_argument_exact(self) -> None:
        ctx = self._post_run_page_path_ctx()

        assert synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"}) is None
        assert (
            synthesized_block_persistence_signal(
                ctx,
                "press_key",
                {"key": "Enter", "selector": "#token"},
            )
            is None
        )
        assert ctx.post_run_page_path_interaction_window.admitted_attempts == 2
        for malformed in (
            None,
            {},
            {"key": "Enter"},
            {"key": "enter", "selector": "#token"},
            {"key": " Enter ", "selector": "#token"},
            {"key": 1, "selector": "#token"},
            {"key": "Enter", "selector": "#unrelated"},
        ):
            assert isinstance(
                synthesized_block_persistence_signal(ctx, "press_key", malformed),
                CopilotToolBlockerSignal,
            )
        assert isinstance(
            synthesized_block_persistence_signal(ctx, "type_text", {"selector": "#token", "text": "123456"}),
            CopilotToolBlockerSignal,
        )

        ctx.post_run_page_observation_workflow_run_id = "wr_129160000000000099"
        assert isinstance(
            synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"}),
            CopilotToolBlockerSignal,
        )

        ctx = self._post_run_page_path_ctx()
        ctx.latest_recorded_build_test_outcome = ctx.latest_recorded_build_test_outcome.model_copy(
            update={"workflow_run_id": None}
        )
        assert isinstance(
            synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"}),
            CopilotToolBlockerSignal,
        )
        assert ctx.post_run_page_path_interaction_window is None

    def test_post_run_page_path_window_anchors_after_stale_trajectory(self) -> None:
        stale_reached_trajectory = [
            {"tool_name": "click", "selector": "#open", "trajectory_index": 3},
            {"tool_name": "click", "selector": "#submit", "trajectory_index": 4},
        ]
        ctx = self._post_run_page_path_ctx(trajectory=stale_reached_trajectory)

        assert synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"}) is None
        assert ctx.post_run_page_path_interaction_window.trajectory_anchor == 4
        assert ctx.post_run_page_path_interaction_window.admitted_attempts == 1

    def test_post_run_page_path_success_requires_fresh_observation_and_closes_on_completed_page(self) -> None:
        ctx = self._post_run_page_path_ctx()

        assert synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"}) is None
        ctx.scout_trajectory.append(
            {
                "tool_name": "click",
                "selector": "#continue",
                "source_url": "https://example.test/challenge",
                "trajectory_index": 0,
            }
        )
        expected_ctx = self._post_run_page_path_ctx()
        expected_ctx.post_run_page_observation_after_failed_test = False
        expected = synthesized_block_persistence_signal(expected_ctx, "click", {"selector": "#continue"})

        stale = synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"})

        assert isinstance(expected, CopilotToolBlockerSignal)
        assert isinstance(stale, CopilotToolBlockerSignal)
        assert stale.model_dump() == expected.model_dump()

        ctx.last_test_ok = False
        _mark_post_run_page_observed(
            ctx,
            source_tool="evaluate",
            url="https://example.test/dashboard",
            page_evidence={
                "workflow_run_id": "wr_129160000000000001",
                "observed_after_workflow_run": True,
                "current_url": "https://example.test/dashboard",
                "result_containers": [{"selector": "#results"}],
            },
        )

        completed = synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"})
        assert isinstance(completed, CopilotToolBlockerSignal)
        assert completed.model_dump() == expected.model_dump()

    def test_post_run_page_path_fresh_observation_supports_three_steps_without_resetting_budget(self) -> None:
        ctx = self._post_run_page_path_ctx()

        assert synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"}) is None
        ctx.scout_trajectory.append(
            {
                "tool_name": "click",
                "selector": "#continue",
                "source_url": "https://example.test/challenge",
                "trajectory_index": 0,
            }
        )
        ctx.last_test_ok = False
        _mark_post_run_page_observed(
            ctx,
            source_tool="evaluate",
            url="https://example.test/mfa",
            page_evidence={
                "workflow_run_id": "wr_129160000000000001",
                "observed_after_workflow_run": True,
                "current_url": "https://example.test/mfa",
                "forms": [
                    {
                        "fields": [{"type": "password", "selector": "#token"}],
                        "submit_controls": [{"type": "submit", "selector": "#verify"}],
                    }
                ],
            },
        )

        assert isinstance(
            synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"}),
            CopilotToolBlockerSignal,
        )
        assert (
            synthesized_block_persistence_signal(
                ctx,
                "press_key",
                {"key": "Enter", "selector": "#verify"},
            )
            is None
        )
        assert ctx.post_run_page_path_interaction_window.admitted_attempts == 2
        ctx.scout_trajectory.append(
            {
                "tool_name": "press_key",
                "selector": "#verify",
                "key": "Enter",
                "source_url": "https://example.test/mfa",
                "trajectory_index": 1,
            }
        )
        _mark_post_run_page_observed(
            ctx,
            source_tool="inspect_page_for_composition",
            url="https://example.test/confirmation",
            page_evidence={
                "workflow_run_id": "wr_129160000000000001",
                "observed_after_workflow_run": True,
                "current_url": "https://example.test/confirmation",
                "challenge_state": {
                    "detected": True,
                    "gates_submit_controls": True,
                    "gated_submit_controls": [{"selector": "#confirm"}],
                },
            },
        )

        assert synthesized_block_persistence_signal(ctx, "click", {"selector": "#confirm"}) is None
        assert ctx.post_run_page_path_interaction_window.admitted_attempts == 3

    def test_post_run_page_path_window_charges_failed_attempts_and_resets_for_new_identity(self) -> None:
        ctx = self._post_run_page_path_ctx()

        for _ in range(4):
            assert synthesized_block_persistence_signal(ctx, "click", {"selector": "#missing"}) is None
        expected_blocker_ctx = self._post_run_page_path_ctx()
        expected_blocker_ctx.post_run_page_observation_after_failed_test = False
        expected = synthesized_block_persistence_signal(expected_blocker_ctx, "click", {"selector": "#missing"})
        exhausted = synthesized_block_persistence_signal(ctx, "click", {"selector": "#missing"})
        assert isinstance(expected, CopilotToolBlockerSignal)
        assert isinstance(exhausted, CopilotToolBlockerSignal)
        assert exhausted.model_dump() == expected.model_dump()

        ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="outcome_not_demonstrated",
            workflow_run_id="wr_129160000000000002",
            structural_failure_identity="completion:new-page-path",
            page_path_failure=PostRunPagePathFailure(
                kind="incomplete_navigation",
                workflow_run_id="wr_129160000000000002",
                current_url="https://example.test/challenge",
                continuation_targets=[
                    PostRunPagePathTarget(kind="navigation", selector="#missing"),
                ],
            ),
        )
        ctx.last_run_blocks_workflow_run_id = "wr_129160000000000002"
        ctx.post_run_page_observation_workflow_run_id = "wr_129160000000000002"
        ctx.scout_trajectory = [
            {"tool_name": "click", "selector": f"#evicted-{index}", "trajectory_index": index}
            for index in range(80, 100)
        ]
        for _ in range(4):
            assert synthesized_block_persistence_signal(ctx, "click", {"selector": "#missing"}) is None
        assert ctx.post_run_page_path_interaction_window.trajectory_anchor == 99

    def test_post_run_page_path_admission_yields_to_terminal_owner_without_spending_budget(self) -> None:
        ctx = self._post_run_page_path_ctx()
        terminal_signal = CopilotToolBlockerSignal(
            blocker_kind="tool_error",
            agent_steering_text="The current page is a terminal challenge.",
            user_facing_reason="I could not continue past the site challenge.",
            recovery_hint="report_blocker_to_user",
            cleared_by_tools=frozenset(),
            preserves_workflow_draft=True,
            renders_final_reply=True,
            internal_reason_code="probable_site_block_stop",
            blocked_tool="click",
        )
        assert stash_turn_halt_from_blocker_signal(ctx, terminal_signal, source="test") is not None

        signal = synthesized_block_persistence_signal(ctx, "click", {"selector": "#continue"})

        assert isinstance(signal, CopilotToolBlockerSignal)
        assert ctx.post_run_page_path_interaction_window is None

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

    def test_unlanded_goal_completion_forces_persistence_after_first_authoring_call(self) -> None:
        trajectory = [
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
            {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)
        ctx.impose_synthesized_code_block = True
        ctx.update_workflow_called = True

        assert synthesized_goal_completion_landing_pending(ctx) is True
        assert synthesized_persistence_reopened(ctx) is True
        assert _should_force_synthesized_block_persistence(ctx) is True
        assert synthesized_block_persistence_signal(ctx, "evaluate") is not None

    def test_landed_goal_completion_stops_forcing_on_identical_resubmission(self) -> None:
        trajectory = [
            {"tool_name": "type_text", "selector": "input[name='q']", "accessible_name": "Search"},
            {"tool_name": "click", "selector": "button[data-action='search']", "accessible_name": "Search"},
        ]
        ctx = self._authoring_ctx(trajectory=trajectory, download_target=None)
        ctx.impose_synthesized_code_block = True
        ctx.update_workflow_called = True
        ctx.synthesized_goal_complete_landed = True

        assert synthesized_goal_completion_landing_pending(ctx) is False
        assert synthesized_persistence_reopened(ctx) is False
        assert _should_force_synthesized_block_persistence(ctx) is False
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

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

    def test_offer_names_missing_steps_when_obligation_open_regardless_of_repeated_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement.synthesize_code_block",
            lambda *args, **kwargs: SynthesizedCodeBlock(code="await page.click('button')"),
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement._get_scouted_spine_missing_steps_for_halt",
            lambda ctx: "`click` on '#search-submit'",
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

        message = _maybe_synthesized_block_offer_msg(ctx)

        assert message is not None
        assert "#search-submit" in message["content"]

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

    def _in_progress_login_ctx(self, trajectory: list[dict[str, object]]) -> _Ctx:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
            required_context={RequiredContextKey.BROWSER_STATE},
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.scout_trajectory = trajectory
        ctx.synthesized_block_offered_trajectory_len = len(trajectory)
        ctx.synthesized_block_offered_goal_complete = synthesized_trajectory_is_goal_complete(ctx)
        return ctx

    @staticmethod
    def _credential_fill(field: str, trajectory_index: int, source_url: str) -> dict[str, object]:
        return {
            "tool_name": "fill_credential_field",
            "trajectory_index": trajectory_index,
            "credential_id": "cred_login",
            "credential_field": field,
            "selector": f"input[name='{field}']",
            "source_url": source_url,
        }

    @staticmethod
    def _login_submit_click(trajectory_index: int, source_url: str) -> dict[str, object]:
        return {
            "tool_name": "click",
            "trajectory_index": trajectory_index,
            "selector": "button[type='submit']",
            "accessible_name": "Log in",
            "source_url": source_url,
        }

    def test_in_progress_login_admits_commit_interactions_after_credential_fills(self) -> None:
        login_url = "https://portal.test/login"
        ctx = self._in_progress_login_ctx(
            [
                self._credential_fill("username", 0, login_url),
                self._credential_fill("password", 1, login_url),
                self._login_submit_click(2, login_url),
            ]
        )

        armed = synthesized_block_persistence_signal(ctx, "select_option", {"value": "monthly"})
        assert isinstance(armed, CopilotToolBlockerSignal)
        assert armed.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE

        assert synthesized_block_persistence_signal(ctx, "click", {"selector": "button[type='submit']"}) is None
        assert synthesized_block_persistence_signal(ctx, "press_key", {"key": "Enter"}) is None
        assert ctx.turn_ownership is not None
        assert TurnClaimant.ACTUATION_OBLIGATION_LOGIN_COMPLETION in ctx.turn_ownership.claims

        non_submit_key = synthesized_block_persistence_signal(ctx, "press_key", {"key": "Tab"})
        assert isinstance(non_submit_key, CopilotToolBlockerSignal)
        assert non_submit_key.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE

    def test_in_progress_login_refuses_press_key_when_arguments_are_absent(self) -> None:
        login_url = "https://portal.test/login"
        ctx = self._in_progress_login_ctx(
            [
                self._credential_fill("username", 0, login_url),
                self._credential_fill("password", 1, login_url),
                self._login_submit_click(2, login_url),
            ]
        )

        assert synthesized_block_persistence_signal(ctx, "press_key", {"key": "Enter"}) is None

        for absent in (None, "Enter", ["Enter"]):
            refused = synthesized_block_persistence_signal(ctx, "press_key", absent)
            assert isinstance(refused, CopilotToolBlockerSignal)
            assert refused.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE

    def test_in_progress_login_admits_two_factor_confirm_after_token_fill(self) -> None:
        login_url = "https://portal.test/login"
        ctx = self._in_progress_login_ctx(
            [
                self._credential_fill("username", 0, login_url),
                self._credential_fill("password", 1, login_url),
                self._login_submit_click(2, login_url),
                self._credential_fill("totp", 3, "https://portal.test/mfa"),
            ]
        )

        armed = synthesized_block_persistence_signal(ctx, "select_option", {"value": "monthly"})
        assert isinstance(armed, CopilotToolBlockerSignal)
        assert armed.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE

        assert synthesized_block_persistence_signal(ctx, "fill_credential_field", {"credential_field": "totp"}) is None
        assert synthesized_block_persistence_signal(ctx, "click", {"selector": "button[data-action='verify']"}) is None
        assert synthesized_block_persistence_signal(ctx, "press_key", {"key": "Enter"}) is None
        assert ctx.turn_ownership is not None
        assert TurnClaimant.ACTUATION_OBLIGATION_LOGIN_COMPLETION in ctx.turn_ownership.claims

    def test_login_completion_closes_at_post_credential_commit(self) -> None:
        login_url = "https://portal.test/login"
        reports_url = "https://portal.test/reports"
        ctx = self._in_progress_login_ctx(
            [
                self._credential_fill("username", 0, login_url),
                self._credential_fill("password", 1, login_url),
                self._login_submit_click(2, login_url),
                {
                    "tool_name": "type_text",
                    "trajectory_index": 3,
                    "selector": "input[name='date_from']",
                    "typed_value": "-7d",
                    "source_url": reports_url,
                },
                {
                    "tool_name": "click",
                    "trajectory_index": 4,
                    "selector": "button[data-action='run-report']",
                    "accessible_name": "Run report",
                    "source_url": reports_url,
                },
            ]
        )

        blocked_click = synthesized_block_persistence_signal(ctx, "click", {"selector": "button[data-action='next']"})
        assert isinstance(blocked_click, CopilotToolBlockerSignal)
        assert blocked_click.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE

        blocked_key = synthesized_block_persistence_signal(ctx, "press_key", {"key": "Enter"})
        assert isinstance(blocked_key, CopilotToolBlockerSignal)
        assert blocked_key.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE
        assert ctx.turn_ownership is None

    def test_login_completion_window_stays_open_on_read_only_post_login_surface(self) -> None:
        """Characterizes the shipped bound, which is wider than 'until the login outcome is observable'.

        The close condition credits a commit only from an ordered pair whose second click is neither a
        generic opener nor results-surface navigation. A read-only post-login surface never supplies one,
        so the window stays open for the rest of the turn. Tracked as a follow-up, not a shipped intent.
        """
        login_url = "https://portal.test/login"
        app_url = "https://portal.test/analytics"
        ctx = self._in_progress_login_ctx(
            [
                self._credential_fill("username", 0, login_url),
                self._credential_fill("password", 1, login_url),
                self._login_submit_click(2, login_url),
                {
                    "tool_name": "click",
                    "trajectory_index": 3,
                    "selector": "a[href='/analytics/web']",
                    "accessible_name": "Web analytics",
                    "role": "link",
                    "source_url": app_url,
                },
                {
                    "tool_name": "click",
                    "trajectory_index": 4,
                    "selector": "a[href='/analytics/web?range=7d']",
                    "accessible_name": "Last 7 days",
                    "role": "link",
                    "source_url": app_url,
                },
                {
                    "tool_name": "click",
                    "trajectory_index": 5,
                    "selector": "table tbody tr:nth-child(2)",
                    "accessible_name": "Row 2",
                    "source_url": app_url,
                },
            ]
        )

        assert synthesized_block_persistence_signal(ctx, "click", {"selector": "button#delete-account"}) is None

    def test_click_outside_authentication_still_blocked_by_persistence_gate(self) -> None:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
            required_context={RequiredContextKey.BROWSER_STATE},
        )
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="form-submit",
                    outcome="form fields are filled",
                    kind="terminal_action",
                    terminal_action_family="form",
                )
            ],
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = 1
        ctx.scout_trajectory = [
            {"tool_name": "click", "selector": "button.start", "accessible_name": "Start"},
        ]

        click_signal = synthesized_block_persistence_signal(ctx, "click")
        assert isinstance(click_signal, CopilotToolBlockerSignal)
        assert click_signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE


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


def _fc(call_id: str) -> dict[str, str]:
    return {"type": "function_call", "call_id": call_id, "name": "evaluate", "arguments": "{}"}


def _history_item(fields: dict[str, Any], *, attr_style: bool) -> dict[str, Any] | SimpleNamespace:
    return SimpleNamespace(**fields) if attr_style else fields


def _tool_history(
    pair_count: int,
    *,
    interleave_screenshots: bool = False,
    attr_style: bool = False,
) -> list[Any]:
    items: list[Any] = [_history_item({"role": "user", "content": "goal"}, attr_style=attr_style)]
    for index in range(pair_count):
        call_id = f"call_{index}"
        items.extend(
            [
                _history_item(_fc(call_id), attr_style=attr_style),
                _history_item(_fco(call_id, "x" * 50), attr_style=attr_style),
            ]
        )
        if interleave_screenshots:
            items.append(
                _history_item(
                    {"role": "user", "content": f"[copilot:screenshot] frame {index}"},
                    attr_style=attr_style,
                )
            )
    return items


def _history_field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _orphaned_tool_result_ids(items: list[Any]) -> list[str]:
    seen_call_ids: set[str] = set()
    orphaned_ids: list[str] = []
    for item in items:
        item_type = _history_field(item, "type")
        call_id = _history_field(item, "call_id")
        if item_type == "function_call" and isinstance(call_id, str):
            seen_call_ids.add(call_id)
        elif item_type == "function_call_output" and call_id not in seen_call_ids:
            orphaned_ids.append(call_id)
    return orphaned_ids


def _call_ids(items: list[Any], item_type: str) -> list[str]:
    return [
        call_id
        for item in items
        if _history_field(item, "type") == item_type and isinstance((call_id := _history_field(item, "call_id")), str)
    ]


def test_aggressive_prune_drops_orphan_from_eight_pair_repro() -> None:
    pruned = aggressive_prune(_tool_history(8))

    assert _orphaned_tool_result_ids(pruned) == []
    assert _call_ids(pruned, "function_call") == ["call_5", "call_6", "call_7"]
    assert _call_ids(pruned, "function_call_output") == ["call_5", "call_6", "call_7"]


@pytest.mark.parametrize("pair_count", [1, 2, 4, 8, 10])
@pytest.mark.parametrize("tail_size", range(1, 21))
@pytest.mark.parametrize("interleave_screenshots", [False, True])
@pytest.mark.parametrize("attr_style", [False, True])
def test_aggressive_prune_never_keeps_orphaned_tool_results(
    monkeypatch: pytest.MonkeyPatch,
    pair_count: int,
    tail_size: int,
    interleave_screenshots: bool,
    attr_style: bool,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement._AGGRESSIVE_PRUNE_TAIL", tail_size)
    history = _tool_history(
        pair_count,
        interleave_screenshots=interleave_screenshots,
        attr_style=attr_style,
    )
    original = deepcopy(history)

    pruned = aggressive_prune(history)

    assert _orphaned_tool_result_ids(pruned) == []
    assert history == original
    assert pruned[0] is history[0]
    assert all(not str(_history_field(item, "content") or "").startswith("[copilot:screenshot]") for item in pruned)
    retained_indexes = [
        next(index for index, original_item in enumerate(history) if original_item is item) for item in pruned
    ]
    assert retained_indexes == sorted(retained_indexes)


def test_aggressive_prune_drops_output_that_precedes_its_call() -> None:
    opening = {"role": "user", "content": "goal"}
    output = _fco("call_late", "result")
    call = _fc("call_late")

    pruned = aggressive_prune([opening, output, call])

    assert pruned == [opening, call]


def test_aggressive_prune_logs_content_free_pair_validity_telemetry() -> None:
    history = _tool_history(8)

    with capture_logs() as logs:
        aggressive_prune(history)

    event = next(entry for entry in logs if entry["event"] == "copilot_aggressive_prune_pair_validity")
    assert event["retained_tail"] == [
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
    ]
    assert event["orphaned_output_dropped"] is True
    assert "call_4" not in json.dumps(event)


def test_copilot_config_qa_budget_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_QA_TOKEN_BUDGET", None)

    assert CopilotConfig().token_budget == 90_000


def test_copilot_config_uses_typed_qa_budget_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    local_settings = Settings(_env_file=None, ENV="local", WORKFLOW_COPILOT_QA_TOKEN_BUDGET=3_000)
    assert local_settings.WORKFLOW_COPILOT_QA_TOKEN_BUDGET == 3_000
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_QA_TOKEN_BUDGET", 3_000)

    assert CopilotConfig().token_budget == 3_000


def test_copilot_config_ignores_qa_budget_in_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_QA_TOKEN_BUDGET", 3_000)

    assert CopilotConfig().token_budget == 90_000


@pytest.mark.asyncio
@pytest.mark.parametrize("tail_size", range(1, 21))
@pytest.mark.parametrize("attr_style", [False, True])
async def test_context_overflow_session_rewrite_stores_pair_valid_history(
    monkeypatch: pytest.MonkeyPatch,
    tail_size: int,
    attr_style: bool,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement._AGGRESSIVE_PRUNE_TAIL", tail_size)
    session = AsyncMock()
    session.get_items.return_value = _tool_history(10, interleave_screenshots=True, attr_style=attr_style)

    await _recover_from_context_overflow(session, current_input="continue")

    stored_items = session.add_items.await_args.args[0]
    assert _orphaned_tool_result_ids(stored_items) == []
    session.clear_session.assert_awaited_once()


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


LISTING_DETAIL_URL = "http://localhost:8901/record/1457803926"

# Generic multi-field detail DOM: exercises the contract's label/header binding vs the
# coverage-token channel. No specific vertical or PII (see CLAUDE.md OSS-sync rules).
LISTING_DETAIL_HTML = """
<html><head><title>Regional Records Directory</title></head><body>
<div class="layout">
  <div class="panel">
    <h1>Search Results</h1>
    <p class="muted">Showing 1 result in <strong>Example Region</strong>.</p>
    <div class="result-card" id="recordCard">
      <div>
        <div class="rc-name">Northgate Unit 7</div>
        <div class="muted">Facility</div>
        <div>Northgate Holdings, LLC</div>
        <div class="muted">general listing</div>
        <div class="small">100 Example Ave # 200, Example City, EX 00001</div>
        <div class="small muted">12.34 units away &middot; <a class="link">1-800-555-0102</a></div>
        <div id="recordDetails">
          <div class="kv"><div class="k">Reference Number</div><div>1457803926</div></div>
          <div class="kv"><div class="k">Region</div><div>North</div></div>
          <div class="kv"><div class="k">Category</div><div>Standard</div></div>
          <div class="kv"><div class="k">Tier</div><div>Two</div></div>
          <div class="kv"><div class="k">Effective date</div><div>01/01/2024</div></div>
          <h3>Locations</h3>
          <p class="muted small">Approval status per location for Northgate Holdings, LLC.</p>
          <table>
            <thead><tr><th>Site</th><th>Address</th><th>Status</th></tr></thead>
            <tbody>
              <tr><td>Northgate Holdings, LLC</td><td>100 Example Ave # 200, Example City, EX 00001</td><td><span class="status-ok">Approved</span></td></tr>
              <tr><td>Northgate Holdings, LLC</td><td>240 Sample Blvd, Example City, EX 00002</td><td><span class="status-ok">Approved</span></td></tr>
              <tr><td>Southgate Group</td><td>512 Test St, Other City, EX 00003</td><td><span class="status-no">Not Approved</span></td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="rc-flags"></div>
    </div>
  </div>
  <div class="panel filter-side">
    <h2>Filter Options</h2>
    <div class="fld"><label for="refInput">Search by Name, Group, or Reference Number</label><input id="refInput" type="text"/></div>
    <div class="fld"><label>Reference Number</label><input type="text" value="1457803926"/></div>
  </div>
</div>
</body></html>
"""


def _criterion(output_path: str, outcome: str) -> CompletionCriterion:
    return CompletionCriterion(id=output_path, outcome=outcome, output_path=output_path)


def _registered_download_criterion() -> CompletionCriterion:
    return CompletionCriterion(
        id="output.statement_pdf",
        outcome="the statement PDF is downloaded",
        output_path="output.statement_pdf",
        deliverable_kind="registered_download",
        requested_output_evidence_source="registered_artifact_content",
    )


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

    @staticmethod
    def _attach_document_plan(ctx: _Ctx, *, step: int) -> None:
        ctx.copilot_config = CopilotConfig(requested_output_path_aliases={"document name": "output.document_name"})
        ctx.flow_evidence = [
            {
                "step": step,
                "reached_via": "interaction",
                "had_bounded_schema": True,
                "evidence": {
                    "source_tool": "scout_interaction",
                    "interaction_tool": "click",
                    "interaction_selector": "button[data-action='search']",
                    "inspection_warnings": [],
                    "result_containers_truncated": False,
                    "key_value_relations_truncated": False,
                    "key_value_relations": [
                        {
                            "key_text": "Document Name",
                            "container_selector": ".document-kv",
                            "container_match_count": 1,
                            "container_position": 0,
                            "value_child_index": 1,
                            "direct_child_count": 2,
                            "visible": True,
                            "value_visible": True,
                        }
                    ],
                    "result_containers": [],
                },
            }
        ]

    def test_post_turn_offer_compiles_plan_recipe(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "Document Name"))
        self._attach_document_plan(ctx, step=6)
        ctx.synthesized_block_offered = False

        message = _maybe_synthesized_block_offer_msg(ctx)

        assert message is not None
        assert 'page.locator(".document-kv").nth(0)' in str(message["content"])
        assert 'return {"output": {"document_name": _extraction_value_0}}' in str(message["content"])

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

    def test_independent_run_evidence_is_exempt_while_runtime_output_stays_gated(self) -> None:
        independent = CompletionCriterion(
            id="output.login_gate_present",
            outcome="whether a login gate blocked the target is recorded",
            output_path="output.login_gate_present",
            requested_output_evidence_source="independent_run_evidence",
        )
        runtime = _criterion("output.document_name", "the order status document name is captured")
        ctx = self._authoring_ctx(independent, runtime)

        assert uncovered_requested_output_paths(ctx) == {"output.document_name"}
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_independent_run_evidence_is_exempt_from_repair_context(self) -> None:
        independent = CompletionCriterion(
            id="output.login_gate_blocks_target",
            outcome="the login-gate judgment is independently observed after the run",
            output_path="output.login_gate_blocks_target",
            expected_output_shape="goal_judgment_boolean",
            requested_output_evidence_source="independent_run_evidence",
        )
        runtime = _criterion("output.document_name", "the document name is captured")
        ctx = self._authoring_ctx(independent, runtime)
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="extract_order",
            reason_code="metadata_reject",
            required_goal_value_paths=["login_gate_blocks_target", "document_name"],
        )

        assert uncovered_requested_output_paths(ctx) == {"output.document_name"}

    @pytest.mark.parametrize(
        "evidence_source",
        ["registered_output_parameter", "registered_artifact_content"],
    )
    def test_registered_post_run_evidence_remains_uncovered_from_repair_context(
        self,
        evidence_source: str,
    ) -> None:
        registered = CompletionCriterion(
            id="output.confirmation_number",
            outcome="the confirmation number is registered after the run",
            output_path="output.confirmation_number",
            requested_output_evidence_source=evidence_source,
        )
        ctx = self._authoring_ctx(registered)
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="extract_order",
            reason_code="metadata_reject",
            required_goal_value_paths=["confirmation_number"],
        )

        assert uncovered_requested_output_paths(ctx) == {"output.confirmation_number"}

    @pytest.mark.parametrize(
        "evidence_source",
        ["registered_output_parameter", "registered_artifact_content"],
    )
    def test_registered_post_run_evidence_stays_gated_when_independent_evidence_uses_same_repair_path(
        self,
        evidence_source: str,
    ) -> None:
        independent = CompletionCriterion(
            id="independent_confirmation_number",
            outcome="the confirmation number is confirmed by an independent run",
            output_path="output.confirmation_number",
            requested_output_evidence_source="independent_run_evidence",
        )
        registered = CompletionCriterion(
            id="registered_confirmation_number",
            outcome="the confirmation number is registered after the run",
            output_path="output.confirmation_number",
            requested_output_evidence_source=evidence_source,
        )
        ctx = self._authoring_ctx(independent, registered)
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="extract_order",
            reason_code="metadata_reject",
            required_goal_value_paths=["confirmation_number"],
        )

        assert uncovered_requested_output_paths(ctx) == {"output.confirmation_number"}

    def test_runtime_output_stays_gated_when_independent_evidence_uses_same_path(self) -> None:
        independent = CompletionCriterion(
            id="independent_document_name",
            outcome="the document name is confirmed by an independent run",
            output_path="output.document_name",
            requested_output_evidence_source="independent_run_evidence",
        )
        runtime = _criterion("output.document_name", "the order status document name is captured")
        ctx = self._authoring_ctx(independent, runtime)

        assert uncovered_requested_output_paths(ctx) == {"output.document_name"}
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_pathless_post_run_criterion_does_not_erase_repair_output_field(self) -> None:
        independent = CompletionCriterion(
            id="c_independent",
            outcome="the judgment is independently observed after the run",
            output_path=None,
            requested_output_evidence_source="independent_run_evidence",
        )
        ctx = self._authoring_ctx(independent)
        ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
            block_label="extract_order",
            reason_code="metadata_reject",
            required_goal_value_paths=["field"],
        )

        assert _requested_output_paths_for_ctx(ctx) == {"output.field"}

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

    def test_uncovered_output_leaves_the_trajectory_goal_reaching(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        assert uncovered_requested_output_paths(ctx) == {"output.document_name"}
        assert synthesized_trajectory_reaches_goal(ctx) is True
        assert synthesized_trajectory_is_goal_complete(ctx) is False
        assert _should_force_synthesized_block_persistence(ctx) is False

    def test_value_bearing_container_coverage_without_plan_does_not_force(self) -> None:
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
        assert synthesized_trajectory_is_goal_complete(ctx) is False
        assert _should_force_synthesized_block_persistence(ctx) is False

    @staticmethod
    def _kv_page(*, key_text: str, url: str, value_prose: str) -> dict[str, object]:
        return {
            "current_url": url,
            "inspection_warnings": [],
            "result_containers_truncated": False,
            "key_value_relations_truncated": False,
            "key_value_relations": [
                {
                    "key_text": key_text,
                    "container_selector": ".kv",
                    "container_match_count": 1,
                    "container_position": 0,
                    "value_child_index": 1,
                    "direct_child_count": 2,
                    "visible": True,
                    "value_visible": True,
                }
            ],
            "result_containers": [{"selector": "#detail", "text_excerpt": value_prose}],
        }

    def test_contract_credits_output_path_without_lexical_overlap(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.overall_credentialing_result", "Overall Credentialing Result"))
        page = self._kv_page(
            key_text="Overall Credentialing Result",
            url="https://example.com/provider",
            value_prose="Status: Credentialed",
        )
        contract = mint_scout_observation_contract_for_ctx(
            ctx,
            page,
            url="https://example.com/provider",
        )
        assert contract is not None

        record_scouted_output_coverage(ctx, page)
        assert ctx.scouted_output_covered_paths == set()

        with capture_logs() as logs:
            record_scouted_output_coverage(ctx, page, contract=contract)
        assert ctx.scouted_output_covered_paths == {"output.overall_credentialing_result"}
        assert uncovered_requested_output_paths(ctx) == set()
        credited = next(entry for entry in logs if entry["event"] == "copilot_scouted_output_coverage_credited")
        assert credited["provenance"] == "value_grounded"
        assert credited["value_grounded_paths"] == ["output.overall_credentialing_result"]

    @staticmethod
    def _shape_registry_config() -> CopilotConfig:
        return CopilotConfig(
            requested_output_shape_expectations={
                "widget_id": ShapeExpectation(ValueShape.NUMERIC_ID, ValueCardinality.SCALAR, id_digit_length=8),
                "depot": ShapeExpectation(ValueShape.POSTAL_ADDRESS, ValueCardinality.COLUMN),
                "phase": ShapeExpectation(ValueShape.CATEGORICAL_TOKEN, ValueCardinality.COLUMN),
            }
        )

    @staticmethod
    def _shape_scout_page() -> dict[str, object]:
        def _row(row_index: int, depot: str, phase: str) -> dict[str, object]:
            return {
                "row_index": row_index,
                "visible": True,
                "has_row_header": False,
                "cells": [
                    {"column_index": 0, "visible": True, "has_text": True, "text": depot},
                    {"column_index": 1, "visible": True, "has_text": True, "text": phase},
                ],
            }

        return {
            "current_url": "https://example.com/sites",
            "source_tool": "scout_interaction",
            "interaction_selector": "#reveal",
            "inspection_warnings": [],
            "result_containers_truncated": False,
            "key_value_relations_truncated": False,
            "key_value_relations": [
                {
                    "key_text": "Ref Code",
                    "value_text": "12345678",
                    "container_selector": ".kv",
                    "container_match_count": 1,
                    "container_position": 0,
                    "value_child_index": 1,
                    "direct_child_count": 2,
                    "visible": True,
                    "value_visible": True,
                }
            ],
            "result_containers": [
                {
                    "tag": "table",
                    "selector": "#sites",
                    "selector_match_count": 1,
                    "visible": True,
                    "span_free": True,
                    "nested_table_free": True,
                    "headers": [
                        {"text": "Loc", "column_index": 0},
                        {"text": "Stage", "column_index": 1},
                    ],
                    "row_selector": "#sites tbody tr",
                    "row_count": 3,
                    "rows_truncated": False,
                    "sample_rows": ["r0", "r1", "r2"],
                    "rows": [
                        _row(0, "12 Peak Way Reno NV 89501", "Complete"),
                        _row(1, "8 Oak Loop Boston MA", "Complete"),
                        _row(2, "40 Fir Trail Fremont CA", "Pending"),
                    ],
                }
            ],
        }

    def test_shape_channel_credits_value_grounded_and_drains_derived_parent(self) -> None:
        ctx = self._authoring_ctx(
            _criterion("output.widget_id", "the eight digit widget reference"),
            _criterion("output.sites", "the list of build sites"),
            _criterion("output.sites[].depot", "each depot postal location"),
            _criterion("output.sites[].phase", "each build stage token"),
        )
        ctx.copilot_config = self._shape_registry_config()
        page = self._shape_scout_page()

        no_registry_ctx = self._authoring_ctx(
            _criterion("output.widget_id", "the eight digit widget reference"),
            _criterion("output.sites", "the list of build sites"),
            _criterion("output.sites[].depot", "each depot postal location"),
            _criterion("output.sites[].phase", "each build stage token"),
        )
        assert mint_scout_observation_contract_for_ctx(no_registry_ctx, page, url=page["current_url"]) is None

        contract = mint_scout_observation_contract_for_ctx(ctx, page, url=page["current_url"])
        assert contract is not None

        with capture_logs() as logs:
            record_scouted_output_coverage(ctx, page, contract=contract)
        assert ctx.scouted_output_covered_paths == {
            "output.widget_id",
            "output.sites",
            "output.sites[].depot",
            "output.sites[].phase",
        }
        assert uncovered_requested_output_paths(ctx) == set()
        credited = next(entry for entry in logs if entry["event"] == "copilot_scouted_output_coverage_credited")
        assert credited["provenance"] == "value_grounded"
        assert any(path == "output.sites" for path in credited["value_grounded_paths"])

    def test_inspect_sourced_packet_shape_grounds_value_regardless_of_interaction(self) -> None:
        page = self._shape_scout_page()
        page["source_tool"] = "inspect_page_for_composition"
        page.pop("interaction_selector", None)

        # First-load capture (no prior interaction) grounds value by shape via witnessed content.
        landing_ctx = self._authoring_ctx(
            _criterion("output.widget_id", "the eight digit widget reference"),
            _criterion("output.sites", "the list of build sites"),
            _criterion("output.sites[].depot", "each depot postal location"),
            _criterion("output.sites[].phase", "each build stage token"),
        )
        landing_ctx.copilot_config = self._shape_registry_config()
        landing_ctx.scout_trajectory = []
        landing_contract = mint_scout_observation_contract_for_ctx(landing_ctx, page, url=page["current_url"])
        assert landing_contract is not None
        with capture_logs() as landing_logs:
            record_scouted_output_coverage(landing_ctx, page, contract=landing_contract)
        landing_credited = next(
            entry for entry in landing_logs if entry["event"] == "copilot_scouted_output_coverage_credited"
        )
        assert landing_credited["provenance"] == "value_grounded"

        ctx = self._authoring_ctx(
            _criterion("output.widget_id", "the eight digit widget reference"),
            _criterion("output.sites", "the list of build sites"),
            _criterion("output.sites[].depot", "each depot postal location"),
            _criterion("output.sites[].phase", "each build stage token"),
        )
        ctx.copilot_config = self._shape_registry_config()
        contract = mint_scout_observation_contract_for_ctx(ctx, page, url=page["current_url"])
        assert contract is not None

        with capture_logs() as logs:
            record_scouted_output_coverage(ctx, page, contract=contract)
        credited = next(entry for entry in logs if entry["event"] == "copilot_scouted_output_coverage_credited")
        assert credited["provenance"] == "value_grounded"
        assert any(path == "output.sites" for path in credited["value_grounded_paths"])

    def test_two_partial_contracts_accumulate_coverage(self) -> None:
        ctx = self._authoring_ctx(
            _criterion("output.overall_credentialing_result", "Overall Credentialing Result"),
            _criterion("output.npi", "NPI"),
        )
        first = self._kv_page(
            key_text="Overall Credentialing Result", url="https://example.com/p1", value_prose="Credentialed"
        )
        second = self._kv_page(key_text="NPI", url="https://example.com/p2", value_prose="1234567890")

        first_contract = mint_scout_observation_contract_for_ctx(
            ctx,
            first,
            url="https://example.com/p1",
        )
        record_scouted_output_coverage(ctx, first, contract=first_contract)
        assert ctx.scouted_output_covered_paths == {"output.overall_credentialing_result"}

        second_contract = mint_scout_observation_contract_for_ctx(
            ctx,
            second,
            url="https://example.com/p2",
        )
        record_scouted_output_coverage(ctx, second, contract=second_contract)
        assert ctx.scouted_output_covered_paths == {"output.overall_credentialing_result", "output.npi"}
        assert uncovered_requested_output_paths(ctx) == set()

    def test_realistic_multifield_dom_contract_binds_and_credits_value_grounded(self) -> None:
        page_evidence = parse_composition_html(
            LISTING_DETAIL_HTML,
            inspected_url=LISTING_DETAIL_URL,
            current_url=LISTING_DETAIL_URL,
        )
        ref_relations = [
            relation for relation in page_evidence["key_value_relations"] if relation["key_text"] == "Reference Number"
        ]
        assert len(ref_relations) == 1

        criteria = (
            _criterion("output.reference_number", "Reference Number"),
            _criterion("output.row_statuses", "Status"),
        )

        ctx = self._authoring_ctx(*criteria)
        contract = mint_scout_observation_contract_for_ctx(
            ctx,
            page_evidence,
            url=LISTING_DETAIL_URL,
        )
        assert contract is not None
        bindings_by_path = {binding.output_path: binding for binding in contract.bindings}
        # The contract binds the reference-number KV and the status column by label/header match,
        # crediting them as value_grounded from the realistic multi-field capture.
        assert set(bindings_by_path) == {"output.reference_number", "output.row_statuses"}
        assert bindings_by_path["output.reference_number"].kind == "key_value"
        assert bindings_by_path["output.row_statuses"].kind == "table_column"

        with capture_logs() as logs:
            record_scouted_output_coverage(ctx, page_evidence, contract=contract)
        assert {"output.reference_number", "output.row_statuses"} <= ctx.scouted_output_covered_paths
        assert uncovered_requested_output_paths(ctx) == set()
        credited = next(entry for entry in logs if entry["event"] == "copilot_scouted_output_coverage_credited")
        assert set(credited["value_grounded_paths"]) >= {"output.reference_number", "output.row_statuses"}

    def test_include_lexical_false_credits_contract_only(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "Document Name"))
        page = self._kv_page(
            key_text="Document Name", url="https://example.com/doc", value_prose="Document Name Resale Certificate"
        )
        contract = mint_scout_observation_contract_for_ctx(
            ctx,
            page,
            url="https://example.com/doc",
        )
        assert contract is not None

        record_scouted_output_coverage(ctx, page, include_lexical=False)
        assert ctx.scouted_output_covered_paths == set()

        record_scouted_output_coverage(ctx, page, contract=contract, include_lexical=False)
        assert ctx.scouted_output_covered_paths == {"output.document_name"}

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

    def test_registered_download_request_not_goal_complete_until_download_reached(self) -> None:
        # Post-run registered-download evidence is absent from the pre-run requested-output gate, so a
        # durable-entry+commit prefix (sign-in) would read goal-complete and land the mechanism-F latch
        # mid-scout — locking out imposition of the real download spine once the scout reaches it.
        ctx = self._authoring_ctx(_registered_download_criterion())
        assert uncovered_requested_output_paths(ctx) == set()
        assert ctx.reached_download_target is None
        assert synthesized_trajectory_is_goal_complete(ctx) is False
        ctx.reached_download_target = _download_target()
        assert synthesized_trajectory_is_goal_complete(ctx) is True

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

    def test_all_generic_token_path_still_requires_producer_plan(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.data", "the data is captured"))
        assert uncovered_requested_output_paths(ctx) == set()
        ctx.synthesized_block_offered_goal_complete = synthesized_trajectory_is_goal_complete(ctx)
        assert synthesized_trajectory_is_goal_complete(ctx) is False
        assert _should_force_synthesized_block_persistence(ctx) is False

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

    def test_log_only_rescout_steer_records_without_consuming(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "ENV", "local")
        monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", True)
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.update_workflow_called = True
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("output.document_name")
        consume_uncovered_output_reopen_event(ctx)

        steer = uncovered_output_reject_scout_steer_signal(ctx, "update_workflow")

        assert steer is None
        assert ctx.uncovered_output_rescout_steer_key is None
        event = ctx.author_time_gate_ablation_events[-1]
        assert event.gate_id == "uncovered_output_rescout_steer"
        assert event.reason_code == UNCOVERED_OUTPUT_RESCOUT_STEER_REASON_CODE
        assert event.blocked_tool == "update_workflow"
        assert event.fingerprint == (
            f"{ctx.latest_recorded_build_test_outcome.structural_failure_identity}|output.document_name"
        )
        assert event.log_only is True

    def test_steer_yields_to_live_ladder_and_one_shot_key_survives(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.update_workflow_called = True
        ctx.latest_recorded_build_test_outcome = _author_time_reject_outcome("output.document_name")
        consume_uncovered_output_reopen_event(ctx)
        ctx.output_contract_actuation_by_signature = {"sig_a": OutputContractAdvisoryState.GRANTED}
        ctx.output_contract_actuation_count_by_signature = {}

        assert uncovered_output_reject_scout_steer_signal(ctx, "update_and_run_blocks") is None
        assert ctx.uncovered_output_rescout_steer_key is None

        ctx.output_contract_actuation_by_signature = {"sig_a": OutputContractAdvisoryState.CONSUMED}
        steer = uncovered_output_reject_scout_steer_signal(ctx, "update_and_run_blocks")
        assert isinstance(steer, CopilotToolBlockerSignal)
        assert ctx.uncovered_output_rescout_steer_key is not None

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

    def test_coverage_reopen_without_plan_does_not_refresh_offer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement.synthesize_code_block",
            lambda *args, **kwargs: SynthesizedCodeBlock(code="await page.click('button')"),
        )
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        ctx.update_workflow_called = True
        assert _maybe_synthesized_block_offer_msg(ctx) is None
        ctx.synthesized_block_reopened_for_output_coverage = True
        assert _maybe_synthesized_block_offer_msg(ctx) is None

    def test_post_hook_failure_rolls_back_coverage_credit(self) -> None:
        assert "scouted_output_covered_paths" in _POST_HOOK_CONTEXT_ROLLBACK_FIELDS
        assert "synthesized_block_reopened_for_output_coverage" in _POST_HOOK_CONTEXT_ROLLBACK_FIELDS
        assert "synthesized_business_required_parameter_keys" in _POST_HOOK_CONTEXT_ROLLBACK_FIELDS
        ctx = _Ctx()
        ctx.scouted_output_covered_paths = {"output.document_name"}
        ctx.synthesized_business_required_parameter_keys = {"service_address"}
        ctx.synthesized_block_reopened_for_output_coverage = False
        snapshot = _snapshot_post_hook_context(ctx)
        ctx.scouted_output_covered_paths.add("output.leaked")
        ctx.synthesized_business_required_parameter_keys.add("leaked_input")
        ctx.synthesized_block_reopened_for_output_coverage = True
        _restore_post_hook_context(ctx, snapshot)
        assert ctx.scouted_output_covered_paths == {"output.document_name"}
        assert ctx.synthesized_business_required_parameter_keys == {"service_address"}
        assert ctx.synthesized_block_reopened_for_output_coverage is False

    def test_post_hook_failure_rolls_back_capture_obligation_credit(self) -> None:
        assert "never_captured_obligation" in _POST_HOOK_CONTEXT_ROLLBACK_FIELDS
        assert "synthesized_block_reopened_for_capture_obligation" in _POST_HOOK_CONTEXT_ROLLBACK_FIELDS
        ctx = _Ctx()
        armed = NeverCapturedObligation(
            identity_digest="identity",
            turn_id="turn-a",
            draft_fingerprint="draft",
            block_label="submit",
            site="whole_trajectory",
            method="click",
            normalized_receiver="page.locator('#submit')",
            call_shape_digest="shape",
            expected_tool_name="click",
            armed_after_trajectory_index=0,
        )
        ctx.never_captured_obligation = armed
        snapshot = _snapshot_post_hook_context(ctx)
        ctx.never_captured_obligation = NeverCapturedObligation(
            **{
                **armed.__dict__,
                "captured_trajectory_index": 1,
                "state": "captured",
            }
        )
        ctx.synthesized_block_reopened_for_capture_obligation = True

        _restore_post_hook_context(ctx, snapshot)

        assert ctx.never_captured_obligation == armed
        assert ctx.synthesized_block_reopened_for_capture_obligation is False

    def test_post_hook_failure_rolls_back_scout_observation_contract(self) -> None:
        assert "scout_observation_contract" in _POST_HOOK_CONTEXT_ROLLBACK_FIELDS
        ctx = _Ctx()
        ctx.scout_observation_contract = None
        snapshot = _snapshot_post_hook_context(ctx)
        ctx.scout_observation_contract = object()
        _restore_post_hook_context(ctx, snapshot)
        assert ctx.scout_observation_contract is None

    def test_both_surfaces_grounded_still_blocks_mutating_tool(self) -> None:
        ctx = self._authoring_ctx(
            _criterion("output.visitors", "Visitors"),
            _criterion("output.signups", "Signups"),
        )
        surface_one = self._kv_page(
            key_text="Visitors",
            url="https://analytics.example.com/dashboard",
            value_prose="Visitors 1,234 recorded this week",
        )
        first_contract = mint_scout_observation_contract_for_ctx(
            ctx, surface_one, url="https://analytics.example.com/dashboard"
        )
        record_scouted_output_coverage(ctx, surface_one, contract=first_contract)

        surface_two = self._kv_page(
            key_text="Signups",
            url="https://analytics.example.com/query",
            value_prose="Signups 987 total this week",
        )
        second_contract = mint_scout_observation_contract_for_ctx(
            ctx, surface_two, url="https://analytics.example.com/query"
        )
        record_scouted_output_coverage(ctx, surface_two, contract=second_contract)

        assert ctx.scouted_output_covered_paths == {"output.visitors", "output.signups"}
        assert uncovered_requested_output_paths(ctx) == set()

        assert _should_block_mutating_tool_after_synthesized_offer(ctx, "click") is True
        signal = synthesized_block_persistence_signal(ctx, "click")
        assert signal is not None
        assert signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE

    def test_rekeyed_requested_output_compiles_the_offer_recipe_named_from_its_label(self) -> None:
        # Without the rekey fallback no label reaches the plan, so the offer is skipped and the
        # schema is left to the agent to invent.
        rekeyed = CompletionCriterion(
            id="slot0",
            outcome="Document Name",
            output_path=None,
            requested_output_evidence_source="runtime_output",
            requested_output_floor_rekeyed=True,
            floor_rekeyed_from_path="output.request_slot_abc_00",
        )
        ctx = self._authoring_ctx(rekeyed)
        self._attach_document_plan(ctx, step=6)
        ctx.synthesized_block_offered = False

        message = _maybe_synthesized_block_offer_msg(ctx)

        assert message is not None
        content = str(message["content"])
        assert 'return {"output": {"document_name": _extraction_value_0}}' in content
        assert "request_slot_abc_00" not in content

    def test_requested_output_without_a_label_leaves_the_ask_legitimate(self) -> None:
        # An underivable field yields no plan, so a clarification about it stays legitimate.
        unlabelled = CompletionCriterion(
            id="slot0",
            outcome="",
            output_path=None,
            requested_output_evidence_source="runtime_output",
            requested_output_floor_rekeyed=True,
            floor_rekeyed_from_path="output.request_slot_abc_00",
        )
        ctx = self._authoring_ctx(unlabelled)
        self._attach_document_plan(ctx, step=6)

        assert requested_scalar_output_extraction_plan(ctx) is None

    def test_floor_rekeyed_runtime_output_stays_owed_until_grounded(self) -> None:
        # The rekey clears output_path but keeps floor_rekeyed_from_path; keyed only on the former,
        # both outputs vanish from the requested set and the blocker forecloses scouting.
        rekeyed = [
            CompletionCriterion(
                id=f"slot{index}",
                outcome=outcome,
                output_path=None,
                requested_output_evidence_source="runtime_output",
                requested_output_floor_rekeyed=True,
                floor_rekeyed_from_path=f"output.request_slot_b97f_{index:02d}",
            )
            for index, outcome in enumerate(["number of website visitors", "number of new signups"])
        ]
        ctx = self._authoring_ctx(*rekeyed)

        # Provenance stands in for the cleared path, so both stay owed and neither is flagged.
        assert uncovered_requested_output_paths(ctx) == {
            "output.request_slot_b97f_00",
            "output.request_slot_b97f_01",
        }
        assert pre_run_gated_outputs_without_path(ctx) == ()

        surface_one = self._kv_page(
            key_text="Website visitors",
            url="https://analytics.example.com/web-analytics",
            value_prose="Website visitors 9,420 recorded for the past 7 days",
        )
        contract = mint_scout_observation_contract_for_ctx(
            ctx, surface_one, url="https://analytics.example.com/web-analytics"
        )
        record_scouted_output_coverage(ctx, surface_one, contract=contract)

        # Coverage keys on the outcome text, since the digest leaf carries no groundable tokens.
        assert uncovered_requested_output_paths(ctx) == {"output.request_slot_b97f_01"}
        ctx.synthesized_block_offered_goal_complete = synthesized_trajectory_is_goal_complete(ctx)
        assert _should_block_mutating_tool_after_synthesized_offer(ctx, "click") is False
        assert synthesized_block_persistence_signal(ctx, "click") is None

    def test_pathless_runtime_output_criterion_reaching_enforcement_is_flagged(self) -> None:
        # A runtime-output criterion reaching enforcement with no identity is surfaced, not dropped.
        pathless = CompletionCriterion(
            id="c0",
            outcome="number of new signups is extracted for the past 7 days",
            output_path=None,
            requested_output_evidence_source="runtime_output",
        )
        flagged = pre_run_gated_outputs_without_path(self._authoring_ctx(pathless))
        assert [criterion.id for criterion in flagged] == ["c0"]

        with_path = _criterion("output.new_signups", "number of new signups is extracted")
        assert pre_run_gated_outputs_without_path(self._authoring_ctx(with_path)) == ()


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


class TestCredentialFlowGoalComplete:
    _LOGIN_URL = "https://portal.example.test/login"
    _PASSWORD_URL = "https://portal.example.test/password"

    @staticmethod
    def _username_fill(source_url: str = "https://portal.example.test/login") -> dict[str, object]:
        return {
            "tool_name": "fill_credential_field",
            "credential_id": "cred_1",
            "credential_field": "username",
            "selector": "#user",
            "source_url": source_url,
        }

    @staticmethod
    def _password_fill(source_url: str = "https://portal.example.test/password") -> dict[str, object]:
        return {
            "tool_name": "fill_credential_field",
            "credential_id": "cred_1",
            "credential_field": "password",
            "selector": "#pass",
            "source_url": source_url,
        }

    @staticmethod
    def _submit(source_url: str, accessible_name: str) -> dict[str, object]:
        return {
            "tool_name": "click",
            "selector": "button[type='submit']",
            "accessible_name": accessible_name,
            "source_url": source_url,
        }

    def _two_screen_first_page(self) -> list[dict[str, object]]:
        return [self._username_fill(), self._submit(self._LOGIN_URL, "Continue")]

    def _two_screen_full_login(self) -> list[dict[str, object]]:
        return [*self._two_screen_first_page(), self._password_fill(), self._submit(self._PASSWORD_URL, "Sign in")]

    def _ctx_with_inventory(
        self,
        trajectory: list[dict[str, object]],
        *,
        inventory: dict[str, frozenset[str]] | None = None,
        observed_at_index: int | None = None,
        observed_password_control: bool = False,
    ) -> _Ctx:
        ctx = _Ctx()
        ctx.turn_intent = TurnIntent(
            mode=TurnIntentMode.BUILD,
            authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
        )
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        for position, item in enumerate(trajectory):
            item.setdefault("trajectory_index", position)
        ctx.scout_trajectory = trajectory
        ctx.scouted_credential_field_inventory_by_credential_id = inventory or {}
        ctx.last_scout_observation_trajectory_index = observed_at_index
        ctx.last_scout_observation_has_password_control = observed_password_control
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = len(trajectory)
        ctx.synthesized_block_offered_goal_complete = synthesized_trajectory_is_goal_complete(ctx)
        return ctx

    def test_half_login_with_unobserved_second_screen_is_incomplete(self) -> None:
        ctx = self._ctx_with_inventory(
            self._two_screen_first_page(),
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is False
        assert _should_force_synthesized_block_persistence(ctx) is False
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    def test_full_login_with_post_fill_submit_is_complete(self) -> None:
        ctx = self._ctx_with_inventory(
            self._two_screen_full_login(),
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is True
        assert _should_force_synthesized_block_persistence(ctx) is True
        assert synthesized_block_persistence_signal(ctx, "evaluate") is not None

    def test_login_only_is_incomplete_when_runtime_outputs_were_floor_rekeyed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = self._ctx_with_inventory(
            self._two_screen_full_login(),
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        ctx.completion_criteria_turn_state = _turn_state(
            CompletionCriterion(
                id="request-id",
                outcome="the request id is output",
                level="run",
                requested_output_floor_rekeyed=True,
                floor_rekeyed_from_path="output.request_id",
            )
        )

        assert synthesized_trajectory_is_goal_complete(ctx) is False
        assert _should_force_synthesized_block_persistence(ctx) is False
        assert synthesized_block_persistence_signal(ctx, "click", {"selector": "#gasCreate"}) is None
        ctx.synthesized_block_offered = False
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement.synthesize_code_block",
            lambda *args, **kwargs: SynthesizedCodeBlock(code="await page.locator('#login').click()"),
        )
        assert _maybe_synthesized_block_offer_msg(ctx) is None

    def test_floor_rekeyed_runtime_output_requires_coverage_after_post_login_business_commit(self) -> None:
        trajectory = [
            *self._two_screen_full_login(),
            {
                "tool_name": "click",
                "selector": "button[data-action='gasCreate']",
                "accessible_name": "Create QuickConnect",
                "source_url": "https://portal.example.test/home",
            },
            {
                "tool_name": "type_text",
                "selector": "#gasAddress",
                "typed_value": "77 Gaslight Way",
                "source_url": "https://portal.example.test/quickconnect",
            },
            {
                "tool_name": "click",
                "selector": "button[data-action='gasSubmit']",
                "accessible_name": "Submit",
                "source_url": "https://portal.example.test/quickconnect",
            },
        ]
        ctx = self._ctx_with_inventory(
            trajectory,
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        ctx.completion_criteria_turn_state = _turn_state(
            CompletionCriterion(
                id="request-id",
                outcome="the request id is output",
                level="run",
                requested_output_floor_rekeyed=True,
                floor_rekeyed_from_path="output.request_id",
            )
        )

        assert synthesized_trajectory_reaches_goal(ctx) is True
        assert uncovered_requested_output_paths(ctx) == {"output.request_id"}
        assert synthesized_trajectory_is_goal_complete(ctx) is False

        ctx.scouted_output_covered_paths.add("output.request_id")
        ctx.flow_evidence = [
            {
                "step": len(trajectory),
                "reached_via": "interaction",
                "had_bounded_schema": True,
                "evidence": {
                    "source_tool": "scout_interaction",
                    "interaction_tool": "click",
                    "interaction_selector": "button[data-action='gasSubmit']",
                    "inspection_warnings": [],
                    "result_containers_truncated": False,
                    "key_value_relations_truncated": False,
                    "key_value_relations": [
                        {
                            "key_text": "the request id is output",
                            "container_selector": ".request-id-kv",
                            "container_match_count": 1,
                            "container_position": 0,
                            "value_child_index": 1,
                            "direct_child_count": 2,
                            "visible": True,
                            "value_visible": True,
                        }
                    ],
                    "result_containers": [],
                },
            }
        ]

        assert uncovered_requested_output_paths(ctx) == set()
        assert synthesized_trajectory_is_goal_complete(ctx) is True

    def test_floor_rekeyed_runtime_output_rejects_create_then_submit_without_business_fill(self) -> None:
        trajectory = [
            *self._two_screen_full_login(),
            {
                "tool_name": "click",
                "selector": "button[data-action='gasCreate']",
                "accessible_name": "Create QuickConnect",
                "source_url": "https://portal.example.test/home",
            },
            {
                "tool_name": "click",
                "selector": "button[data-action='gasSubmit']",
                "accessible_name": "Submit",
                "source_url": "https://portal.example.test/quickconnect",
            },
        ]
        ctx = self._ctx_with_inventory(
            trajectory,
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        ctx.completion_criteria_turn_state = _turn_state(
            CompletionCriterion(
                id="request-id",
                outcome="the request id is output",
                level="run",
                requested_output_floor_rekeyed=True,
                floor_rekeyed_from_path="output.request_id",
            )
        )
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="submit-request",
                    outcome="the QuickConnect request is submitted",
                    kind="terminal_action",
                    terminal_action_family="request",
                    level="run",
                )
            ]
        )
        ctx.synthesized_block_offered = True
        ctx.synthesized_block_offered_trajectory_len = len(trajectory)
        ctx.synthesized_block_offered_goal_complete = True

        assert synthesized_trajectory_is_goal_complete(ctx) is False
        assert _should_force_synthesized_block_persistence(ctx) is False
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    def test_request_terminal_action_does_not_offer_on_create_then_table_navigation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trajectory = [
            *self._two_screen_full_login(),
            {
                "tool_name": "click",
                "selector": "button[data-action='gasCreate']",
                "accessible_name": "Create QuickConnect",
                "source_url": "https://portal.example.test/home",
            },
            {
                "tool_name": "click",
                "selector": "button[data-action='gasTable']",
                "accessible_name": "My QuickConnects",
                "source_url": "https://portal.example.test/quickconnect",
            },
            {
                "tool_name": "click",
                "selector": "a[data-action='quickconnects']",
                "accessible_name": "QuickConnects",
                "source_url": "https://portal.example.test/quickconnect/table",
            },
        ]
        ctx = self._ctx_with_inventory(
            trajectory,
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        ctx.completion_criteria_turn_state = None
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(
                    id="submit-request",
                    outcome="the QuickConnect request is submitted",
                    kind="terminal_action",
                    terminal_action_family="request",
                    level="run",
                )
            ]
        )
        ctx.synthesized_block_offered = False

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement.synthesize_code_block",
            lambda *args, **kwargs: SynthesizedCodeBlock(code="await page.locator('#gasTable').click()"),
        )
        assert _maybe_synthesized_block_offer_msg(ctx) is None

    def test_username_only_flow_completes_after_no_password_control_observation(self) -> None:
        trajectory = self._two_screen_first_page()
        ctx = self._ctx_with_inventory(
            trajectory,
            inventory={"cred_1": frozenset({"username", "password"})},
            observed_at_index=len(trajectory) - 1,
            observed_password_control=False,
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is True
        assert _should_force_synthesized_block_persistence(ctx) is True

    def test_observed_password_screen_keeps_flow_incomplete(self) -> None:
        trajectory = self._two_screen_first_page()
        ctx = self._ctx_with_inventory(
            trajectory,
            inventory={"cred_1": frozenset({"username", "password"})},
            observed_at_index=len(trajectory) - 1,
            observed_password_control=True,
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_observation_before_submit_does_not_drop_password_requirement(self) -> None:
        trajectory = self._two_screen_first_page()
        ctx = self._ctx_with_inventory(
            trajectory,
            inventory={"cred_1": frozenset({"username", "password"})},
            observed_at_index=0,
            observed_password_control=False,
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_unmatched_incidental_click_before_observation_keeps_password_demand(self) -> None:
        trajectory = [
            self._username_fill(),
            {
                "tool_name": "click",
                "selector": "#cookie-accept",
                "accessible_name": "Accept",
                "source_url": "https://consent.example.test/banner",
            },
            self._submit(self._LOGIN_URL, "Continue"),
        ]
        ctx = self._ctx_with_inventory(
            trajectory,
            inventory={"cred_1": frozenset({"username", "password"})},
            observed_at_index=1,
            observed_password_control=False,
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_non_dict_trajectory_entry_does_not_release_demand_early(self) -> None:
        trajectory: list[Any] = [{**self._username_fill(), "trajectory_index": 0}, "scout-note"]
        ctx = self._ctx_with_inventory(
            [self._username_fill()],
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        ctx.scout_trajectory = trajectory
        _record_scout_page_observation(ctx, {"forms": [{"fields": [{"selector": "#user", "type": "text"}]}]})
        trajectory.append({**self._submit(self._LOGIN_URL, "Continue"), "trajectory_index": 2})
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_observation_after_submit_with_non_dict_entry_releases_demand(self) -> None:
        trajectory: list[Any] = [
            {**self._username_fill(), "trajectory_index": 0},
            "scout-note",
            {**self._submit(self._LOGIN_URL, "Continue"), "trajectory_index": 2},
        ]
        ctx = self._ctx_with_inventory(
            [self._username_fill()],
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        ctx.scout_trajectory = trajectory
        _record_scout_page_observation(ctx, {"forms": [{"fields": [{"selector": "#user", "type": "text"}]}]})
        assert synthesized_trajectory_is_goal_complete(ctx) is True

    def test_eviction_does_not_reorder_observation_past_submit(self) -> None:
        fill_index = _MAX_SCOUTED_INTERACTIONS - 1
        trajectory: list[dict[str, object]] = [
            {
                "tool_name": "click",
                "selector": f"#step-{index}",
                "source_url": "https://portal.example.test/browse",
                "trajectory_index": index,
            }
            for index in range(fill_index)
        ]
        trajectory.append({**self._username_fill(), "trajectory_index": fill_index})
        ctx = self._ctx_with_inventory([], inventory={"cred_1": frozenset({"username", "password"})})
        ctx.scout_trajectory = trajectory
        _record_scout_page_observation(ctx, {"forms": [{"fields": [{"selector": "#user", "type": "text"}]}]})
        trajectory = list(trajectory)
        trajectory.append({**self._submit(self._LOGIN_URL, "Continue"), "trajectory_index": fill_index + 1})
        ctx.scout_trajectory = _capped_with_eviction_accounting(trajectory, collection="scout_trajectory")
        assert len(ctx.scout_trajectory) == _MAX_SCOUTED_INTERACTIONS
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_password_only_reauth_completes(self) -> None:
        ctx = self._ctx_with_inventory(
            [self._password_fill(), self._submit(self._PASSWORD_URL, "Sign in")],
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is True

    def test_username_only_credential_without_password_completes(self) -> None:
        ctx = self._ctx_with_inventory(
            self._two_screen_first_page(),
            inventory={"cred_1": frozenset({"username"})},
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is True

    def test_legacy_session_without_inventory_degrades_to_filled_fields(self) -> None:
        ctx = self._ctx_with_inventory(self._two_screen_first_page(), inventory={})
        assert synthesized_trajectory_is_goal_complete(ctx) is True

    def test_totp_only_continuation_falls_through_to_shape_heuristic(self) -> None:
        trajectory = [
            {
                "tool_name": "fill_credential_field",
                "credential_id": "cred_1",
                "credential_field": "totp",
                "selector": "#totp",
                "source_url": self._PASSWORD_URL,
            },
            self._submit(self._PASSWORD_URL, "Verify"),
        ]
        ctx = self._ctx_with_inventory(trajectory, inventory={"cred_1": frozenset({"username", "password"})})
        assert synthesized_trajectory_is_goal_complete(ctx) is True

    def test_filled_password_without_post_fill_submit_is_incomplete(self) -> None:
        ctx = self._ctx_with_inventory(
            [self._username_fill(), self._submit(self._LOGIN_URL, "Continue"), self._password_fill()],
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_mixed_credentials_incomplete_until_both_flows_finish(self) -> None:
        second_fill = {
            "tool_name": "fill_credential_field",
            "credential_id": "cred_2",
            "credential_field": "username",
            "selector": "#user2",
            "source_url": self._PASSWORD_URL,
        }
        trajectory = [*self._two_screen_full_login(), second_fill, self._submit(self._PASSWORD_URL, "Next")]
        ctx = self._ctx_with_inventory(
            trajectory,
            inventory={
                "cred_1": frozenset({"username", "password"}),
                "cred_2": frozenset({"username", "password"}),
            },
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_download_target_does_not_bypass_credential_flow(self) -> None:
        ctx = self._ctx_with_inventory(
            self._two_screen_first_page(),
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        ctx.reached_download_target = ReachedDownloadTarget(
            selector="a.report",
            affordance_text="Report",
            download_kind="extension",
            source_step="trajectory_recency",
            already_registered=False,
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_mutating_tools_admitted_while_credential_flow_incomplete(self) -> None:
        ctx = self._ctx_with_inventory(
            self._two_screen_first_page(),
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        assert _should_block_mutating_tool_after_synthesized_offer(ctx, "click") is False
        assert synthesized_block_persistence_signal(ctx, "click") is None
        assert synthesized_block_persistence_signal(ctx, "type_text") is None

    def test_mutating_tools_blocked_again_once_flow_completes(self) -> None:
        ctx = self._ctx_with_inventory(
            self._two_screen_full_login(),
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        assert _should_block_mutating_tool_after_synthesized_offer(ctx, "click") is True
        assert synthesized_block_persistence_signal(ctx, "click") is not None


class TestCredentialScoutReopen:
    def _offered_complete_ctx(self) -> _Ctx:
        helper = TestCredentialFlowGoalComplete()
        return helper._ctx_with_inventory(
            helper._two_screen_full_login(),
            inventory={"cred_1": frozenset({"username", "password"})},
        )

    def test_arm_is_one_shot_per_identity_digest(self) -> None:
        ctx = make_copilot_context()
        assert arm_credential_scout_reopen(ctx, "identity-1") is True
        assert ctx.synthesized_block_reopened_for_credential_scout is True
        assert synthesized_persistence_reopened(ctx) is True

        ctx.synthesized_block_reopened_for_credential_scout = False
        assert arm_credential_scout_reopen(ctx, "identity-1") is False
        assert ctx.synthesized_block_reopened_for_credential_scout is False
        assert synthesized_persistence_reopened(ctx) is False

        assert arm_credential_scout_reopen(ctx, "identity-2") is True
        assert ctx.synthesized_block_reopened_for_credential_scout is True

    def test_reopen_admits_evaluate_while_offer_is_goal_complete(self) -> None:
        ctx = self._offered_complete_ctx()
        assert synthesized_block_persistence_signal(ctx, "evaluate") is not None
        ctx.synthesized_block_reopened_for_credential_scout = True
        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

    def test_reopen_admission_registers_precedence_claim(self) -> None:
        ctx = self._offered_complete_ctx()
        ctx.synthesized_block_reopened_for_credential_scout = True

        assert synthesized_block_persistence_signal(ctx, "evaluate") is None

        assert ctx.turn_ownership is not None
        assert TurnClaimant.CREDENTIAL_SCOUT_REOPEN in ctx.turn_ownership.claims
        assert current_turn_owner(ctx) is None

    def test_reopen_reopens_offer_refresh_window(self) -> None:
        ctx = self._offered_complete_ctx()
        ctx.update_workflow_called = True
        assert _should_force_synthesized_block_persistence(ctx) is False
        ctx.synthesized_block_reopened_for_credential_scout = True
        assert _should_force_synthesized_block_persistence(ctx) is True


class TestNeverCapturedObligationAdmission:
    def _offered_complete_ctx(self) -> _Ctx:
        helper = TestCredentialFlowGoalComplete()
        ctx = helper._ctx_with_inventory(
            helper._two_screen_full_login(),
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        ctx.turn_id = "turn-capture"
        ctx.never_captured_obligation = NeverCapturedObligation(
            identity_digest="capture-identity",
            turn_id=ctx.turn_id,
            draft_fingerprint="draft",
            block_label="submit",
            site="whole_trajectory",
            method="click",
            normalized_receiver="page.locator('#gasSubmit')",
            call_shape_digest="shape",
            expected_tool_name="click",
            armed_after_trajectory_index=0,
        )
        return ctx

    def test_exact_target_admission_registers_precedence_claim(self) -> None:
        ctx = self._offered_complete_ctx()

        assert synthesized_block_persistence_signal(ctx, "click", {"selector": "#gasSubmit"}) is None

        assert ctx.turn_ownership is not None
        assert TurnClaimant.CAPTURE_OBLIGATION_REOPEN in ctx.turn_ownership.claims
        assert current_turn_owner(ctx) is None

    def test_same_tool_for_different_target_remains_blocked(self) -> None:
        ctx = self._offered_complete_ctx()

        signal = synthesized_block_persistence_signal(ctx, "click", {"selector": "#unrelated"})

        assert isinstance(signal, CopilotToolBlockerSignal)
        assert signal.internal_reason_code == SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE
        assert ctx.turn_ownership is None
