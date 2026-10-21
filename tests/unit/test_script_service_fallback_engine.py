"""Tests for engine inheritance in script_service._fallback_to_ai_run.

When a cached script block fails and falls back to the agent, the fallback TaskBlock must
inherit the engine configured on the original block in the run-bound workflow definition,
not silently pin to skyvern_v1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.config import settings
from skyvern.errors.errors import UserDefinedError
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.block import (
    BaseTaskBlock,
    Block,
    TaskBlock,
    takes_default_engine,
    v3_ab_ineligibility_reason,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.runs import RunEngine
from skyvern.schemas.workflows import BlockType, WorkflowDefinitionYAML
from skyvern.services import script_service
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ActionStatus
from tests.unit._workflow_block_engine_fakes import FakeExperimentationProvider, resolve_arm

MODULE = "skyvern.services.script_service"


def _make_output_parameter(key: str) -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        output_parameter_id=f"op_{key}",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


def _make_task_block(label: str, engine: RunEngine | None = RunEngine.skyvern_v1) -> TaskBlock:
    return TaskBlock(
        label=label,
        output_parameter=_make_output_parameter(f"{label}_output"),
        title=label,
        engine=engine,
    )


def _make_workflow(blocks: list[TaskBlock]) -> Workflow:
    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id="w_test",
        organization_id="o_test",
        title="test workflow",
        workflow_permanent_id="wpid_test",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=blocks),
        created_at=now,
        modified_at=now,
    )


def _make_context() -> SkyvernContext:
    return SkyvernContext(
        organization_id="o_test",
        workflow_run_id="wr_test",
        workflow_id="w_test",
        task_id="tsk_test",
        step_id="stp_test",
    )


def _make_app(workflow: Workflow) -> MagicMock:
    app = MagicMock()
    app.DATABASE.tasks.update_step = AsyncMock(return_value=SimpleNamespace(order=0))
    app.DATABASE.organizations.get_organization = AsyncMock(return_value=SimpleNamespace())
    app.DATABASE.tasks.get_task = AsyncMock(return_value=SimpleNamespace(url="https://example.com"))
    app.DATABASE.workflows.get_workflow = AsyncMock(return_value=workflow)
    app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=SimpleNamespace(ai_fallback=True))
    app.DATABASE.tasks.create_step = AsyncMock(return_value=SimpleNamespace(step_id="stp_ai_1"))
    app.DATABASE.workflow_runs.update_workflow_run = AsyncMock()
    app.agent.execute_step = AsyncMock()
    return app


async def _run_fallback(cache_key: str, workflow: Workflow, engine: RunEngine = RunEngine.skyvern_v1) -> MagicMock:
    """Run `_fallback_to_ai_run` against `workflow` and return the mocked app for assertions."""
    app = _make_app(workflow)
    with (
        patch(f"{MODULE}.app", app),
        patch(f"{MODULE}.skyvern_context.current", return_value=_make_context()),
    ):
        await script_service._fallback_to_ai_run(
            block_type=BlockType.NAVIGATION,
            cache_key=cache_key,
            prompt="do the thing",
            engine=engine,
        )
    return app


def _fallback_task_block(app: MagicMock) -> TaskBlock:
    # The dispatch gate reads the engine PARAM, not task_block.engine — assert both stay in sync
    # so an inert-inheritance regression (block carries v3, dispatch gets default) cannot pass.
    kwargs = app.agent.execute_step.call_args.kwargs
    assert kwargs["engine"] == kwargs["task_block"].engine
    return kwargs["task_block"]


@pytest.mark.asyncio
async def test_fallback_inherits_engine_from_run_bound_definition() -> None:
    workflow = _make_workflow([_make_task_block("my_block", engine=RunEngine.skyvern_v3)])

    app = await _run_fallback("my_block", workflow)

    assert _fallback_task_block(app).engine == RunEngine.skyvern_v3


@pytest.mark.asyncio
async def test_fallback_keeps_default_engine_when_block_engine_is_default() -> None:
    workflow = _make_workflow([_make_task_block("my_block", engine=RunEngine.skyvern_v1)])

    app = await _run_fallback("my_block", workflow)

    assert _fallback_task_block(app).engine == RunEngine.skyvern_v1


@pytest.mark.asyncio
async def test_fallback_keeps_default_engine_when_block_missing_from_definition() -> None:
    workflow = _make_workflow([_make_task_block("other_block", engine=RunEngine.skyvern_v3)])

    app = await _run_fallback("my_block", workflow)

    assert _fallback_task_block(app).engine == RunEngine.skyvern_v1


@pytest.mark.asyncio
async def test_failed_cached_task_carries_detected_codes_to_block_update() -> None:
    workflow = _make_workflow([_make_task_block("my_block")])
    app = _make_app(workflow)
    app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=SimpleNamespace(ai_fallback=False))
    app.DATABASE.tasks.get_task = AsyncMock(return_value=SimpleNamespace(errors=[]))
    app.DATABASE.tasks.update_task = AsyncMock()
    error = UserDefinedError(error_code="blocked", reasoning="Blocked", confidence_float=1.0)
    update_block = AsyncMock()

    with (
        patch(f"{MODULE}.app", app),
        patch(f"{MODULE}.skyvern_context.current", return_value=_make_context()),
        patch(f"{MODULE}._detect_user_defined_errors", new=AsyncMock(return_value=[error])),
        patch(f"{MODULE}._update_workflow_block", update_block),
    ):
        await script_service._fallback_to_ai_run(
            block_type=BlockType.NAVIGATION,
            cache_key="my_block",
            error_code_mapping={"blocked": "Blocked"},
            error=RuntimeError("Script failed"),
            workflow_run_block_id="wrb_test",
        )

    assert update_block.await_args.kwargs["error_codes"] == ["blocked"]


@pytest.mark.asyncio
async def test_ai_fallback_carries_refreshed_task_codes_to_block_update() -> None:
    workflow = _make_workflow([_make_task_block("my_block")])
    app = _make_app(workflow)
    app.DATABASE.tasks.get_task = AsyncMock(
        side_effect=[
            SimpleNamespace(url="https://example.com", errors=[]),
            SimpleNamespace(
                status=TaskStatus.failed,
                failure_reason="Fallback identified the failure",
                errors=[{"error_code": "picked", "reasoning": "Matched"}],
            ),
        ]
    )
    update_block = AsyncMock()

    with (
        patch(f"{MODULE}.app", app),
        patch(f"{MODULE}.skyvern_context.current", return_value=_make_context()),
        patch(f"{MODULE}._update_workflow_block", update_block),
    ):
        await script_service._fallback_to_ai_run(
            block_type=BlockType.NAVIGATION,
            cache_key="my_block",
            workflow_run_block_id="wrb_test",
        )

    assert update_block.await_args.kwargs["error_codes"] == ["picked"]


@pytest.mark.asyncio
async def test_fallback_fails_open_to_default_when_engine_lookup_raises() -> None:
    workflow = _make_workflow([_make_task_block("my_block", engine=RunEngine.skyvern_v3)])
    app = _make_app(workflow)

    with (
        patch(f"{MODULE}.app", app),
        patch(f"{MODULE}.skyvern_context.current", return_value=_make_context()),
        patch(f"{MODULE}._resolve_original_block_engine", side_effect=RuntimeError("boom")),
    ):
        await script_service._fallback_to_ai_run(
            block_type=BlockType.NAVIGATION,
            cache_key="my_block",
            prompt="do the thing",
        )

    assert _fallback_task_block(app).engine == RunEngine.skyvern_v1


@pytest.mark.asyncio
async def test_fallback_respects_explicit_engine_without_lookup() -> None:
    workflow = _make_workflow([_make_task_block("my_block", engine=RunEngine.skyvern_v3)])
    app = _make_app(workflow)

    with (
        patch(f"{MODULE}.app", app),
        patch(f"{MODULE}.skyvern_context.current", return_value=_make_context()),
        patch(f"{MODULE}._resolve_original_block_engine") as resolve_mock,
    ):
        await script_service._fallback_to_ai_run(
            block_type=BlockType.NAVIGATION,
            cache_key="my_block",
            prompt="do the thing",
            engine=RunEngine.skyvern_v2,
        )

    resolve_mock.assert_not_called()
    assert _fallback_task_block(app).engine == RunEngine.skyvern_v2


@pytest.mark.parametrize(
    ("born_at", "expected"),
    [
        (datetime(2026, 10, 2, tzinfo=timezone.utc), RunEngine.skyvern_v3),
        (datetime(2026, 9, 1, tzinfo=timezone.utc), RunEngine.skyvern_v1),
    ],
)
@pytest.mark.asyncio
async def test_fallback_of_an_unset_engine_block_follows_the_chosen_engine_cutoff(
    monkeypatch: pytest.MonkeyPatch, born_at: datetime, expected: RunEngine
) -> None:
    # A script run of a workflow born past the cutoff: its cached block's AI fallback must run where
    # the same block would run uncached, v3, not on the skyvern_v1 an unset engine used to mean.
    monkeypatch.setattr(settings, "TASK_V3_DEFAULT_ENGINE_WORKFLOW_CUTOFF", None)
    monkeypatch.setattr(settings, "TASK_V3_CHOSEN_ENGINE_CUTOFF", datetime(2026, 10, 1, tzinfo=timezone.utc))
    blocks = [_make_task_block("my_block", engine=None)]
    workflow = _make_workflow(blocks)
    context = _make_context()
    await resolve_arm(
        context,
        FakeExperimentationProvider(),
        workflow_run_id="wr_test",
        ineligibility_reason=v3_ab_ineligibility_reason(blocks),
        takes_default_engine=takes_default_engine(blocks),
        first_version_created_at=born_at,
    )
    app = _make_app(workflow)

    with patch(f"{MODULE}.app", app), patch(f"{MODULE}.skyvern_context.current", return_value=context):
        await script_service._fallback_to_ai_run(
            block_type=BlockType.NAVIGATION, cache_key="my_block", prompt="do the thing"
        )

    assert _fallback_task_block(app).engine == expected


_UNCACHED_HELPER_CALLS = {
    "navigation": lambda: script_service.run_task(prompt="p", label="navigation"),
    "file_download": lambda: script_service.download(prompt="p", label="file_download"),
    "action": lambda: script_service.action(prompt="p", label="action"),
    "login": lambda: script_service.login(prompt="p", label="login"),
    "extraction": lambda: script_service.extract(prompt="p", label="extraction"),
    "validation": lambda: script_service.execute_validation("done", None, None, label="validation"),
}


class _Built(Exception):
    pass


_CUTOFF = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("cutoff", "born_at"),
    [
        pytest.param(_CUTOFF, datetime(2026, 10, 2, tzinfo=timezone.utc), id="past_cutoff"),
        pytest.param(_CUTOFF, datetime(2026, 9, 30, tzinfo=timezone.utc), id="born_before_cutoff"),
        pytest.param(None, datetime(2026, 10, 2, tzinfo=timezone.utc), id="cutoff_unset"),
    ],
)
@pytest.mark.parametrize("stored_engine", [None, RunEngine.skyvern_v1.value, RunEngine.skyvern_v3.value])
@pytest.mark.parametrize("block_type", list(_UNCACHED_HELPER_CALLS))
@pytest.mark.asyncio
async def test_an_uncached_script_block_runs_where_its_stored_block_would_only_past_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    block_type: str,
    stored_engine: str | None,
    cutoff: datetime | None,
    born_at: datetime,
) -> None:
    monkeypatch.setattr(settings, "TASK_V3_DEFAULT_ENGINE_WORKFLOW_CUTOFF", None)
    monkeypatch.setattr(settings, "TASK_V3_CHOSEN_ENGINE_CUTOFF", cutoff)
    goal = {"validation": "complete_criterion", "extraction": "data_extraction_goal"}.get(block_type, "navigation_goal")
    stored_block = {"block_type": block_type, "label": block_type, goal: "p"}
    if stored_engine is not None:
        stored_block["engine"] = stored_engine
    # A second block left unset makes the run take the default engine, so a skyvern-1.0 pin is honored.
    unset_block = {"block_type": "navigation", "label": "unset", "navigation_goal": "p"}
    definition = convert_workflow_definition(
        WorkflowDefinitionYAML.model_validate({"parameters": [], "blocks": [stored_block, unset_block]}), "w_test"
    )
    workflow = _make_workflow(definition.blocks)
    stored = definition.blocks[0]
    context = _make_context()
    await resolve_arm(
        context,
        FakeExperimentationProvider(),
        workflow_run_id="wr_test",
        ineligibility_reason=v3_ab_ineligibility_reason(workflow.workflow_definition.blocks),
        takes_default_engine=takes_default_engine(workflow.workflow_definition.blocks),
        first_version_created_at=born_at,
    )
    ran_on: list[tuple[RunEngine, RunEngine]] = []

    async def capture(self: Block, **_: object) -> None:
        assert isinstance(self, BaseTaskBlock) and isinstance(stored, BaseTaskBlock)
        ran_on.append((self.resolve_engine("wr_test"), stored.resolve_engine("wr_test")))
        raise _Built

    with (
        patch(f"{MODULE}.app", _make_app(workflow)),
        patch(f"{MODULE}.skyvern_context.current", return_value=context),
        patch(f"{MODULE}.skyvern_context.ensure_context", return_value=context),
        patch(f"{MODULE}.script_run_context_manager.get_cached_fn", return_value=None),
        patch.object(Block, "execute_safe", capture),
        pytest.raises(_Built),
    ):
        await _UNCACHED_HELPER_CALLS[block_type]()

    [(helper_engine, stored_block_engine)] = ran_on
    if cutoff is not None and born_at > cutoff:
        assert helper_engine == stored_block_engine
    else:
        # Outside the cutoff the helpers keep main's behaviour, where every stored engine ran on skyvern_v1.
        assert helper_engine == RunEngine.skyvern_v1


def test_resolver_finds_loop_nested_block_engine() -> None:
    # A cached block inside a for-loop must keep its configured engine on fallback; the lookup
    # is recursive (labels are globally unique, nested included).
    from skyvern.forge.sdk.workflow.models.block import ForLoopBlock

    nested = _make_task_block("inner_block", RunEngine.skyvern_v3)
    loop = ForLoopBlock(
        label="outer_loop",
        loop_blocks=[nested],
        loop_over=None,
        loop_variable_reference="items",
        output_parameter=nested.output_parameter,
    )
    workflow = _make_workflow([loop])
    assert script_service._resolve_original_block_engine("inner_block", workflow, None) == RunEngine.skyvern_v3
    assert script_service._resolve_original_block_engine("missing", workflow, None) is None


def _make_run_context(values: dict[str, object]) -> MagicMock:
    workflow_run_context = MagicMock()
    workflow_run_context.values = dict(values)
    workflow_run_context.get_block_metadata.return_value = {}
    workflow_run_context.workflow_title = "test workflow"
    workflow_run_context.workflow_id = "w_test"
    workflow_run_context.workflow_permanent_id = "wpid_test"
    workflow_run_context.workflow_run_id = "wr_test"
    workflow_run_context.browser_session_id = None
    return workflow_run_context


async def _resolve_otp(
    workflow: Workflow,
    values: dict[str, object],
    totp_identifier: str | None = None,
    totp_url: str | None = None,
) -> tuple[tuple[str | None, str | None], MagicMock]:
    app = _make_app(workflow)
    app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = _make_run_context(values)
    with (
        patch(f"{MODULE}.app", app),
        patch(f"{MODULE}.skyvern_context.current", return_value=_make_context()),
    ):
        resolved = await script_service._resolve_block_otp_config("my_block", totp_identifier, totp_url)
    return resolved, app


@pytest.mark.asyncio
async def test_otp_config_inherited_from_block_definition_when_call_site_omits_it() -> None:
    # Static-script run signatures omit the block's totp fields (SKY-15221); the task the
    # script path creates must still poll the identifier the workflow block configured.
    block = _make_task_block("my_block")
    block.totp_identifier = "{{ email }}"
    workflow = _make_workflow([block])

    (identifier, url), _ = await _resolve_otp(workflow, {"email": "candidate+x@gmail.com"})

    assert identifier == "candidate+x@gmail.com"
    assert url is None


@pytest.mark.asyncio
async def test_unresolvable_otp_template_yields_none_not_the_literal() -> None:
    # A literal "{{ email }}" as identifier would match nothing for the whole poll —
    # worse than no identifier, because the failure reads as "code never arrived".
    block = _make_task_block("my_block")
    block.totp_identifier = "{{ email }}"
    workflow = _make_workflow([block])

    (identifier, _), _ = await _resolve_otp(workflow, {})

    assert identifier is None


@pytest.mark.asyncio
async def test_call_site_otp_values_pass_through_without_workflow_lookup() -> None:
    workflow = _make_workflow([_make_task_block("my_block")])

    (identifier, url), app = await _resolve_otp(workflow, {}, totp_identifier="direct@example.com")

    assert identifier == "direct@example.com"
    assert url is None
    app.DATABASE.workflows.get_workflow.assert_not_called()


@pytest.mark.asyncio
async def test_otp_inheritance_finds_loop_nested_block() -> None:
    from skyvern.forge.sdk.workflow.models.block import ForLoopBlock

    nested = _make_task_block("my_block")
    nested.totp_identifier = "{{ email }}"
    loop = ForLoopBlock(
        label="outer_loop",
        loop_blocks=[nested],
        loop_over=None,
        loop_variable_reference="items",
        output_parameter=nested.output_parameter,
    )
    workflow = _make_workflow([loop])

    (identifier, _), _ = await _resolve_otp(workflow, {"email": "candidate+x@gmail.com"})

    assert identifier == "candidate+x@gmail.com"


@pytest.mark.asyncio
async def test_block_screenshot_without_browser_state_is_not_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    # A block that runs before a browser exists, or never needs one, has no screenshot to take;
    # skipping it is routine and must not compete with real warnings.
    from skyvern.services import script_service as script_service_module

    log = MagicMock()
    monkeypatch.setattr(script_service_module, "LOG", log)
    monkeypatch.setattr(
        "skyvern.services.script_service.app.BROWSER_MANAGER.get_for_workflow_run", lambda *_a, **_k: None
    )

    await script_service_module._take_workflow_run_block_screenshot("wr_test", "o_test", MagicMock())

    log.warning.assert_not_called()
    log.info.assert_called_once()
    assert log.info.call_args.args[0] == "No browser state found when creating workflow_run_block"


@pytest.mark.asyncio
async def test_block_screenshot_timeout_is_logged_and_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    # The pre-block capture is best effort: a capture that runs out of budget must not abort the block setup
    # that already persisted its rows, mirroring Block.execute_safe.
    log = MagicMock()
    monkeypatch.setattr(script_service, "LOG", log)
    browser_state = SimpleNamespace(take_fullpage_screenshot=AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(script_service.app.BROWSER_MANAGER, "get_for_workflow_run", lambda *_a, **_k: browser_state)
    create_artifact = AsyncMock()
    monkeypatch.setattr(script_service.app.ARTIFACT_MANAGER, "create_workflow_run_block_artifact", create_artifact)

    await script_service._take_workflow_run_block_screenshot(
        "wr_test", "o_test", SimpleNamespace(workflow_run_block_id="wrb_test")
    )

    log.warning.assert_called_once()
    create_artifact.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_episode_excludes_decision_row_from_agent_action_count() -> None:
    # Twin pin of the workflow/service.py count-filter test: _fallback_to_ai_run keeps its own copy
    # of the decision-row exclusion, and a verdict row must not count as agent activity here either.
    workflow = _make_workflow([_make_task_block("my_block", engine=RunEngine.skyvern_v1)])
    workflow.run_with = "code"
    workflow.code_version = 2
    app = _make_app(workflow)
    app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(
        return_value=SimpleNamespace(ai_fallback=True, run_with=None)
    )
    app.DATABASE.tasks.get_task = AsyncMock(
        return_value=SimpleNamespace(url="https://example.com", status=TaskStatus.completed, failure_reason=None)
    )
    app.DATABASE.scripts.create_fallback_episode = AsyncMock(return_value=SimpleNamespace(episode_id="cep_1"))
    update_episode = AsyncMock()
    app.DATABASE.scripts.update_fallback_episode = update_episode
    app.DATABASE.tasks.get_task_actions = AsyncMock(
        return_value=[Action(action_type=ActionType.COMPLETE, status=ActionStatus.completed, step_id="stp_ai_1")]
    )

    # create_fallback_episode only fires when the context carries a workflow_permanent_id
    # (is_adaptive_caching's gate); _make_context() leaves it unset for the other tests in
    # this file, so this test needs its own context with it filled in.
    context = _make_context()
    context.workflow_permanent_id = "wpid_test"
    with (
        patch(f"{MODULE}.app", app),
        patch(f"{MODULE}.skyvern_context.current", return_value=context),
    ):
        await script_service._fallback_to_ai_run(
            block_type=BlockType.NAVIGATION,
            cache_key="my_block",
            prompt="do the thing",
        )

    update_episode.assert_awaited_once()
    assert update_episode.await_args.kwargs["fallback_succeeded"] is False
    assert (
        update_episode.await_args.kwargs["agent_actions"]["failure_reason"]
        == script_service.VERIFIER_SWAP_FAILURE_REASON
    )


class _DispatchReached(Exception):
    pass


@pytest.mark.parametrize(
    ("block_engine", "recorded"), [(RunEngine.skyvern_v3, ["skyvern-3.0"]), (RunEngine.skyvern_v1, [])]
)
@pytest.mark.asyncio
async def test_a_v3_fallback_records_its_engine_on_the_cached_blocks_row(
    block_engine: RunEngine, recorded: list[str]
) -> None:
    # The row was created for cached code with no engine. Script generation and the reviewer read this
    # column to keep a v3 run's selector-less actions out of the workflow's script.
    workflow = _make_workflow([_make_task_block("my_block", engine=block_engine)])
    app = _make_app(workflow)
    app.DATABASE.observer.update_workflow_run_block = AsyncMock()
    # The engine is written before dispatch; stop there instead of faking everything after it.
    app.agent.execute_step = AsyncMock(side_effect=_DispatchReached())
    with (
        patch(f"{MODULE}.app", app),
        patch(f"{MODULE}.skyvern_context.current", return_value=_make_context()),
        pytest.raises(_DispatchReached),
    ):
        await script_service._fallback_to_ai_run(
            block_type=BlockType.NAVIGATION,
            cache_key="my_block",
            prompt="do the thing",
            workflow_run_block_id="wrb_cached",
        )

    engines = [
        call.kwargs["engine"]
        for call in app.DATABASE.observer.update_workflow_run_block.call_args_list
        if "engine" in call.kwargs
    ]
    assert engines == recorded


@pytest.mark.asyncio
async def test_a_v3_fallback_that_cannot_record_its_engine_does_not_run() -> None:
    # Running anyway would leave a v3 run whose rows read v1, which script generation would then mint.
    workflow = _make_workflow([_make_task_block("my_block", engine=RunEngine.skyvern_v3)])
    app = _make_app(workflow)
    app.DATABASE.observer.update_workflow_run_block = AsyncMock(side_effect=RuntimeError("db unavailable"))
    with (
        patch(f"{MODULE}.app", app),
        patch(f"{MODULE}.skyvern_context.current", return_value=_make_context()),
        pytest.raises(RuntimeError),
    ):
        await script_service._fallback_to_ai_run(
            block_type=BlockType.NAVIGATION,
            cache_key="my_block",
            prompt="do the thing",
            workflow_run_block_id="wrb_cached",
        )

    app.agent.execute_step.assert_not_awaited()
