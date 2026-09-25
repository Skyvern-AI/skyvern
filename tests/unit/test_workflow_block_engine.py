"""Tests for the WORKFLOW_TASK_V3_AB run-level engine A/B: run eligibility, arm resolution
(idempotency, kill switch, fail-closed), and the invariant that the persisted engine on
workflow_run_blocks and the dispatched engine come from the same resolution.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.experimentation.billing_tier import BILLING_TIER_PROPERTY, BillingTier
from skyvern.forge.sdk.experimentation.providers import BaseExperimentationProvider, NoOpExperimentationProvider
from skyvern.forge.sdk.experimentation.workflow_block_engine import (
    DISABLE_TASK_V3_FLAG,
    TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG,
    WORKFLOW_TASK_V3_AB_FLAG,
    NewWorkflowDefaultRollout,
    WorkflowBlockEngineRouteReason,
    _as_utc,
    resolve_workflow_block_engine_arm,
    workflow_block_engine_override,
)
from skyvern.forge.sdk.workflow.models.block import (
    ActionBlock,
    BaseTaskBlock,
    Block,
    BranchCondition,
    BranchEvaluationContext,
    CodeBlock,
    ConditionalBlock,
    ExtractionBlock,
    FileDownloadBlock,
    ForLoopBlock,
    HumanInteractionBlock,
    JinjaBranchCriteria,
    NavigationBlock,
    PromptBranchCriteria,
    TaskBlock,
    UrlBlock,
    V3AbIneligibleReason,
    ValidationBlock,
    WhileLoopBlock,
    _evaluate_prompt_branch_conditions_batch,
    get_all_blocks,
    run_is_eligible_for_v3_ab,
    v3_ab_ineligibility_reason,
)
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.forge.taskv3.goal_composition import render_block_context
from skyvern.schemas.run_enums import RunEngine
from skyvern.schemas.workflows import BlockResult, BlockType, WorkflowStatus
from skyvern.services import script_service
from tests.unit._workflow_block_engine_fakes import (
    WORKFLOW_BLOCK_ENGINE_APP_TARGET,
    FakeExperimentationProvider,
    consulted_flags,
    resolve_arm,
)
from tests.unit.helpers import make_organization, make_task
from tests.unit.test_agent_task_v3 import (
    _make_block,
    _make_output_parameter,
    _run_execute_step_gate,
    stub_workflow_block_engine_app,
)
from tests.unit.test_block_description_caching import _block_result, _setup_mocks
from tests.unit.test_missing_starter_url import _mock_block_execute_deps


@pytest.fixture
def scoped_context() -> Iterator[SkyvernContext]:
    context = SkyvernContext()
    skyvern_context.set(context)
    try:
        yield context
    finally:
        skyvern_context.reset()


@pytest.fixture(autouse=True)
def _no_ambient_v3_default_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    # The rollout this file covers sets TASK_V3_DEFAULT_ENGINE_WORKFLOW_CUTOFF in the environment, and
    # Settings reads .env at import: without pinning it, every test here answers to whatever the
    # machine happens to export.
    monkeypatch.setattr(settings, "TASK_V3_DEFAULT_ENGINE_WORKFLOW_CUTOFF", None)


@pytest.fixture
def v3_default_cutoff(monkeypatch: pytest.MonkeyPatch, _no_ambient_v3_default_cutoff: None) -> datetime:
    # Ordered after the autouse pin above by requesting it, so the off-by-default pin cannot clobber
    # the cutoff a test asked for.
    cutoff = datetime(2026, 9, 15, tzinfo=UTC)
    monkeypatch.setattr(settings, "TASK_V3_DEFAULT_ENGINE_WORKFLOW_CUTOFF", cutoff)
    return cutoff


def test_mixed_eligibility_run_download_block_is_eligible(scoped_context: SkyvernContext) -> None:
    eligible_1 = _make_block(TaskBlock, label="t1")
    eligible_2 = _make_block(NavigationBlock, label="t2", navigation_goal="Apply to the job")
    download_block = _make_block(ActionBlock, label="dl", complete_on_download=True)
    blocks: list[BaseTaskBlock] = [eligible_1, eligible_2, download_block]

    assert run_is_eligible_for_v3_ab(blocks, is_script_run=False) is True


def test_mixed_eligibility_run_file_download_block_is_eligible(scoped_context: SkyvernContext) -> None:
    eligible_1 = _make_block(TaskBlock, label="t1")
    eligible_2 = _make_block(NavigationBlock, label="t2", navigation_goal="Apply to the job")
    file_download_block = _make_block(FileDownloadBlock, label="fd")
    blocks: list[BaseTaskBlock] = [eligible_1, eligible_2, file_download_block]

    assert run_is_eligible_for_v3_ab(blocks, is_script_run=False) is True


def test_mixed_eligibility_run_download_gated_validation_block_is_not_eligible(
    scoped_context: SkyvernContext,
) -> None:
    # A validation block never acts on the page, so it can't trigger the download it would
    # complete on (SKY-14905); it must disqualify the whole run from the v3 A/B.
    eligible_1 = _make_block(TaskBlock, label="t1")
    eligible_2 = _make_block(NavigationBlock, label="t2", navigation_goal="Apply to the job")
    download_gated_validation = _make_block(ValidationBlock, label="dlv", complete_on_download=True)
    blocks: list[BaseTaskBlock] = [eligible_1, eligible_2, download_gated_validation]

    assert run_is_eligible_for_v3_ab(blocks, is_script_run=False) is False


@pytest.mark.asyncio
async def test_mixed_eligibility_run_pins_whole_run_to_control(scoped_context: SkyvernContext) -> None:
    eligible_1 = _make_block(TaskBlock, label="t1")
    eligible_2 = _make_block(NavigationBlock, label="t2", navigation_goal="Apply to the job")
    ineligible_block = _make_block(TaskBlock, label="unsupported")
    ineligible_block.block_type = BlockType.WAIT
    blocks: list[BaseTaskBlock] = [eligible_1, eligible_2, ineligible_block]

    assert run_is_eligible_for_v3_ab(blocks, is_script_run=False) is False

    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id="wr_mixed",
        ineligibility_reason=V3AbIneligibleReason.unsupported_block,
    )

    for block in blocks:
        assert block.resolve_engine("wr_mixed") == RunEngine.skyvern_v1
    # An ineligible run never even asks the provider -- there is nothing to bucket.
    assert provider.calls == []


@pytest.mark.asyncio
async def test_explicit_block_engine_is_never_overridden_by_treatment_arm(scoped_context: SkyvernContext) -> None:
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await resolve_arm(scoped_context, provider, workflow_run_id="wr_pinned", ineligibility_reason=None)
    assert scoped_context.workflow_block_engine_override == RunEngine.skyvern_v3

    cua_block = _make_block(TaskBlock, label="cua", engine=RunEngine.openai_cua)
    v2_block = _make_block(TaskBlock, label="v2", engine=RunEngine.skyvern_v2)

    assert cua_block.resolve_engine("wr_pinned") == RunEngine.openai_cua
    assert v2_block.resolve_engine("wr_pinned") == RunEngine.skyvern_v2


@pytest.mark.parametrize("pinned_engine", [RunEngine.openai_cua, RunEngine.skyvern_v3])
def test_pinned_non_default_engine_block_disqualifies_the_run(pinned_engine: RunEngine) -> None:
    eligible = _make_block(TaskBlock, label="ok")
    # Pinned as-authored in both arms, but that leaves control mixed-engine, so it
    # disqualifies the whole run rather than being skipped. A v3 pin is the user opting
    # in explicitly, not a treatment exposure.
    pinned = _make_block(NavigationBlock, label="pinned", navigation_goal="Apply to the job", engine=pinned_engine)

    assert run_is_eligible_for_v3_ab([eligible, pinned], is_script_run=False) is False


@pytest.mark.asyncio
async def test_all_eligible_run_resolves_every_block_to_treatment(scoped_context: SkyvernContext) -> None:
    blocks: list[BaseTaskBlock] = [
        _make_block(TaskBlock, label="t1"),
        _make_block(NavigationBlock, label="t2", navigation_goal="Apply to the job"),
        _make_block(ActionBlock, label="t3"),
    ]
    assert run_is_eligible_for_v3_ab(blocks, is_script_run=False) is True

    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await resolve_arm(scoped_context, provider, workflow_run_id="wr_treatment", ineligibility_reason=None)

    for block in blocks:
        assert block.resolve_engine("wr_treatment") == RunEngine.skyvern_v3


@pytest.mark.asyncio
async def test_arm_resolved_once_per_run_survives_mid_run_flag_flip(scoped_context: SkyvernContext) -> None:
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await resolve_arm(scoped_context, provider, workflow_run_id="wr_once", ineligibility_reason=None)
    assert scoped_context.workflow_block_engine_override == RunEngine.skyvern_v3

    # Invalidate the provider's own 300s cache and flip the flag, so a second query would
    # return False if it actually reached the provider. The idempotency guard must still
    # short-circuit on context.workflow_block_engine_resolved_run_id before that happens.
    provider.invalidate_resolution_caches()
    provider.flags[WORKFLOW_TASK_V3_AB_FLAG] = False
    await resolve_arm(scoped_context, provider, workflow_run_id="wr_once", ineligibility_reason=None)

    assert scoped_context.workflow_block_engine_override == RunEngine.skyvern_v3


@pytest.mark.asyncio
async def test_different_run_id_on_same_context_reresolves_instead_of_inheriting(
    scoped_context: SkyvernContext,
) -> None:
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await resolve_arm(scoped_context, provider, workflow_run_id="wr_A", ineligibility_reason=None)
    assert workflow_block_engine_override("wr_A") == RunEngine.skyvern_v3

    provider.flags[WORKFLOW_TASK_V3_AB_FLAG] = False
    await resolve_arm(scoped_context, provider, workflow_run_id="wr_B", ineligibility_reason=None)

    assert workflow_block_engine_override("wr_B") is None
    # The pin moved to B: A must not read as still-treatment via a stale resolution.
    assert workflow_block_engine_override("wr_A") is None


@pytest.mark.asyncio
async def test_unresolved_run_id_reads_control_without_resolving(scoped_context: SkyvernContext) -> None:
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await resolve_arm(scoped_context, provider, workflow_run_id="wr_A", ineligibility_reason=None)
    assert workflow_block_engine_override("wr_A") == RunEngine.skyvern_v3

    # wr_B was never resolved (task_v2 / cached-script helper paths never call the resolver for
    # their run), so the reader must not fall back to A's pin or to a bare "is anything pinned".
    assert workflow_block_engine_override("wr_B") is None
    assert all(distinct_id == "wr_A" for _, distinct_id, _ in provider.calls)


@pytest.mark.asyncio
async def test_resolver_matches_execute_step_flag_contract(scoped_context: SkyvernContext) -> None:
    """Derives the expected DISABLE_TASK_V3 call from the real execute_step gate instead of a
    hardcoded literal, so a drift in agent.py's distinct_id or properties reds this test.
    """
    gate_provider = FakeExperimentationProvider()
    await _run_execute_step_gate(
        engine=RunEngine.skyvern_v3,
        task_block=_make_block(TaskBlock, label="contract"),
        experimentation_provider=gate_provider,
        workflow_run_id="wr_contract",
    )
    gate_disable_calls = [call for call in gate_provider.calls if call[0] == DISABLE_TASK_V3_FLAG]
    assert len(gate_disable_calls) == 1
    gate_call = gate_disable_calls[0]
    assert gate_call[1] == "wr_contract"
    # Pinned separately from the equality below: both callers now build this dict in one shared
    # place, so dropping it would keep them agreeing with each other while silently losing the
    # organization targeting the flag's release conditions are written against.
    assert gate_call[2] == {"organization_id": make_organization(datetime.now(UTC)).organization_id}

    resolver_provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await resolve_arm(
        scoped_context,
        resolver_provider,
        workflow_run_id="wr_contract",
        ineligibility_reason=None,
        organization_id=(gate_call[2] or {}).get("organization_id"),
        workflow_permanent_id="wpid_contract",
    )
    resolver_disable_calls = [call for call in resolver_provider.calls if call[0] == DISABLE_TASK_V3_FLAG]
    assert len(resolver_disable_calls) == 1
    assert resolver_disable_calls[0] == gate_call

    assert any(
        call[0] == WORKFLOW_TASK_V3_AB_FLAG and (call[2] or {}).get("workflow_permanent_id") == "wpid_contract"
        for call in resolver_provider.calls
    )


@pytest.mark.asyncio
async def test_disable_flag_wins_over_ab_flag(scoped_context: SkyvernContext) -> None:
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True, DISABLE_TASK_V3_FLAG: True})
    await resolve_arm(scoped_context, provider, workflow_run_id="wr_disabled", ineligibility_reason=None)

    assert scoped_context.workflow_block_engine_override is None
    block = _make_block(TaskBlock, label="disabled_block")
    assert block.resolve_engine("wr_disabled") == RunEngine.skyvern_v1


@pytest.mark.asyncio
async def test_provider_exception_fails_closed_to_control(scoped_context: SkyvernContext) -> None:
    provider = FakeExperimentationProvider(raise_error=True)
    resolution = await resolve_arm(scoped_context, provider, workflow_run_id="wr_err", ineligibility_reason=None)

    assert scoped_context.workflow_block_engine_override is None
    block = _make_block(TaskBlock, label="err_block")
    assert block.resolve_engine("wr_err") == RunEngine.skyvern_v1
    # An unrandomized control run has to be separable from a bucketed one in the same read.
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.flag_error


@pytest.mark.asyncio
async def test_a_failing_ab_evaluation_is_labelled_flag_error_not_bucketed_control(
    scoped_context: SkyvernContext,
) -> None:
    # A failed A/B evaluation and a real control bucket produce the same arm, so only route_reason
    # separates them. Reading a provider outage as flag_bucket_control mixes runs that were never
    # randomized into the control cell of every per-arm comparison, which is the one thing this field
    # exists to prevent. Only reachable through the strict resolver: the boolean one swallows the
    # error into None and bool()s it to a plain False.
    provider = FakeExperimentationProvider(strict_error_flags={WORKFLOW_TASK_V3_AB_FLAG})
    resolution = await resolve_arm(scoped_context, provider, workflow_run_id="wr_ab_down", ineligibility_reason=None)

    assert scoped_context.workflow_block_engine_override is None
    assert _make_block(TaskBlock, label="ab_down").resolve_engine("wr_ab_down") == RunEngine.skyvern_v1
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.flag_error


@pytest.mark.asyncio
async def test_an_ab_flag_local_evaluation_cannot_answer_is_control_but_not_a_bucket(
    scoped_context: SkyvernContext,
) -> None:
    # The third A/B answer: None from local evaluation is neither a failure nor a bucket. A key the
    # snapshot has no row for, or a condition local evaluation cannot answer, leaves the run on
    # control without it ever having been randomized -- and the runbook builds this rule's control
    # cell out of flag_bucket_control, so labelling it that way pours never-randomized runs into
    # every per-arm comparison, the same defect flag_error exists to prevent. The arm is unchanged.
    provider = FakeExperimentationProvider(
        {WORKFLOW_TASK_V3_AB_FLAG: True}, unresolvable_flags={WORKFLOW_TASK_V3_AB_FLAG}
    )
    resolution = await resolve_arm(
        scoped_context, provider, workflow_run_id="wr_ab_undefined", ineligibility_reason=None
    )

    assert scoped_context.workflow_block_engine_override is None
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.flag_undefined


def test_script_run_is_never_eligible() -> None:
    blocks: list[BaseTaskBlock] = [
        _make_block(TaskBlock, label="t1"),
        _make_block(NavigationBlock, label="t2", navigation_goal="Apply to the job"),
    ]
    assert run_is_eligible_for_v3_ab(blocks, is_script_run=True) is False


@pytest.mark.asyncio
async def test_noop_provider_never_queried_and_leaves_engine_unchanged(scoped_context: SkyvernContext) -> None:
    provider = NoOpExperimentationProvider()
    spy = AsyncMock(wraps=provider._is_feature_enabled)
    with patch(WORKFLOW_BLOCK_ENGINE_APP_TARGET) as mock_app, patch.object(provider, "_is_feature_enabled", spy):
        mock_app.EXPERIMENTATION_PROVIDER = provider
        stub_workflow_block_engine_app(mock_app)
        await resolve_workflow_block_engine_arm(
            scoped_context,
            workflow_run_id="wr_noop",
            organization_id="org_1",
            workflow_permanent_id="wpid_1",
            workflow_status=WorkflowStatus.published,
            trigger_type=WorkflowRunTriggerType.api,
            ineligibility_reason=None,
        )

    spy.assert_not_called()
    assert scoped_context.workflow_block_engine_override is None
    block = _make_block(TaskBlock, label="noop_block")
    assert block.resolve_engine("wr_noop") == RunEngine.skyvern_v1


def test_non_task_blocks_ignored_but_nested_loop_task_blocks_considered() -> None:
    code_block = CodeBlock(label="code", output_parameter=_make_output_parameter("code"), code="pass")
    eligible = _make_block(TaskBlock, label="ok")

    flat_without_loop = get_all_blocks([code_block, eligible])
    assert run_is_eligible_for_v3_ab(flat_without_loop, is_script_run=False) is True

    ineligible_nested = _make_block(TaskBlock, label="nested_pinned", engine=RunEngine.openai_cua)
    loop = ForLoopBlock(
        label="loop",
        output_parameter=_make_output_parameter("loop"),
        loop_blocks=[ineligible_nested],
    )

    flat_with_loop = get_all_blocks([code_block, eligible, loop])
    assert ineligible_nested in flat_with_loop
    assert run_is_eligible_for_v3_ab(flat_with_loop, is_script_run=False) is False


def test_inert_blocks_do_not_disqualify_an_otherwise_eligible_run() -> None:
    # Nearly every workflow starts with a Go-to-URL block; treating it (or a trailing
    # HumanInteractionBlock) as a disqualifier would kill nearly all experiment traffic.
    url_block = _make_block(UrlBlock, label="goto", url="https://example.com")
    human_block = _make_block(HumanInteractionBlock, label="human")
    eligible = _make_block(TaskBlock, label="ok")

    assert run_is_eligible_for_v3_ab([url_block, human_block, eligible], is_script_run=False) is True


def test_run_with_only_inert_or_non_task_blocks_is_not_eligible() -> None:
    code_block = CodeBlock(label="code", output_parameter=_make_output_parameter("code"), code="pass")
    url_block = _make_block(UrlBlock, label="goto", url="https://example.com")
    human_block = _make_block(HumanInteractionBlock, label="human")

    assert run_is_eligible_for_v3_ab([code_block, url_block, human_block], is_script_run=False) is False


@pytest.mark.asyncio
async def test_inert_blocks_resolve_to_v1_in_a_treated_run(scoped_context: SkyvernContext) -> None:
    # Eligibility skips GOTO_URL/HumanInteraction as engine-inert, so resolve_engine must skip
    # them too -- otherwise their workflow_run_blocks rows claim an engine that never ran.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await resolve_arm(scoped_context, provider, workflow_run_id="wr_inert", ineligibility_reason=None)
    assert scoped_context.workflow_block_engine_override == RunEngine.skyvern_v3

    url_block = _make_block(UrlBlock, label="goto", url="https://example.com")
    human_block = _make_block(HumanInteractionBlock, label="human")

    assert url_block.resolve_engine("wr_inert") == RunEngine.skyvern_v1
    assert human_block.resolve_engine("wr_inert") == RunEngine.skyvern_v1


@pytest.mark.asyncio
async def test_exclude_from_engine_ab_block_is_never_rerouted(scoped_context: SkyvernContext) -> None:
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await resolve_arm(scoped_context, provider, workflow_run_id="wr_excluded", ineligibility_reason=None)
    assert scoped_context.workflow_block_engine_override == RunEngine.skyvern_v3

    excluded_block = _make_block(ActionBlock, label="excluded")
    excluded_block._exclude_from_engine_ab = True
    assert excluded_block.resolve_engine("wr_excluded") == RunEngine.skyvern_v1


@pytest.mark.asyncio
async def test_execute_safe_persists_the_resolved_engine(scoped_context: SkyvernContext) -> None:
    scoped_context.workflow_block_engine_resolved_run_id = "wr_persist"
    scoped_context.workflow_block_engine_override = RunEngine.skyvern_v3
    block = _make_block(TaskBlock, label="persist_block")

    with (
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, return_value=_block_result()),
        patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
    ):
        _setup_mocks(mock_app)

        await block.execute_safe(workflow_run_id="wr_persist")

    persisted_engine = mock_app.DATABASE.observer.create_workflow_run_block.await_args.kwargs["engine"]
    assert persisted_engine == RunEngine.skyvern_v3


class _EngineCaptured(BaseException):
    """Raised from the create_workflow_run_block mock once the dispatched engine is captured.

    Subclasses BaseException, not Exception, so it escapes the `except Exception` handlers in
    Block.execute_safe, WorkflowService._execute_block_via_agent_if_allowed, and
    WorkflowService._execute_single_block instead of being swallowed into a failed BlockResult.
    """


@pytest.mark.asyncio
async def test_base_task_block_execute_dispatches_with_the_resolved_engine(scoped_context: SkyvernContext) -> None:
    """Drives the real BaseTaskBlock.execute (not a mock of it) up to the app.agent.execute_step
    call. A revert of engine=self.resolve_engine(...) back to engine=self.engine only shows up
    here -- the persist-side test mocks BaseTaskBlock.execute out entirely.
    """
    scoped_context.workflow_block_engine_resolved_run_id = "wr_missing_starter_url_test"
    scoped_context.workflow_block_engine_override = RunEngine.skyvern_v3
    block = _make_block(TaskBlock, label="dispatch_block")

    captured: dict[str, Any] = {}

    async def _capture_engine_and_abort(**kwargs: Any) -> Any:
        captured["engine"] = kwargs.get("engine")
        raise _EngineCaptured()

    with _mock_block_execute_deps(working_page_url="https://example.com/dashboard") as deps:
        deps["agent"].execute_step = AsyncMock(side_effect=_capture_engine_and_abort)

        with pytest.raises(_EngineCaptured):
            await block.execute(
                workflow_run_id="wr_missing_starter_url_test",
                workflow_run_block_id="wrb_test",
                organization_id="o_test",
            )

    assert captured["engine"] == RunEngine.skyvern_v3


async def _persisted_engine_from_execute_workflow_blocks(
    monkeypatch: pytest.MonkeyPatch,
    provider: BaseExperimentationProvider,
    *,
    workflow_status: WorkflowStatus = WorkflowStatus.published,
    billing_tier: BillingTier = BillingTier.UNKNOWN,
    first_version_created_at: datetime | None = None,
    trigger_type: WorkflowRunTriggerType | None = WorkflowRunTriggerType.api,
) -> RunEngine:
    """Drives the real WorkflowService._execute_workflow_blocks -> _execute_single_block ->
    Block.execute_safe chain for a single eligible TaskBlock and returns the engine it persisted,
    forcing only the experimentation provider, the billing tier and the workflow's birth timestamp.
    Everything the arm decision depends on is therefore supplied by the real caller, not by the
    resolver's own test seam.
    """
    monkeypatch.setattr(app, "EXPERIMENTATION_PROVIDER", provider)
    monkeypatch.setattr(app.AGENT_FUNCTION, "resolve_billing_tier", AsyncMock(return_value=billing_tier))
    monkeypatch.setattr(
        app.DATABASE.workflows,
        "get_workflow_permanent_id_created_at",
        AsyncMock(return_value=first_version_created_at),
    )

    block = _make_block(TaskBlock, label="e2e_block")
    workflow = MagicMock()
    workflow.workflow_definition.blocks = [block]
    workflow.workflow_definition.version = 1
    workflow.workflow_definition.finally_block_label = None
    workflow.status = workflow_status

    workflow_run = MagicMock()
    workflow_run.workflow_run_id = "wr_e2e"
    workflow_run.workflow_permanent_id = "wpid_e2e"
    workflow_run.organization_id = "org_e2e"
    workflow_run.run_with = None
    workflow_run.retried_from_workflow_run_id = None
    workflow_run.trigger_type = trigger_type

    organization = MagicMock()
    organization.organization_id = "org_e2e"

    # update_workflow_run_if_not_final's return value feeds datetime arithmetic in
    # mark_workflow_run_as_running; it needs real datetimes, not MagicMock, to avoid an
    # unrelated crash before execution ever reaches the resolver.
    now = datetime.now(UTC)
    running_run = MagicMock(organization_id="org_e2e", started_at=now, created_at=now, finished_at=None)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=None))
    monkeypatch.setattr(
        app.DATABASE.workflow_runs, "update_workflow_run_if_not_final", AsyncMock(return_value=running_run)
    )
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "register_block_parameters_for_workflow_run", AsyncMock())

    captured: dict[str, Any] = {}

    async def _capture_engine_and_abort(**kwargs: Any) -> Any:
        captured["engine"] = kwargs.get("engine")
        raise _EngineCaptured()

    monkeypatch.setattr(
        app.DATABASE.observer, "create_workflow_run_block", AsyncMock(side_effect=_capture_engine_and_abort)
    )

    service = WorkflowService()
    monkeypatch.setattr(service, "should_run_script", AsyncMock(return_value=False))

    with pytest.raises(_EngineCaptured):
        await service._execute_workflow_blocks(
            workflow=workflow,
            workflow_run=workflow_run,
            organization=organization,
        )

    return captured["engine"]


@pytest.mark.asyncio
async def test_execute_workflow_blocks_pins_the_context_execute_safe_reads_from(
    scoped_context: SkyvernContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If resolve_workflow_block_engine_arm pinned the arm on a different SkyvernContext object
    than the one execute_safe's resolve_engine() reads back from, this fails: the persisted engine
    would be skyvern_v1 instead of skyvern_v3.
    """
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})

    engine = await _persisted_engine_from_execute_workflow_blocks(monkeypatch, provider)

    assert engine == RunEngine.skyvern_v3


