"""Tests for enforcement pruning and null-data handling.

These cover three regressions observed in trace 019d7b5c884dff0ff648680b9f31f715:
  1. Extraction returning all-null fields was treated as success.
  2. Context grew linearly because old tool outputs kept full content.
  3. No escalation when the agent looped on the same null-data failure.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents import RunConfig
from structlog.testing import capture_logs

from skyvern.config import Settings, settings
from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
)
from skyvern.forge.sdk.copilot.build_phase import BuildPhase
from skyvern.forge.sdk.copilot.build_test_outcome import (
    PostRunPagePathFailure,
    PostRunPagePathTarget,
    RecordedBuildTestOutcome,
    _post_run_page_path_failure,
    bind_post_run_page_path_failure,
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
    _RECENT_TOOL_OUTPUT_CHAR_CAP,
    KEEP_RECENT_TOOL_OUTPUTS,
    TOTAL_TIMEOUT_SECONDS,
    _mark_copilot_total_timeout,
    _mark_copilot_total_timeout_if_elapsed,
    _maybe_synthesized_block_offer_msg,
    _needs_suspicious_success_nudge,
    _prune_input_list,
    _recover_from_context_overflow,
    _requested_output_paths_for_ctx,
    _should_force_advisory_run_dispatch,
    _summarize_tool_output,
    aggressive_prune,
    arm_credential_scout_reopen,
    enforcement_decision,
    mint_scout_observation_contract_for_ctx,
    pre_run_gated_outputs_without_path,
    record_scouted_output_coverage,
    requested_scalar_output_extraction_plan,
    run_with_enforcement,
    synthesized_goal_completion_landing_pending,
    synthesized_offer_reopened_for_extraction_plan,
    synthesized_persistence_reopened,
    synthesized_persistence_reopened_after_failed_run,
    synthesized_trajectory_is_goal_complete,
    synthesized_trajectory_reaches_goal,
    uncovered_requested_output_paths,
)
from skyvern.forge.sdk.copilot.mcp_adapter import (
    _POST_HOOK_CONTEXT_ROLLBACK_FIELDS,
    _restore_post_hook_context,
    _snapshot_post_hook_context,
)
from skyvern.forge.sdk.copilot.output_contracts import (
    OutputContractAdvisoryState,
)
from skyvern.forge.sdk.copilot.output_extraction_plan import ShapeExpectation, ValueCardinality, ValueShape
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.tools import (
    _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
    _analyze_run_blocks,
    _is_meaningful_extracted_data,
    _record_run_blocks_result,
    _record_workflow_update_result,
)
from skyvern.forge.sdk.copilot.tools._shared import TOTAL_TIMEOUT_SECONDS as shared_total_timeout_seconds
from skyvern.forge.sdk.copilot.tools.page_observation import _record_composition_page_observation
from skyvern.forge.sdk.copilot.tools.scouting import (
    _MAX_SCOUTED_INTERACTIONS,
    _capped_with_eviction_accounting,
    _mark_post_run_page_observed,
    _record_scout_page_observation,
)
from skyvern.forge.sdk.copilot.turn_halt import stash_turn_halt_from_blocker_signal
from skyvern.forge.sdk.copilot.turn_intent import RequiredContextKey, TurnIntent, TurnIntentAuthority, TurnIntentMode
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
        self.persisted_draft_browser_calls = None
        self.test_after_update_done = False
        self.post_update_nudge_count = 0
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
        self.synthesized_block_reopened_for_credential_scout = False
        self.credential_scout_rescout_context_key = None
        self.synthesized_goal_complete_landed = False
        self.impose_synthesized_code_block = False
        self.scouted_output_covered_paths: set[str] = set()
        self.scout_observed_terminal_criterion_ids: set[str] = set()
        self.scout_observation_contract: object | None = None
        self.flow_evidence: list[dict[str, object]] = []
        self.last_bound_requested_output_extraction_plan = None
        self.requested_output_designations: list[dict[str, object]] = []
        self.composition_page_evidence = None
        self.copilot_config: CopilotConfig | None = None
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

        assert _maybe_synthesized_block_offer_msg(ctx) is not None
        assert ctx.synthesized_block_offered_trajectory_len == len(trajectory)

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
    from skyvern.forge.sdk.copilot.enforcement import CopilotUnrecoverableToolError

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
        enforcement_decision(ctx)

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


# tail_size samples the boundaries that change behaviour: below one pair, exactly one
# pair, either side of KEEP_RECENT_TOOL_OUTPUTS, the production default, and longer
# than the 21 non-screenshot items _tool_history(10) builds.
@pytest.mark.parametrize("pair_count", [1, 2, 4, 8, 10])
@pytest.mark.parametrize("tail_size", [1, 2, 3, 4, 7, 25])
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
@pytest.mark.parametrize("tail_size", [1, 2, 3, 4, 7, 25])
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


def test_recent_code_sized_output_survives_untruncated() -> None:
    # A code-bearing result in the recent window must reach the model whole; the cap
    # is a pathological-payload tripwire, never a ration on legitimate code payloads.
    code_sized = json.dumps({"ok": True, "data": {"code": "await page.click()\n" * 400}})
    assert 2000 < len(code_sized) < _RECENT_TOOL_OUTPUT_CHAR_CAP
    items = [_fco("c0", code_sized)]

    pruned = _prune_input_list(items)
    assert pruned[0]["output"] == code_sized


def test_scout_budgets_stay_within_the_recent_window_cap() -> None:
    from skyvern.forge.sdk.copilot.tools.scouting import _SCOUT_RECON_RESULT_CHAR_CAP, _SCOUT_RESULT_CHAR_CAP

    # The shed-never-slice guarantee holds only while every scout budget fits inside
    # the transcript's recent-window cap.
    assert _SCOUT_RESULT_CHAR_CAP <= _SCOUT_RECON_RESULT_CHAR_CAP <= _RECENT_TOOL_OUTPUT_CHAR_CAP


def test_old_code_output_synopsis_names_elided_code_size() -> None:
    code = "await page.click()\n" * 300
    old_output = json.dumps({"ok": True, "data": {"code": code}})
    items = [_fco("c_old", old_output)] + [_fco(f"c{i}", '{"ok":true}') for i in range(KEEP_RECENT_TOOL_OUTPUTS)]

    pruned = _prune_input_list(items)
    synopsis = json.loads(pruned[0]["output"])
    assert synopsis["code_chars_elided"] == len(code)


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
    import structlog.testing

    # Over-cap JSON in the most-recent slot should be head-truncated,
    # NOT replaced with a summary.
    large = '{"ok":true,"data":{"value":"' + ("y" * (_RECENT_TOOL_OUTPUT_CHAR_CAP + 1000)) + '"}}'
    items = [_fco("c_recent", large)]
    with structlog.testing.capture_logs() as logs:
        pruned = _prune_input_list(items)
    out = pruned[0]["output"]
    assert out.startswith('{"ok":true,')
    assert out.endswith("\n... [truncated]")
    assert len(out) <= _RECENT_TOOL_OUTPUT_CHAR_CAP + 20
    assert any(entry["event"] == "copilot_recent_tool_output_truncated" for entry in logs)


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
        ctx = self._make_ctx()
        assert enforcement_decision(ctx) is None

    def test_post_navigate_nudge(self) -> None:
        ctx = self._make_ctx(navigate_called=True, observation_after_navigate=False)
        nudge = enforcement_decision(ctx)
        assert nudge is not None
        assert nudge.rule == "post_navigate"
        assert ctx.navigate_enforcement_done is True

    def test_post_navigate_only_fires_once(self) -> None:
        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            navigate_enforcement_done=True,
        )
        assert enforcement_decision(ctx) is None

    def test_post_update_nudge(self) -> None:
        ctx = self._make_ctx(update_workflow_called=True, test_after_update_done=False)
        nudge = enforcement_decision(ctx)
        assert nudge is not None
        assert nudge.rule == "post_update"

    def test_navigate_takes_priority_over_update(self) -> None:
        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            update_workflow_called=True,
            test_after_update_done=False,
        )
        nudge = enforcement_decision(ctx)
        assert nudge is not None
        assert nudge.rule == "post_navigate"

    def test_ask_question_passes_response_enforcement(self) -> None:
        import json

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=1,
            user_message="Go to france.fr and then download all french regulations",
        )
        ask = MagicMock()
        ask.final_output = json.dumps({"type": "ASK_QUESTION", "user_response": "Which source?"})
        ask.new_items = []
        assert enforcement_decision(ctx, ask) is None

    def test_plain_labeled_ask_question_passes_response_enforcement(self) -> None:
        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=1,
            user_message="Go to france.fr and then download all french regulations",
        )
        ask = MagicMock()
        ask.final_output = "ASK_QUESTION\nWhich source?"
        ask.new_items = []
        assert enforcement_decision(ctx, ask) is None

    def test_explore_without_workflow_nudge(self) -> None:
        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=False,
            test_after_update_done=False,
        )
        nudge = enforcement_decision(ctx)
        assert nudge.rule == "post_explore_without_workflow"
        assert ctx.explore_without_workflow_nudge_count == 1

    def test_explore_without_workflow_not_when_update_called(self) -> None:
        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=True,
            test_after_update_done=False,
        )
        nudge = enforcement_decision(ctx)
        assert nudge.rule == "post_update"
        assert ctx.explore_without_workflow_nudge_count == 0

    def test_update_without_test_allowed_for_explicit_untested_draft(self) -> None:
        ctx = self._make_ctx(
            allow_untested_workflow_draft=True,
            update_workflow_called=True,
            test_after_update_done=False,
        )

        assert enforcement_decision(ctx) is None

    def test_explore_without_workflow_not_when_test_done(self) -> None:
        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=False,
            test_after_update_done=True,
        )

        assert enforcement_decision(ctx) is None

    def test_explore_without_workflow_respects_cap(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=False,
            test_after_update_done=False,
            explore_without_workflow_nudge_count=MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES,
        )

        assert enforcement_decision(ctx) is None

    def test_explore_without_workflow_not_without_observation(self) -> None:
        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            update_workflow_called=False,
            test_after_update_done=False,
        )
        nudge = enforcement_decision(ctx)
        assert nudge.rule == "post_navigate"
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

    def test_post_hook_failure_rolls_back_coverage_credit(self) -> None:
        assert "scouted_output_covered_paths" in _POST_HOOK_CONTEXT_ROLLBACK_FIELDS
        assert "synthesized_business_required_parameter_keys" in _POST_HOOK_CONTEXT_ROLLBACK_FIELDS
        ctx = _Ctx()
        ctx.scouted_output_covered_paths = {"output.document_name"}
        ctx.synthesized_business_required_parameter_keys = {"service_address"}
        snapshot = _snapshot_post_hook_context(ctx)
        ctx.scouted_output_covered_paths.add("output.leaked")
        ctx.synthesized_business_required_parameter_keys.add("leaked_input")
        _restore_post_hook_context(ctx, snapshot)
        assert ctx.scouted_output_covered_paths == {"output.document_name"}
        assert ctx.synthesized_business_required_parameter_keys == {"service_address"}

    def test_post_hook_failure_rolls_back_scout_observation_contract(self) -> None:
        assert "scout_observation_contract" in _POST_HOOK_CONTEXT_ROLLBACK_FIELDS
        ctx = _Ctx()
        ctx.scout_observation_contract = None
        snapshot = _snapshot_post_hook_context(ctx)
        ctx.scout_observation_contract = object()
        _restore_post_hook_context(ctx, snapshot)
        assert ctx.scout_observation_contract is None

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

    def test_all_generic_token_path_still_requires_producer_plan(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.data", "the data is captured"))
        assert uncovered_requested_output_paths(ctx) == set()
        ctx.synthesized_block_offered_goal_complete = synthesized_trajectory_is_goal_complete(ctx)
        assert synthesized_trajectory_is_goal_complete(ctx) is False

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

    def test_uncovered_output_keeps_gate_open_and_admits_scout_tools(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        assert uncovered_requested_output_paths(ctx) == {"output.document_name"}
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_uncovered_output_leaves_the_trajectory_goal_reaching(self) -> None:
        ctx = self._authoring_ctx(_criterion("output.document_name", "the order status document name is captured"))
        assert uncovered_requested_output_paths(ctx) == {"output.document_name"}
        assert synthesized_trajectory_reaches_goal(ctx) is True
        assert synthesized_trajectory_is_goal_complete(ctx) is False

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
    # These helpers fill the password screen, but CodeQL's sensitive-data
    # heuristic is name-based: a "password"-named symbol taints every value it
    # produces, and py/weak-sensitive-data-hashing then reports the unrelated
    # evidence fingerprint downstream as password hashing.
    _LOGIN_URL = "https://portal.example.test/login"
    _SECOND_SCREEN_URL = "https://portal.example.test/password"

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
    def _second_screen_fill(source_url: str = "https://portal.example.test/password") -> dict[str, object]:
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
        return [
            *self._two_screen_first_page(),
            self._second_screen_fill(),
            self._submit(self._SECOND_SCREEN_URL, "Sign in"),
        ]

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
            [self._second_screen_fill(), self._submit(self._SECOND_SCREEN_URL, "Sign in")],
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
                "source_url": self._SECOND_SCREEN_URL,
            },
            self._submit(self._SECOND_SCREEN_URL, "Verify"),
        ]
        ctx = self._ctx_with_inventory(trajectory, inventory={"cred_1": frozenset({"username", "password"})})
        assert synthesized_trajectory_is_goal_complete(ctx) is True

    def test_filled_password_without_post_fill_submit_is_incomplete(self) -> None:
        ctx = self._ctx_with_inventory(
            [self._username_fill(), self._submit(self._LOGIN_URL, "Continue"), self._second_screen_fill()],
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is False

    def test_mixed_credentials_incomplete_until_both_flows_finish(self) -> None:
        second_fill = {
            "tool_name": "fill_credential_field",
            "credential_id": "cred_2",
            "credential_field": "username",
            "selector": "#user2",
            "source_url": self._SECOND_SCREEN_URL,
        }
        trajectory = [*self._two_screen_full_login(), second_fill, self._submit(self._SECOND_SCREEN_URL, "Next")]
        ctx = self._ctx_with_inventory(
            trajectory,
            inventory={
                "cred_1": frozenset({"username", "password"}),
                "cred_2": frozenset({"username", "password"}),
            },
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is False

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

    def test_full_login_with_post_fill_submit_is_complete(self) -> None:
        ctx = self._ctx_with_inventory(
            self._two_screen_full_login(),
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is True

    def test_half_login_with_unobserved_second_screen_is_incomplete(self) -> None:
        ctx = self._ctx_with_inventory(
            self._two_screen_first_page(),
            inventory={"cred_1": frozenset({"username", "password"})},
        )
        assert synthesized_trajectory_is_goal_complete(ctx) is False

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
        ctx.synthesized_block_offered = False
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.enforcement.synthesize_code_block",
            lambda *args, **kwargs: SynthesizedCodeBlock(code="await page.locator('#login').click()"),
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


def _deadline_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        copilot_total_timeout_exceeded=False,
        copilot_credential_pause_seconds=0.0,
        build_phase=BuildPhase.TESTING,
    )


def _deadline_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry.get("event") == "copilot_turn_deadline_expired"]


def test_deadline_fingerprint_carries_elapsed_iteration_and_phase() -> None:
    ctx = _deadline_ctx()

    with capture_logs() as logs:
        _mark_copilot_total_timeout(ctx, elapsed_seconds=901.4567, iteration=12)

    events = _deadline_events(logs)
    assert len(events) == 1
    assert events[0]["elapsed_seconds"] == 901.457
    assert events[0]["iteration"] == 12
    assert events[0]["build_phase"] == "testing"
    assert ctx.copilot_total_timeout_exceeded is True


def test_deadline_fingerprint_emitted_once_per_turn_across_both_reachable_sites() -> None:
    ctx = _deadline_ctx()

    with capture_logs() as logs:
        _mark_copilot_total_timeout(ctx, elapsed_seconds=901.0, iteration=3)
        _mark_copilot_total_timeout(ctx, elapsed_seconds=902.0, iteration=4)

    assert len(_deadline_events(logs)) == 1
    assert ctx.copilot_total_timeout_exceeded is True


def test_deadline_flag_still_written_when_fingerprint_is_suppressed() -> None:
    ctx = _deadline_ctx()
    ctx.copilot_total_timeout_exceeded = True

    with capture_logs() as logs:
        _mark_copilot_total_timeout(ctx, elapsed_seconds=901.0, iteration=1)

    assert _deadline_events(logs) == []
    assert ctx.copilot_total_timeout_exceeded is True


@pytest.mark.parametrize("iteration", [0, 5, 11])
def test_cancel_site_helper_threads_iteration_into_the_fingerprint(iteration: int) -> None:
    ctx = _deadline_ctx()
    start_time = time.monotonic() - (TOTAL_TIMEOUT_SECONDS + 5.0)

    with capture_logs() as logs:
        _mark_copilot_total_timeout_if_elapsed(ctx, start_time, iteration)

    events = _deadline_events(logs)
    assert len(events) == 1
    assert events[0]["iteration"] == iteration
    assert events[0]["elapsed_seconds"] >= TOTAL_TIMEOUT_SECONDS


def test_cancel_site_helper_is_silent_before_the_deadline() -> None:
    ctx = _deadline_ctx()

    with capture_logs() as logs:
        _mark_copilot_total_timeout_if_elapsed(ctx, time.monotonic(), 2)

    assert _deadline_events(logs) == []
    assert ctx.copilot_total_timeout_exceeded is False


def test_total_timeout_override_binds_on_settings_and_defaults_unset() -> None:
    unset = Settings(_env_file=None, WORKFLOW_COPILOT_TOTAL_TIMEOUT_SECONDS=None)
    overridden = Settings(_env_file=None, WORKFLOW_COPILOT_TOTAL_TIMEOUT_SECONDS=300)

    assert unset.WORKFLOW_COPILOT_TOTAL_TIMEOUT_SECONDS is None
    assert overridden.WORKFLOW_COPILOT_TOTAL_TIMEOUT_SECONDS == 300


def test_shared_tools_bind_the_configured_total_timeout() -> None:
    assert shared_total_timeout_seconds == TOTAL_TIMEOUT_SECONDS
    assert TOTAL_TIMEOUT_SECONDS == (settings.WORKFLOW_COPILOT_TOTAL_TIMEOUT_SECONDS or 900)


class TestUnboundOfferReopen:
    """The offer names relations no read has claimed, so it stops once one of them is read."""

    @staticmethod
    def _ctx_on_a_page_with_relations() -> _Ctx:
        ctx = _Ctx()
        ctx.scout_trajectory = []
        ctx.request_policy = RequestPolicy(
            completion_criteria=[
                CompletionCriterion(id="c0", outcome="the number of visitors", output_path="output.visitors")
            ]
        )
        ctx.flow_evidence = [
            {
                "step": 1,
                "reached_via": "current_page",
                "had_bounded_schema": True,
                "evidence": {
                    "source_tool": "inspect_page_for_composition",
                    "inspection_warnings": [],
                    "result_containers": [],
                    "result_containers_truncated": False,
                    "key_value_relations_truncated": False,
                    "key_value_relations": [
                        {
                            "key_text": "Visitors",
                            "value_text": "7.82K",
                            "container_selector": ".card",
                            "container_match_count": 1,
                            "container_position": 0,
                            "value_child_index": 1,
                            "direct_child_count": 3,
                            "visible": True,
                            "value_visible": True,
                        }
                    ],
                },
            }
        ]
        return ctx

    def test_an_unread_requested_output_reopens_the_offer(self) -> None:
        ctx = self._ctx_on_a_page_with_relations()

        assert synthesized_offer_reopened_for_extraction_plan(ctx, None) is True

    def test_a_read_that_answered_the_path_stops_reopening_it(self) -> None:
        ctx = self._ctx_on_a_page_with_relations()
        ctx.scout_trajectory = [
            {
                "tool_name": "read_value",
                "read_expression": "document.querySelector('.card .val').innerText",
                "read_output_path": "output.visitors",
                "read_output_path_source": "declared",
                "read_result_value": "7.82K",
                "read_result_shape": "str",
            }
        ]

        # Without the latch this stays True for the rest of the turn and the whole offer is
        # re-emitted on every prompt build.
        assert synthesized_offer_reopened_for_extraction_plan(ctx, None) is False


class TestCollapseSupersededSynthesizedOffers:
    """Refreshed synthesized-block offers supersede rather than stack: the
    collapse keeps at most the last offer and drops the superseded ones."""

    @staticmethod
    def _offer(body: str) -> dict[str, Any]:
        from skyvern.forge.sdk.copilot.code_block_synthesis import SYNTHESIZED_OFFER_SENTINEL

        return {"role": "user", "content": SYNTHESIZED_OFFER_SENTINEL + " " + body}

    def test_keeps_only_the_last_offer_with_distinct_payloads(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import collapse_superseded_synthesized_offers

        goal = {"role": "user", "content": "build me a login workflow"}
        # Distinct, non-monotonic payloads: a refresh can fire because the plan
        # changed or the trajectory was evicted, so the newest body is not a
        # superset of the older ones. Keep-last must not assume it is.
        items = [
            goal,
            self._offer("await page.goto('https://a.example')"),
            {"type": "function_call", "call_id": "c1", "name": "evaluate", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": '{"ok": true}'},
            self._offer("await page.click('#submit')"),
            {"role": "assistant", "content": "scouting"},
            self._offer("await page.goto('https://b.example')"),
        ]
        result = collapse_superseded_synthesized_offers(items)

        offers = [it for it in result if isinstance(it, dict) and "SYNTHESIZED CODE BLOCK" in str(it.get("content"))]
        assert len(offers) == 1
        assert "b.example" in offers[0]["content"]
        # Everything that is not a superseded offer survives untouched, in order.
        assert result[0] == goal
        assert [it.get("call_id") for it in result if isinstance(it, dict) and it.get("call_id")] == ["c1", "c1"]

    def test_idempotent_and_noop_without_stacking(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import collapse_superseded_synthesized_offers

        items = [
            {"role": "user", "content": "build me a workflow"},
            {"type": "function_call_output", "call_id": "c1", "output": "{}"},
            self._offer("await page.goto('https://a.example')"),
        ]
        once = collapse_superseded_synthesized_offers(items)
        assert once == items
        assert collapse_superseded_synthesized_offers(once) == once

    def test_never_drops_the_opening_item(self) -> None:
        """A first transcript item that happens to start with the sentinel is the
        user's goal, not a superseded offer; collapsing it would delete the turn's
        anchor. It also must not count as the kept offer."""
        from skyvern.forge.sdk.copilot.enforcement import collapse_superseded_synthesized_offers

        opening = self._offer("verbatim text a user pasted")
        items = [opening, self._offer("stale"), self._offer("fresh")]
        result = collapse_superseded_synthesized_offers(items)
        assert result[0] == opening
        assert [it["content"] for it in result[1:]] == [items[2]["content"]]

    def test_ignores_non_offer_user_messages_and_list_content(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import collapse_superseded_synthesized_offers

        items = [
            {"role": "user", "content": "build me a workflow"},
            {"role": "user", "content": "SYNTHESIZED but not the sentinel"},
            {"role": "user", "content": [{"type": "input_text", "text": "screenshot-ish"}]},
            self._offer("stale"),
            self._offer("fresh"),
            {"role": "user", "content": "one more real message"},
        ]
        result = collapse_superseded_synthesized_offers(items)
        assert [it.get("content") for it in result] == [
            "build me a workflow",
            "SYNTHESIZED but not the sentinel",
            [{"type": "input_text", "text": "screenshot-ish"}],
            items[4]["content"],
            "one more real message",
        ]

    def test_prune_input_list_collapses_offers(self) -> None:
        items = [
            {"role": "user", "content": "build me a workflow"},
            self._offer("stale one"),
            {"type": "function_call_output", "call_id": "c1", "output": "{}"},
            self._offer("stale two"),
            self._offer("fresh"),
        ]
        result = _prune_input_list(items)
        offers = [it for it in result if isinstance(it, dict) and "SYNTHESIZED CODE BLOCK" in str(it.get("content"))]
        assert len(offers) == 1
        assert "fresh" in offers[0]["content"]

    def test_offer_is_a_synthetic_user_message(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import is_synthetic_user_message

        assert is_synthetic_user_message(self._offer("await page.goto('https://a.example')"))
        assert not is_synthetic_user_message({"role": "user", "content": "build me a workflow"})
