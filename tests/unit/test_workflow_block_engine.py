"""Tests for the WORKFLOW_TASK_V3_AB run-level engine A/B: run eligibility, arm resolution
(idempotency, kill switch, fail-closed), and the invariant that the persisted engine on
workflow_run_blocks and the dispatched engine come from the same resolution.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.experimentation.providers import BaseExperimentationProvider, NoOpExperimentationProvider
from skyvern.forge.sdk.experimentation.workflow_block_engine import (
    DISABLE_TASK_V3_FLAG,
    WORKFLOW_TASK_V3_AB_FLAG,
    resolve_workflow_block_engine_arm,
    workflow_block_engine_override,
)
from skyvern.forge.sdk.workflow.models.block import (
    ActionBlock,
    BaseTaskBlock,
    Block,
    CodeBlock,
    FileDownloadBlock,
    ForLoopBlock,
    HumanInteractionBlock,
    NavigationBlock,
    TaskBlock,
    UrlBlock,
    get_all_blocks,
    run_is_eligible_for_v3_ab,
)
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.run_enums import RunEngine
from tests.unit.helpers import make_organization
from tests.unit.test_agent_task_v3 import _make_block, _make_output_parameter, _run_execute_step_gate
from tests.unit.test_block_description_caching import _block_result, _setup_mocks
from tests.unit.test_missing_starter_url import _mock_block_execute_deps

WORKFLOW_BLOCK_ENGINE_APP_TARGET = "skyvern.forge.sdk.experimentation.workflow_block_engine.app"


class _FakeExperimentationProvider(BaseExperimentationProvider):
    def __init__(self, flags: dict[str, bool] | None = None, raise_error: bool = False) -> None:
        super().__init__()
        self.flags = dict(flags or {})
        self.calls: list[tuple[str, str, dict | None]] = []
        self.raise_error = raise_error

    async def _is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        self.calls.append((feature_name, distinct_id, properties))
        if self.raise_error:
            raise RuntimeError("provider unavailable")
        return self.flags.get(feature_name, False)

    async def _get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        return None

    async def _get_payload(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> Any:
        return None


@pytest.fixture
def scoped_context() -> Iterator[SkyvernContext]:
    context = SkyvernContext()
    skyvern_context.set(context)
    try:
        yield context
    finally:
        skyvern_context.reset()


async def _resolve(
    context: SkyvernContext,
    provider: BaseExperimentationProvider,
    *,
    workflow_run_id: str,
    run_is_eligible: bool,
    organization_id: str | None = "org_1",
    workflow_permanent_id: str | None = "wpid_1",
) -> None:
    with patch(WORKFLOW_BLOCK_ENGINE_APP_TARGET) as mock_app:
        mock_app.EXPERIMENTATION_PROVIDER = provider
        await resolve_workflow_block_engine_arm(
            context,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            run_is_eligible=run_is_eligible,
        )


def test_mixed_eligibility_run_download_block_disqualifies(scoped_context: SkyvernContext) -> None:
    eligible_1 = _make_block(TaskBlock, label="t1")
    eligible_2 = _make_block(NavigationBlock, label="t2", navigation_goal="Apply to the job")
    download_block = _make_block(ActionBlock, label="dl", complete_on_download=True)
    blocks: list[BaseTaskBlock] = [eligible_1, eligible_2, download_block]

    assert run_is_eligible_for_v3_ab(blocks, is_script_run=False) is False


def test_mixed_eligibility_run_file_download_block_disqualifies(scoped_context: SkyvernContext) -> None:
    eligible_1 = _make_block(TaskBlock, label="t1")
    eligible_2 = _make_block(NavigationBlock, label="t2", navigation_goal="Apply to the job")
    file_download_block = _make_block(FileDownloadBlock, label="fd")
    blocks: list[BaseTaskBlock] = [eligible_1, eligible_2, file_download_block]

    assert run_is_eligible_for_v3_ab(blocks, is_script_run=False) is False


@pytest.mark.asyncio
async def test_mixed_eligibility_run_pins_whole_run_to_control(scoped_context: SkyvernContext) -> None:
    eligible_1 = _make_block(TaskBlock, label="t1")
    eligible_2 = _make_block(NavigationBlock, label="t2", navigation_goal="Apply to the job")
    totp_block = _make_block(TaskBlock, label="totp", totp_verification_url="https://example.com/otp")
    blocks: list[BaseTaskBlock] = [eligible_1, eligible_2, totp_block]

    assert run_is_eligible_for_v3_ab(blocks, is_script_run=False) is False

    provider = _FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await _resolve(scoped_context, provider, workflow_run_id="wr_mixed", run_is_eligible=False)

    for block in blocks:
        assert block.resolve_engine("wr_mixed") == RunEngine.skyvern_v1
    # An ineligible run never even asks the provider -- there is nothing to bucket.
    assert provider.calls == []


@pytest.mark.asyncio
async def test_explicit_block_engine_is_never_overridden_by_treatment_arm(scoped_context: SkyvernContext) -> None:
    provider = _FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await _resolve(scoped_context, provider, workflow_run_id="wr_pinned", run_is_eligible=True)
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

    provider = _FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await _resolve(scoped_context, provider, workflow_run_id="wr_treatment", run_is_eligible=True)

    for block in blocks:
        assert block.resolve_engine("wr_treatment") == RunEngine.skyvern_v3


@pytest.mark.asyncio
async def test_arm_resolved_once_per_run_survives_mid_run_flag_flip(scoped_context: SkyvernContext) -> None:
    provider = _FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await _resolve(scoped_context, provider, workflow_run_id="wr_once", run_is_eligible=True)
    assert scoped_context.workflow_block_engine_override == RunEngine.skyvern_v3

    # Invalidate the provider's own 300s cache and flip the flag, so a second query would
    # return False if it actually reached the provider. The idempotency guard must still
    # short-circuit on context.workflow_block_engine_resolved_run_id before that happens.
    provider.invalidate_resolution_caches()
    provider.flags[WORKFLOW_TASK_V3_AB_FLAG] = False
    await _resolve(scoped_context, provider, workflow_run_id="wr_once", run_is_eligible=True)

    assert scoped_context.workflow_block_engine_override == RunEngine.skyvern_v3


@pytest.mark.asyncio
async def test_different_run_id_on_same_context_reresolves_instead_of_inheriting(
    scoped_context: SkyvernContext,
) -> None:
    provider = _FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await _resolve(scoped_context, provider, workflow_run_id="wr_A", run_is_eligible=True)
    assert workflow_block_engine_override("wr_A") == RunEngine.skyvern_v3

    provider.flags[WORKFLOW_TASK_V3_AB_FLAG] = False
    await _resolve(scoped_context, provider, workflow_run_id="wr_B", run_is_eligible=True)

    assert workflow_block_engine_override("wr_B") is None
    # The pin moved to B: A must not read as still-treatment via a stale resolution.
    assert workflow_block_engine_override("wr_A") is None


@pytest.mark.asyncio
async def test_unresolved_run_id_reads_control_without_resolving(scoped_context: SkyvernContext) -> None:
    provider = _FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await _resolve(scoped_context, provider, workflow_run_id="wr_A", run_is_eligible=True)
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
    gate_provider = _FakeExperimentationProvider()
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

    resolver_provider = _FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await _resolve(
        scoped_context,
        resolver_provider,
        workflow_run_id="wr_contract",
        run_is_eligible=True,
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
    provider = _FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True, DISABLE_TASK_V3_FLAG: True})
    await _resolve(scoped_context, provider, workflow_run_id="wr_disabled", run_is_eligible=True)

    assert scoped_context.workflow_block_engine_override is None
    block = _make_block(TaskBlock, label="disabled_block")
    assert block.resolve_engine("wr_disabled") == RunEngine.skyvern_v1


@pytest.mark.asyncio
async def test_provider_exception_fails_closed_to_control(scoped_context: SkyvernContext) -> None:
    provider = _FakeExperimentationProvider(raise_error=True)
    await _resolve(scoped_context, provider, workflow_run_id="wr_err", run_is_eligible=True)

    assert scoped_context.workflow_block_engine_override is None
    block = _make_block(TaskBlock, label="err_block")
    assert block.resolve_engine("wr_err") == RunEngine.skyvern_v1


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
        await resolve_workflow_block_engine_arm(
            scoped_context,
            workflow_run_id="wr_noop",
            organization_id="org_1",
            workflow_permanent_id="wpid_1",
            run_is_eligible=True,
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

    ineligible_nested = _make_block(ActionBlock, label="nested_dl", complete_on_download=True)
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
    provider = _FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await _resolve(scoped_context, provider, workflow_run_id="wr_inert", run_is_eligible=True)
    assert scoped_context.workflow_block_engine_override == RunEngine.skyvern_v3

    url_block = _make_block(UrlBlock, label="goto", url="https://example.com")
    human_block = _make_block(HumanInteractionBlock, label="human")

    assert url_block.resolve_engine("wr_inert") == RunEngine.skyvern_v1
    assert human_block.resolve_engine("wr_inert") == RunEngine.skyvern_v1


@pytest.mark.asyncio
async def test_exclude_from_engine_ab_block_is_never_rerouted(scoped_context: SkyvernContext) -> None:
    provider = _FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    await _resolve(scoped_context, provider, workflow_run_id="wr_excluded", run_is_eligible=True)
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


@pytest.mark.asyncio
async def test_execute_workflow_blocks_pins_the_context_execute_safe_reads_from(
    scoped_context: SkyvernContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drives the real WorkflowService._execute_workflow_blocks -> _execute_single_block ->
    Block.execute_safe chain for a single eligible TaskBlock, forcing only the experimentation
    provider. If resolve_workflow_block_engine_arm pinned the arm on a different SkyvernContext
    object than the one execute_safe's resolve_engine() reads back from, this fails: the
    persisted engine would be skyvern_v1 instead of skyvern_v3.
    """
    provider = _FakeExperimentationProvider({WORKFLOW_TASK_V3_AB_FLAG: True})
    monkeypatch.setattr(app, "EXPERIMENTATION_PROVIDER", provider)

    block = _make_block(TaskBlock, label="e2e_block")
    workflow = MagicMock()
    workflow.workflow_definition.blocks = [block]
    workflow.workflow_definition.version = 1
    workflow.workflow_definition.finally_block_label = None

    workflow_run = MagicMock()
    workflow_run.workflow_run_id = "wr_e2e"
    workflow_run.workflow_permanent_id = "wpid_e2e"
    workflow_run.organization_id = "org_e2e"
    workflow_run.run_with = None
    workflow_run.retried_from_workflow_run_id = None

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

    assert captured["engine"] == RunEngine.skyvern_v3