@pytest.mark.parametrize(
    ("workflow_status", "expected_engine"),
    [
        (WorkflowStatus.auto_generated, RunEngine.skyvern_v1),
        (WorkflowStatus.published, RunEngine.skyvern_v3),
    ],
)
@pytest.mark.asyncio
async def test_execute_workflow_blocks_hands_the_resolver_the_running_versions_status(
    scoped_context: SkyvernContext,
    monkeypatch: pytest.MonkeyPatch,
    v3_default_cutoff: datetime,
    workflow_status: WorkflowStatus,
    expected_engine: RunEngine,
) -> None:
    # The per-call exclusion is only as real as the status the caller passes. Both cases are the same
    # new self-serve workflow with the percentage knob off, so the only thing deciding the engine is
    # workflow.status reaching the resolver: a caller that stopped passing it, or passed the
    # workflow_run instead, would take the login/download/credential-test/SDK endpoints' per-call
    # workflows onto v3 with it.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: False})

    engine = await _persisted_engine_from_execute_workflow_blocks(
        monkeypatch,
        provider,
        workflow_status=workflow_status,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=v3_default_cutoff.replace(tzinfo=None) + timedelta(days=1),
    )

    assert engine == expected_engine


@pytest.mark.parametrize(
    ("trigger_type", "expected_engine"),
    [
        (WorkflowRunTriggerType.job_recipe_apply, RunEngine.skyvern_v1),
        (WorkflowRunTriggerType.job_recipe_extract, RunEngine.skyvern_v1),
        (WorkflowRunTriggerType.api, RunEngine.skyvern_v3),
    ],
)
@pytest.mark.asyncio
async def test_execute_workflow_blocks_hands_the_resolver_the_runs_trigger_type(
    scoped_context: SkyvernContext,
    monkeypatch: pytest.MonkeyPatch,
    v3_default_cutoff: datetime,
    trigger_type: WorkflowRunTriggerType,
    expected_engine: RunEngine,
) -> None:
    # The job-recipe endpoints run a brand-new published workflow_permanent_id per request, so the
    # status the caller passes cannot exclude them and only the run's trigger kind can. All three
    # cases are the same new self-serve published workflow with the percentage knob off, so the only
    # thing deciding the engine is workflow_run.trigger_type reaching the resolver: a caller that
    # stopped passing it would force-route every recipe call to v3, unrandomized.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: False})

    engine = await _persisted_engine_from_execute_workflow_blocks(
        monkeypatch,
        provider,
        workflow_status=WorkflowStatus.published,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=v3_default_cutoff.replace(tzinfo=None) + timedelta(days=1),
        trigger_type=trigger_type,
    )

    assert engine == expected_engine


