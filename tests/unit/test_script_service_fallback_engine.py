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

from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.block import TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.schemas.runs import RunEngine
from skyvern.schemas.workflows import BlockType
from skyvern.services import script_service
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ActionStatus

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


def _make_task_block(label: str, engine: RunEngine = RunEngine.skyvern_v1) -> TaskBlock:
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
    assert script_service._resolve_original_block_engine("inner_block", workflow) == RunEngine.skyvern_v3
    assert script_service._resolve_original_block_engine("missing", workflow) is None


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