def _eligible_mix_blocks() -> list[BaseTaskBlock]:
    return [
        _make_block(TaskBlock, label="t1"),
        _make_block(NavigationBlock, label="t2", navigation_goal="Apply to the job"),
        _make_block(ActionBlock, label="t3"),
    ]


def _pinned_engine_blocks() -> list[BaseTaskBlock]:
    eligible = _make_block(TaskBlock, label="ok")
    pinned = _make_block(
        NavigationBlock, label="pinned", navigation_goal="Apply to the job", engine=RunEngine.skyvern_v2
    )
    return [eligible, pinned]


def _unsupported_block_type_blocks() -> list[BaseTaskBlock]:
    # No shipped BaseTaskBlock subclass declares an unsupported, non-inert block_type today, so
    # force one to exercise the branch _task_block_supports_v3 exists to guard.
    unsupported = _make_block(TaskBlock, label="unsupported")
    unsupported.block_type = BlockType.WAIT
    return [unsupported]


def _download_gated_validation_blocks() -> list[BaseTaskBlock]:
    return [_make_block(ValidationBlock, label="dlv", complete_on_download=True)]


def _totp_blocks() -> list[BaseTaskBlock]:
    # The multi-block shape the removed gate excluded: a verification-URL block alongside an ordinary
    # one. v3 carries the verification source per block, so neither block disqualifies the run.
    return [
        _make_block(TaskBlock, label="signin", totp_verification_url="https://example.com/otp"),
        _make_block(NavigationBlock, label="after_signin", navigation_goal="Apply to the job"),
    ]


def _no_reroutable_blocks() -> list[BaseTaskBlock]:
    code_block = CodeBlock(label="code", output_parameter=_make_output_parameter("code"), code="pass")
    url_block = _make_block(UrlBlock, label="goto", url="https://example.com")
    human_block = _make_block(HumanInteractionBlock, label="human")
    return [code_block, url_block, human_block]


def _conditional_block(criteria: PromptBranchCriteria | JinjaBranchCriteria) -> ConditionalBlock:
    return ConditionalBlock(
        label="cond",
        output_parameter=_make_output_parameter("cond"),
        branch_conditions=[
            BranchCondition(criteria=criteria, next_block_label="a"),
            BranchCondition(is_default=True, next_block_label="b"),
        ],
    )


def _prompt_branch_conditional_only_blocks() -> list[Block]:
    return [*_no_reroutable_blocks(), _conditional_block(PromptBranchCriteria(expression="the page shows an error"))]


def _jinja_only_conditional_blocks() -> list[Block]:
    # BranchCondition's validator re-types criteria from expression shape: only a PURE jinja
    # expression stays JinjaBranchCriteria; jinja mixed with bare text re-types to prompt.
    return [*_no_reroutable_blocks(), _conditional_block(JinjaBranchCriteria(expression="{{ x == 'y' }}"))]


def _while_loop(criteria: PromptBranchCriteria | JinjaBranchCriteria) -> WhileLoopBlock:
    body = CodeBlock(label="loop_body", output_parameter=_make_output_parameter("loop_body"), code="pass")
    return WhileLoopBlock(
        label="while",
        output_parameter=_make_output_parameter("while"),
        condition=criteria,
        loop_blocks=[body],
    )


def _prompt_condition_while_loop_only_blocks() -> list[Block]:
    return [*_no_reroutable_blocks(), _while_loop(PromptBranchCriteria(expression="the page shows an error"))]


def _jinja_condition_while_loop_blocks() -> list[Block]:
    return [*_no_reroutable_blocks(), _while_loop(JinjaBranchCriteria(expression="{{ x == 'y' }}"))]


@pytest.mark.parametrize(
    ("blocks_factory", "is_script_run", "expected_reason"),
    [
        (_eligible_mix_blocks, True, V3AbIneligibleReason.script_run),
        (_pinned_engine_blocks, False, V3AbIneligibleReason.pinned_engine),
        (_unsupported_block_type_blocks, False, V3AbIneligibleReason.unsupported_block),
        (_download_gated_validation_blocks, False, V3AbIneligibleReason.unsupported_block),
        (_totp_blocks, False, None),
        (_no_reroutable_blocks, False, V3AbIneligibleReason.no_reroutable_blocks),
        (_eligible_mix_blocks, False, None),
        (_prompt_branch_conditional_only_blocks, False, None),
        (_jinja_only_conditional_blocks, False, V3AbIneligibleReason.no_reroutable_blocks),
        (_prompt_condition_while_loop_only_blocks, False, None),
        (_jinja_condition_while_loop_blocks, False, V3AbIneligibleReason.no_reroutable_blocks),
    ],
    ids=[
        "script_run",
        "pinned_engine",
        "unsupported_block_type",
        "unsupported_block_validation_download",
        "totp_verification_url_is_admitted",
        "no_reroutable_blocks",
        "eligible",
        "prompt_branch_conditional_is_a_reroutable_surface",
        "jinja_only_conditional_stays_invisible",
        "prompt_condition_while_loop_is_a_reroutable_surface",
        "jinja_condition_while_loop_stays_invisible",
    ],
)
def test_v3_ab_ineligibility_reason_maps_each_disqualifier(
    blocks_factory: Callable[[], list[BaseTaskBlock]],
    is_script_run: bool,
    expected_reason: V3AbIneligibleReason | None,
) -> None:
    blocks = blocks_factory()
    reason = v3_ab_ineligibility_reason(blocks, is_script_run=is_script_run)
    assert reason == expected_reason
    # run_is_eligible_for_v3_ab must stay a thin wrapper over the same decision.
    assert run_is_eligible_for_v3_ab(blocks, is_script_run=is_script_run) is (expected_reason is None)


@pytest.mark.asyncio
async def test_resolver_logs_the_ineligibility_reason(scoped_context: SkyvernContext) -> None:
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    with (
        patch(WORKFLOW_BLOCK_ENGINE_APP_TARGET) as mock_app,
        patch("skyvern.forge.sdk.experimentation.workflow_block_engine.LOG") as mock_log,
    ):
        mock_app.EXPERIMENTATION_PROVIDER = provider
        stub_workflow_block_engine_app(mock_app)
        await resolve_workflow_block_engine_arm(
            scoped_context,
            workflow_run_id="wr_logged",
            organization_id="org_1",
            workflow_permanent_id="wpid_1",
            workflow_status=WorkflowStatus.published,
            trigger_type=WorkflowRunTriggerType.api,
            ineligibility_reason=V3AbIneligibleReason.pinned_engine,
        )

    assert scoped_context.workflow_block_engine_override is None
    resolved_call = mock_log.info.call_args
    assert resolved_call.args[0] == "Resolved workflow-block engine arm"
    assert resolved_call.kwargs["arm"] == "control"
    assert resolved_call.kwargs["ineligibility_reason"] == V3AbIneligibleReason.pinned_engine
    # The provider is never even queried for an ineligible run -- nothing to bucket.
    assert provider.calls == []


def _branch_eval_stub(captured_engines: list[RunEngine], workflow_run_id: str) -> Any:
    async def _capture_engine(self: ExtractionBlock, *args: Any, **kwargs: Any) -> BlockResult:
        # The label prefix is the telemetry handle that lets reads slice branch-eval outcomes
        # per arm (the spawned task takes it as its title); pinned here so it can't silently drift.
        assert self.label.startswith("prompt_branch_eval_")
        captured_engines.append(self.resolve_engine(workflow_run_id))
        return BlockResult(
            success=True,
            output_parameter=self.output_parameter,
            output_parameter_value={"evaluations": [{"condition_index": 1, "reasoning": "ok", "result": True}]},
            failure_reason=None,
        )

    return _capture_engine


@pytest.mark.asyncio
async def test_branch_eval_synthetic_block_honors_v3_override(scoped_context: SkyvernContext) -> None:
    """The prompt-branch batch evaluator's synthetic ExtractionBlock honors the run-level engine
    override like an authored block: a treated run evaluates branch conditions on its arm."""
    scoped_context.workflow_block_engine_resolved_run_id = "wr_branch_eval_v3"
    scoped_context.workflow_block_engine_override = RunEngine.skyvern_v3

    captured_engines: list[RunEngine] = []
    branch = BranchCondition(
        criteria=PromptBranchCriteria(expression="user selected premium plan"), next_block_label="x"
    )
    evaluation_context = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={})  # type: ignore[method-assign]

    with patch.object(ExtractionBlock, "execute", _branch_eval_stub(captured_engines, "wr_branch_eval_v3")):
        await _evaluate_prompt_branch_conditions_batch(
            log_label="cond",
            branches=[branch],
            evaluation_context=evaluation_context,
            workflow_run_id="wr_branch_eval_v3",
            workflow_run_block_id="wrb",
            organization_id="org_1",
            browser_session_id=None,
            workflow_id="wf_1",
        )

    assert captured_engines == [RunEngine.skyvern_v3]


@pytest.mark.asyncio
async def test_branch_eval_synthetic_block_gets_no_extraction_report_framing(scoped_context: SkyvernContext) -> None:
    """The branch evaluator's block follows the run's arm and has no navigation goal, so without its marker the
    TASK_V3_EXTRACTION_REPORTS framing ("absent fields are null, finish completed") would reach it. Its own prompt
    judges conditions from their text, and a null result parses as False: a silently wrong branch (SKY-16398)."""
    scoped_context.workflow_block_engine_resolved_run_id = "wr_branch_eval_framing"
    scoped_context.workflow_block_engine_override = RunEngine.skyvern_v3
    captured: list[ExtractionBlock] = []
    stub = _branch_eval_stub([], "wr_branch_eval_framing")

    async def _capture_block(self: ExtractionBlock, *args: Any, **kwargs: Any) -> BlockResult:
        captured.append(self)
        return await stub(self, *args, **kwargs)

    branch = BranchCondition(
        criteria=PromptBranchCriteria(expression="user selected premium plan"), next_block_label="x"
    )
    evaluation_context = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={})  # type: ignore[method-assign]

    with patch.object(ExtractionBlock, "execute", _capture_block):
        await _evaluate_prompt_branch_conditions_batch(
            log_label="cond",
            branches=[branch],
            evaluation_context=evaluation_context,
            workflow_run_id="wr_branch_eval_framing",
            workflow_run_block_id="wrb",
            organization_id="org_1",
            browser_session_id=None,
            workflow_id="wf_1",
        )

    (block,) = captured
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), navigation_goal=None, data_extraction_goal=block.data_extraction_goal)
    assert render_block_context(task, block, None, extraction_reports=True) == render_block_context(task, block, None)


@pytest.mark.asyncio
async def test_branch_eval_synthetic_block_defaults_to_v1_without_override(scoped_context: SkyvernContext) -> None:
    """With no run-level override pinned at all, the synthetic ExtractionBlock resolves to
    skyvern_v1 -- control runs stay on v1."""
    scoped_context.workflow_block_engine_resolved_run_id = "wr_branch_eval_control"
    scoped_context.workflow_block_engine_override = None

    captured_engines: list[RunEngine] = []
    branch = BranchCondition(
        criteria=PromptBranchCriteria(expression="user selected premium plan"), next_block_label="x"
    )
    evaluation_context = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={})  # type: ignore[method-assign]

    with patch.object(ExtractionBlock, "execute", _branch_eval_stub(captured_engines, "wr_branch_eval_control")):
        await _evaluate_prompt_branch_conditions_batch(
            log_label="cond",
            branches=[branch],
            evaluation_context=evaluation_context,
            workflow_run_id="wr_branch_eval_control",
            workflow_run_block_id="wrb",
            organization_id="org_1",
            browser_session_id=None,
            workflow_id="wf_1",
        )

    assert captured_engines == [RunEngine.skyvern_v1]


@pytest.mark.asyncio
async def test_script_create_site_stamps_the_script_run_marker(scoped_context: SkyvernContext) -> None:
    """A cached-script block never resolves an engine, so `engine IS NULL` alone cannot separate
    a script-executed block from an agent-executed one whose engine was never written. The script
    create-site stamps `script_run` at creation, making (engine NULL, script_run NULL) a genuine
    anomaly instead of an ordinary script run.
    """
    scoped_context.workflow_run_id = "wr_script_marker"
    scoped_context.organization_id = "o_test"

    with patch("skyvern.services.script_service.app") as mock_app:
        mock_app.DATABASE.observer.create_workflow_run_block = AsyncMock(
            return_value=SimpleNamespace(workflow_run_block_id="wrb_script_marker")
        )
        mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
        mock_app.DATABASE.tasks.create_task = AsyncMock(
            return_value=SimpleNamespace(task_id="tsk_script_marker", workflow_run_id="wr_script_marker")
        )
        mock_app.DATABASE.tasks.create_step = AsyncMock(return_value=SimpleNamespace(step_id="stp_script_marker"))
        mock_app.BROWSER_MANAGER.get_for_workflow_run.return_value = None
        mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value.cancel_failure_evidence_capture = (
            AsyncMock()
        )
        mock_app.AGENT_FUNCTION.is_recipe_step_attempt = AsyncMock(return_value=False)
        mock_app.DATABASE.tasks.update_step = AsyncMock(return_value=SimpleNamespace(step_id="stp_script_marker"))

        block_id, task_id, step_id = await script_service._create_workflow_block_run_and_task(
            block_type=BlockType.NAVIGATION,
            label="nav",
        )

    # Everything after the create call is wrapped in a try/except that collapses to
    # (None, None, None); asserting the ids proves the whole path ran rather than bailing early.
    assert (block_id, task_id, step_id) == ("wrb_script_marker", "tsk_script_marker", "stp_script_marker")
    create_kwargs = mock_app.DATABASE.observer.create_workflow_run_block.await_args.kwargs
    assert create_kwargs["ai_fallback_triggered"] is False
    assert create_kwargs.get("engine") is None


@pytest.mark.asyncio
async def test_agent_create_site_leaves_the_script_run_marker_unset(scoped_context: SkyvernContext) -> None:
    """The other half of the discriminator: an agent-executed block must NOT carry the marker,
    or `script_run IS NOT NULL` stops meaning "script-executed". A forward guard on the agent
    create-site, which this change deliberately leaves alone -- it passes with or without it."""
    scoped_context.workflow_block_engine_resolved_run_id = "wr_agent_marker"
    scoped_context.workflow_block_engine_override = RunEngine.skyvern_v3
    block = _make_block(TaskBlock, label="agent_marker_block")

    with (
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, return_value=_block_result()),
        patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
    ):
        _setup_mocks(mock_app)

        await block.execute_safe(workflow_run_id="wr_agent_marker")

    create_kwargs = mock_app.DATABASE.observer.create_workflow_run_block.await_args.kwargs
    assert create_kwargs["engine"] == RunEngine.skyvern_v3
    assert create_kwargs.get("ai_fallback_triggered") is None


@pytest.mark.asyncio
async def test_arm_offers_the_billing_tier_to_the_ab_flag_but_not_the_kill_switch(
    scoped_context: SkyvernContext,
) -> None:
    # The tier is what lets one release condition hold enterprise and self-serve at different
    # percentages. It must stay off DISABLE_TASK_V3: this resolver and the dispatch gate both
    # evaluate that flag through task_v3_disabled, and the provider caches on
    # (flag, distinct_id, properties), so adding a property on one side only would let the kill
    # switch answer differently for the same run.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    with patch(WORKFLOW_BLOCK_ENGINE_APP_TARGET) as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER = provider
        stub_workflow_block_engine_app(mock_app, billing_tier=BillingTier.SELF_SERVE)
        await resolve_workflow_block_engine_arm(
            scoped_context,
            workflow_run_id="wr_tier",
            organization_id="org_1",
            workflow_permanent_id="wpid_1",
            workflow_status=WorkflowStatus.published,
            trigger_type=WorkflowRunTriggerType.api,
            ineligibility_reason=None,
        )

    by_flag = {flag: properties or {} for flag, _distinct_id, properties in provider.calls}
    assert by_flag[WORKFLOW_TASK_V3_AB_FLAG][BILLING_TIER_PROPERTY] == "self_serve"
    assert BILLING_TIER_PROPERTY not in by_flag[DISABLE_TASK_V3_FLAG]
    assert scoped_context.workflow_block_engine_override == RunEngine.skyvern_v3


@pytest.mark.asyncio
async def test_a_run_that_never_reaches_the_ab_pays_for_no_tier_lookup(scoped_context: SkyvernContext) -> None:
    # An ineligible run reads no flag, so a tier read would be pure cost on a path that cannot use
    # it, and a logged tier would claim the run was bucketed on one. The same holds for a run the
    # kill switch stops: a Redis or pooler incident is exactly when that switch gets flipped, and
    # this resolver holds a lock while it runs.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    for reason, run_id in ((V3AbIneligibleReason.script_run, "wr_inelig"), (None, "wr_killed")):
        provider.flags[DISABLE_TASK_V3_FLAG] = reason is None
        tier = AsyncMock(return_value=BillingTier.ENTERPRISE)
        with (
            patch(WORKFLOW_BLOCK_ENGINE_APP_TARGET) as mock_app,
            patch("skyvern.forge.sdk.experimentation.workflow_block_engine.LOG") as mock_log,
        ):
            mock_app.EXPERIMENTATION_PROVIDER = provider
            mock_app.AGENT_FUNCTION.resolve_billing_tier = tier
            await resolve_workflow_block_engine_arm(
                scoped_context,
                workflow_run_id=run_id,
                organization_id="org_1",
                workflow_permanent_id="wpid_1",
                workflow_status=WorkflowStatus.published,
                trigger_type=WorkflowRunTriggerType.api,
                ineligibility_reason=reason,
            )
        tier.assert_not_awaited()
        assert mock_log.info.call_args.kwargs["billing_tier"] is None
        assert scoped_context.workflow_block_engine_override is None


@pytest.mark.asyncio
async def test_a_failing_billing_tier_lookup_cannot_decide_the_arm(scoped_context: SkyvernContext) -> None:
    # The resolver's catch-all turns any exception inside it into control, so a tier read that
    # escaped would let a billing-lookup outage move every run of the experiment onto v1 -- a
    # dependency the arm never had. The treated run must still be treated.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    with patch(WORKFLOW_BLOCK_ENGINE_APP_TARGET) as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER = provider
        mock_app.AGENT_FUNCTION.resolve_billing_tier = AsyncMock(side_effect=RuntimeError("billing down"))
        await resolve_workflow_block_engine_arm(
            scoped_context,
            workflow_run_id="wr_tier_down",
            organization_id="org_1",
            workflow_permanent_id="wpid_1",
            workflow_status=WorkflowStatus.published,
            trigger_type=WorkflowRunTriggerType.api,
            ineligibility_reason=None,
        )

    assert scoped_context.workflow_block_engine_override == RunEngine.skyvern_v3
    by_flag = {flag: properties or {} for flag, _distinct_id, properties in provider.calls}
    assert by_flag[WORKFLOW_TASK_V3_AB_FLAG][BILLING_TIER_PROPERTY] == "unknown"


@pytest.mark.asyncio
async def test_new_self_serve_workflow_defaults_to_v3_without_consulting_the_ab_flag(
    scoped_context: SkyvernContext,
    v3_default_cutoff: datetime,
) -> None:
    # The whole point of the rule: the percentage knob is bypassed, so a flag sitting at 0% (or a
    # bucket that landed on control) cannot take a new self-serve workflow back to v1.
    provider = FakeExperimentationProvider(
        {WORKFLOW_TASK_V3_AB_FLAG: False, TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG: True}
    )
    run_id = "wr_new_self_serve_enrolled"
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id=run_id,
        ineligibility_reason=None,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=v3_default_cutoff.replace(tzinfo=None) + timedelta(seconds=1),
    )

    assert scoped_context.workflow_block_engine_override == RunEngine.skyvern_v3
    assert _make_block(TaskBlock, label="new_wf").resolve_engine(run_id) == RunEngine.skyvern_v3
    # The percentage knob is still never consulted for an enrolled run.
    assert consulted_flags(provider) == [DISABLE_TASK_V3_FLAG, TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG]
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.new_self_serve_workflow_default
    assert resolution.log["new_workflow_default_rollout"] is True
    assert resolution.log["new_workflow_default_rollout_resolution"] == NewWorkflowDefaultRollout.enrolled
    assert resolution.log["billing_tier"] == BillingTier.SELF_SERVE.value


@pytest.mark.parametrize(
    ("ab_flag", "expected_override", "expected_reason"),
    [
        (True, RunEngine.skyvern_v3, WorkflowBlockEngineRouteReason.flag_bucket_treatment),
        (False, None, WorkflowBlockEngineRouteReason.flag_bucket_control),
    ],
)
@pytest.mark.asyncio
async def test_an_unenrolled_new_workflow_is_bucketed_by_the_ab_as_if_the_rule_did_not_exist(
    scoped_context: SkyvernContext,
    v3_default_cutoff: datetime,
    ab_flag: bool,
    expected_override: RunEngine | None,
    expected_reason: WorkflowBlockEngineRouteReason,
) -> None:
    # A conclusive False is what an operator's fastest off-gesture produces: posthog's local evaluator
    # answers False for an INACTIVE flag before it reads any filter, and the same value comes back for
    # a run outside the percentage or excluded by a condition. Enrolling on it would put 100% of this
    # population on v3 with the control cell zeroed at the exact moment someone switched the rollout
    # off, and the log would read like a rule that was not firing. So False leaves the run in the
    # ordinary bucketing, both ways -- which is also where this population's concurrent control and
    # its scoped kill come from, the cutoff being a setting that needs a restart and a condition on
    # WORKFLOW_TASK_V3_AB being unable to reach a run that never evaluates it.
    provider = FakeExperimentationProvider(
        {WORKFLOW_TASK_V3_AB_FLAG: ab_flag, TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG: False}
    )
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id=f"wr_not_enrolled_{ab_flag}",
        ineligibility_reason=None,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=v3_default_cutoff.replace(tzinfo=None) + timedelta(days=1),
    )

    assert scoped_context.workflow_block_engine_override == expected_override
    assert resolution.log["route_reason"] == expected_reason
    assert resolution.log["new_workflow_default_rollout"] is False
    assert resolution.log["new_workflow_default_rollout_resolution"] == NewWorkflowDefaultRollout.not_enrolled
    rollout_call = next(call for call in provider.calls if call[0] == TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG)
    # Bucketed per run, and carrying the two properties a scoped condition is written against: holding
    # one organization or one workflow out of the rollout is what the runbook promises as the fast
    # lever.
    assert rollout_call[1] == f"wr_not_enrolled_{ab_flag}"
    assert rollout_call[2] == {
        "organization_id": "org_1",
        "workflow_permanent_id": "wpid_1",
        BILLING_TIER_PROPERTY: BillingTier.SELF_SERVE.value,
    }


@pytest.mark.asyncio
async def test_a_failing_rollout_evaluation_leaves_the_run_on_the_ab_path(
    scoped_context: SkyvernContext, v3_default_cutoff: datetime
) -> None:
    # An evaluation that raised is not an answer, and treating it as one would enrol a population
    # whose fastest kill is the flag that just failed. It also must not be reported as a real
    # "outside the percentage": the error resolution is what separates the two on the log. Only
    # reachable through the strict resolver -- the boolean one swallows the error into None and
    # bool()s it to False.
    provider = FakeExperimentationProvider(
        {WORKFLOW_TASK_V3_AB_FLAG: False}, strict_error_flags={TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG}
    )
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id="wr_rollout_down",
        ineligibility_reason=None,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=v3_default_cutoff.replace(tzinfo=None) + timedelta(days=1),
    )

    assert scoped_context.workflow_block_engine_override is None
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.flag_bucket_control
    assert resolution.log["new_workflow_default_rollout"] is False
    assert resolution.log["new_workflow_default_rollout_resolution"] == NewWorkflowDefaultRollout.error
    assert WORKFLOW_TASK_V3_AB_FLAG in consulted_flags(provider)
    # A rollout that stopped answering must not be silent: an operator reading a quiet
    # new_self_serve_workflow_default needs this warning and the resolution field above to tell a
    # broken evaluation from a rollout nobody enabled.
    assert [call.kwargs.get("workflow_run_id") for call in resolution.warnings] == ["wr_rollout_down"]


@pytest.mark.parametrize(
    ("ab_flag", "expected_override", "expected_reason"),
    [
        (True, RunEngine.skyvern_v3, WorkflowBlockEngineRouteReason.flag_bucket_treatment),
        (False, None, WorkflowBlockEngineRouteReason.flag_bucket_control),
    ],
)
@pytest.mark.asyncio
async def test_an_unresolvable_rollout_flag_leaves_the_run_on_the_ab_path(
    scoped_context: SkyvernContext,
    v3_default_cutoff: datetime,
    ab_flag: bool,
    expected_override: RunEngine | None,
    expected_reason: WorkflowBlockEngineRouteReason,
) -> None:
    # The arm-resolving processes evaluate PostHog locally against a snapshot, which answers None --
    # not False, not an error -- for a flag key it has no row for; so does a cutoff set before anyone
    # created the flag, and so does a condition local evaluation cannot answer. Enrolling on that
    # would make the cutoff alone a 100%-v3 cohort with no concurrent control, and would bypass the
    # organization exclusions this rollout's own conditions carry. So an unresolved flag leaves the
    # run in the ordinary bucketing, both ways -- and the resolution field still separates "no flag"
    # from a rollout that answered, which is what an operator reads after an enable.
    provider = FakeExperimentationProvider(
        {WORKFLOW_TASK_V3_AB_FLAG: ab_flag},
        unresolvable_flags={TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG},
    )
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id=f"wr_rollout_undefined_{ab_flag}",
        ineligibility_reason=None,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=v3_default_cutoff.replace(tzinfo=None) + timedelta(days=1),
    )

    assert scoped_context.workflow_block_engine_override == expected_override
    assert resolution.log["route_reason"] == expected_reason
    assert resolution.log["new_workflow_default_rollout"] is False
    assert resolution.log["new_workflow_default_rollout_resolution"] == NewWorkflowDefaultRollout.undefined
    assert WORKFLOW_TASK_V3_AB_FLAG in consulted_flags(provider)


@pytest.mark.asyncio
async def test_a_new_version_of_an_older_workflow_is_not_a_new_workflow(
    scoped_context: SkyvernContext, v3_default_cutoff: datetime
) -> None:
    # A workflow saved again after the cutoff keeps an old birthday. resolve_arm wires the latest
    # version at LATEST_VERSION_CREATED_AT, on the far side of the cutoff, so a resolver that read
    # the version this run executes would enrol this workflow and flip the arm below to treatment --
    # which is the regression that would otherwise take every long-lived workflow onto v3 at once.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: False})
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id="wr_old_wf",
        ineligibility_reason=None,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=v3_default_cutoff.replace(tzinfo=None) - timedelta(days=30),
    )

    assert scoped_context.workflow_block_engine_override is None
    assert DISABLE_TASK_V3_FLAG in [flag for flag, _distinct_id, _properties in provider.calls]
    assert WORKFLOW_TASK_V3_AB_FLAG in [flag for flag, _distinct_id, _properties in provider.calls]
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.flag_bucket_control
    assert TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG not in consulted_flags(provider)
    # Asserted here rather than inside the fake, whose AssertionError the rule's catch-all would
    # swallow into "not a new workflow": the birth timestamp is read once, scoped to this run's own
    # organization. The read itself is the min over every version of the id including deleted ones,
    # so a deleted or copilot-test first version cannot make a long-lived workflow look unborn.
    resolution.birth_reads.assert_awaited_once_with("wpid_1", "org_1")


@pytest.mark.asyncio
async def test_a_per_call_auto_generated_workflow_is_never_a_new_workflow(
    scoped_context: SkyvernContext, v3_default_cutoff: datetime
) -> None:
    # /v1/run/tasks/login, /v1/run/tasks/download_files, the two credential test-login endpoints and
    # the SDK run endpoint each mint a fresh workflow_permanent_id per call and run it while it is
    # still auto_generated, holding a v3-eligible block. Every one of those is born after any cutoff,
    # so without this exclusion setting the cutoff moves all of that standing API traffic onto v3 at
    # once instead of only the workflows customers keep.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: False})
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id="wr_auto_generated",
        ineligibility_reason=None,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=v3_default_cutoff.replace(tzinfo=None) + timedelta(days=1),
        workflow_status=WorkflowStatus.auto_generated,
    )

    assert scoped_context.workflow_block_engine_override is None
    assert WORKFLOW_TASK_V3_AB_FLAG in consulted_flags(provider)
    assert TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG not in consulted_flags(provider)
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.flag_bucket_control
    assert resolution.log["new_workflow_default_rollout_resolution"] is None
    # A status the rule cannot use must not pay for the workflow read either.
    resolution.birth_reads.assert_not_awaited()


@pytest.mark.parametrize(
    "trigger_type",
    [WorkflowRunTriggerType.job_recipe_apply, WorkflowRunTriggerType.job_recipe_extract],
)
@pytest.mark.asyncio
async def test_a_per_call_recipe_run_is_never_a_new_workflow(
    scoped_context: SkyvernContext,
    v3_default_cutoff: datetime,
    trigger_type: WorkflowRunTriggerType,
) -> None:
    # The job-recipe endpoints also mint a fresh workflow_permanent_id per request and run it in that
    # same request, but the definition they build carries no status and so is born published: the
    # auto_generated exclusion cannot see it, and every one of those ids is born after any cutoff.
    # One run per permanent id also means an enrolled recipe call has no control run of the same
    # workflow to be matched against, so the trigger kind is what keeps this standing API traffic
    # randomized by the A/B instead of force-routed.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: False})
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id=f"wr_{trigger_type.value}",
        ineligibility_reason=None,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=v3_default_cutoff.replace(tzinfo=None) + timedelta(days=1),
        workflow_status=WorkflowStatus.published,
        trigger_type=trigger_type,
    )

    assert scoped_context.workflow_block_engine_override is None
    assert WORKFLOW_TASK_V3_AB_FLAG in consulted_flags(provider)
    assert TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG not in consulted_flags(provider)
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.flag_bucket_control
    assert resolution.log["new_workflow_default_rollout_resolution"] is None
    # A trigger the rule cannot use must not pay for the workflow read either.
    resolution.birth_reads.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_workflow_born_auto_generated_and_kept_by_the_customer_is_a_new_workflow(
    scoped_context: SkyvernContext, v3_default_cutoff: datetime
) -> None:
    # POST /workflows/create-from-prompt -- the prompt box, the product's main new-workflow path --
    # writes version 1 as auto_generated whenever publish_workflow is false, which is its default, and
    # then drops the customer into the editor on that same workflow_permanent_id; an editor save
    # writes the next version as published. Excluding on the BIRTH version's status would therefore
    # exclude every prompt-created workflow forever, including the ones a customer edits and re-runs
    # for months, and the rule's whole stated purpose would be defeated for the path that creates most
    # of its population. The exclusion is about the version this run executes, not about how the id
    # was born.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: False})
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id="wr_prompt_created_kept",
        ineligibility_reason=None,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=v3_default_cutoff.replace(tzinfo=None) + timedelta(days=1),
        first_version_status=WorkflowStatus.auto_generated,
        workflow_status=WorkflowStatus.published,
    )

    assert scoped_context.workflow_block_engine_override == RunEngine.skyvern_v3
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.new_self_serve_workflow_default
    assert WORKFLOW_TASK_V3_AB_FLAG not in consulted_flags(provider)


@pytest.mark.parametrize(
    ("first_version_created_at", "expect_v3_default"),
    [
        (datetime(2026, 9, 14, 23, 59, 59), False),
        # Naive rows come from datetime.utcnow(): the cutoff is aware, so a resolver that compared
        # them directly would raise and lose every enrolment. At-or-after includes the instant itself.
        (datetime(2026, 9, 15, 0, 0, 0), True),
        (datetime(2026, 9, 15, 0, 0, 1), True),
        (datetime(2026, 9, 15, 0, 0, 1, tzinfo=UTC), True),
        # 2026-09-14 21:00 at UTC-4 is 2026-09-15 01:00 UTC, i.e. after the cutoff despite the date.
        (datetime(2026, 9, 14, 21, 0, 0, tzinfo=timezone(timedelta(hours=-4))), True),
    ],
)
@pytest.mark.asyncio
async def test_cutoff_compares_aware_and_naive_first_version_timestamps_in_utc(
    scoped_context: SkyvernContext,
    v3_default_cutoff: datetime,
    first_version_created_at: datetime,
    expect_v3_default: bool,
) -> None:
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: False})
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id=f"wr_cutoff_{first_version_created_at.isoformat()}",
        ineligibility_reason=None,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=first_version_created_at,
    )

    expected_override = RunEngine.skyvern_v3 if expect_v3_default else None
    assert scoped_context.workflow_block_engine_override == expected_override
    expected_reason = (
        WorkflowBlockEngineRouteReason.new_self_serve_workflow_default
        if expect_v3_default
        else WorkflowBlockEngineRouteReason.flag_bucket_control
    )
    assert resolution.log["route_reason"] == expected_reason


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="TZ cannot be changed in-process on this platform")
def test_a_naive_created_at_is_read_as_utc_not_as_local_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # "Naive means UTC" and the classic astimezone() spelling that means "naive means local" are the
    # same function on a UTC machine, which CI and every container is -- so neither the cases above
    # nor any assertion made under the ambient zone can tell them apart. workflows.created_at is
    # datetime.utcnow(), so reading it as local time misclassifies every workflow born within the
    # process's TZ offset of the cutoff, silently enrolling or excluding runs that have no control arm.
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert _as_utc(datetime(2026, 9, 15, 12)) == datetime(2026, 9, 15, 12, tzinfo=UTC)
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("billing_tier", [BillingTier.ENTERPRISE, BillingTier.UNKNOWN])
@pytest.mark.asyncio
async def test_only_the_self_serve_tier_gets_the_new_workflow_default(
    scoped_context: SkyvernContext, v3_default_cutoff: datetime, billing_tier: BillingTier
) -> None:
    # UNKNOWN is a failed lookup, so it must fall through with enterprise rather than be force-routed.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id=f"wr_tier_{billing_tier.value}",
        ineligibility_reason=None,
        billing_tier=billing_tier,
        first_version_created_at=v3_default_cutoff.replace(tzinfo=None) + timedelta(days=1),
    )

    assert WORKFLOW_TASK_V3_AB_FLAG in [flag for flag, _distinct_id, _properties in provider.calls]
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.flag_bucket_treatment
    # A tier the rule cannot use must not pay for the workflow read either.
    resolution.birth_reads.assert_not_awaited()
    assert TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG not in consulted_flags(provider)


@pytest.mark.asyncio
async def test_kill_switch_wins_over_the_new_workflow_default(
    scoped_context: SkyvernContext, v3_default_cutoff: datetime
) -> None:
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True, DISABLE_TASK_V3_FLAG: True})
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id="wr_new_but_killed",
        ineligibility_reason=None,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=v3_default_cutoff.replace(tzinfo=None) + timedelta(days=1),
    )

    assert scoped_context.workflow_block_engine_override is None
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.disabled
    resolution.birth_reads.assert_not_awaited()
    assert TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG not in consulted_flags(provider)


@pytest.mark.asyncio
async def test_no_cutoff_leaves_every_workflow_on_the_flag_path(scoped_context: SkyvernContext) -> None:
    # Deliberately does not request v3_default_cutoff: the autouse pin holds the setting at None.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: False})
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id="wr_no_cutoff",
        ineligibility_reason=None,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=datetime(2030, 1, 1),
    )

    assert scoped_context.workflow_block_engine_override is None
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.flag_bucket_control
    resolution.birth_reads.assert_not_awaited()
    assert TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG not in consulted_flags(provider)


@pytest.mark.asyncio
async def test_ineligible_run_is_never_defaulted_to_v3(
    scoped_context: SkyvernContext, v3_default_cutoff: datetime
) -> None:
    # Run-level eligibility is about whether the run can be rerouted at all, so the rule changes the
    # arm decision only: a script run or a pinned block still executes as authored.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id="wr_new_but_ineligible",
        ineligibility_reason=V3AbIneligibleReason.script_run,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=v3_default_cutoff.replace(tzinfo=None) + timedelta(days=1),
    )

    assert scoped_context.workflow_block_engine_override is None
    assert provider.calls == []
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.ineligible
    resolution.birth_reads.assert_not_awaited()
    assert TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG not in consulted_flags(provider)


@pytest.mark.asyncio
async def test_a_failing_first_version_read_falls_back_to_the_flag_path(
    scoped_context: SkyvernContext, v3_default_cutoff: datetime
) -> None:
    # The new rule must not be able to take the run off the arm it would have had: a database blip
    # falls back to the A/B, not to control, and never forces v3.
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id="wr_first_version_down",
        ineligibility_reason=None,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_error=RuntimeError("workflows read failed"),
    )

    assert scoped_context.workflow_block_engine_override == RunEngine.skyvern_v3
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.flag_bucket_treatment
    # The fallback is silent in the arm it produces, so the swallowed read has to be visible
    # somewhere: a rule that stops enrolling because a query broke must not look like a rule nobody
    # set the cutoff for.
    assert [call.kwargs.get("workflow_permanent_id") for call in resolution.warnings] == ["wpid_1"]


@pytest.mark.asyncio
async def test_a_missing_first_version_falls_back_to_the_flag_path(
    scoped_context: SkyvernContext, v3_default_cutoff: datetime
) -> None:
    provider = FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: False})
    resolution = await resolve_arm(
        scoped_context,
        provider,
        workflow_run_id="wr_first_version_missing",
        ineligibility_reason=None,
        billing_tier=BillingTier.SELF_SERVE,
        first_version_created_at=None,
    )

    assert scoped_context.workflow_block_engine_override is None
    assert resolution.log["route_reason"] == WorkflowBlockEngineRouteReason.flag_bucket_control
