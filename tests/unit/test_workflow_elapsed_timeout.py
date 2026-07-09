import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.routes.agent_protocol import _workflow_run_request_to_legacy_request
from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.models.run_limits import (
    WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES,
    WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES,
    get_effective_workflow_run_max_elapsed_time_minutes,
)
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRequestBody,
    WorkflowRun,
    WorkflowRunStatus,
)
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.runs import WorkflowRunRequest
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest, WorkflowDefinitionYAML


def _workflow_run(
    status: WorkflowRunStatus,
    *,
    started_at: datetime,
    max_elapsed_time_minutes: int | None = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_run_id="wr_1",
        workflow_id="wf_1",
        workflow_permanent_id="wp_1",
        organization_id="org_1",
        browser_profile_id="bp_1",
        browser_address=None,
        status=status,
        failure_reason=None,
        ignore_inherited_workflow_system_prompt=False,
        parent_workflow_run_id=None,
        proxy_location=None,
        max_elapsed_time_minutes=max_elapsed_time_minutes,
        started_at=started_at,
        created_at=started_at,
        code_gen=False,
        run_with="agent",
    )


def test_workflow_request_accepts_max_elapsed_time_above_legacy_four_hour_runtime() -> None:
    with pytest.warns(DeprecationWarning):
        request = WorkflowRequestBody(max_elapsed_time_minutes=300)

    assert request.max_elapsed_time_minutes == 300
    assert get_effective_workflow_run_max_elapsed_time_minutes(None) == WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES
    assert get_effective_workflow_run_max_elapsed_time_minutes(0) == WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES
    assert get_effective_workflow_run_max_elapsed_time_minutes(-1) == WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES
    assert get_effective_workflow_run_max_elapsed_time_minutes(cast(int, "bad")) == (
        WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES
    )
    assert get_effective_workflow_run_max_elapsed_time_minutes(300) == 300
    assert get_effective_workflow_run_max_elapsed_time_minutes(600) == WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES


def test_elapsed_timeout_failure_reason_falls_back_when_invariant_is_missing() -> None:
    assert (
        service_module._require_elapsed_timeout_failure_reason(None)
        == "Workflow run exceeded max elapsed runtime limit."
    )


def test_workflow_run_elapsed_timeout_uses_platform_default_when_max_elapsed_time_is_none() -> None:
    workflow_run = cast(
        WorkflowRun,
        _workflow_run(
            WorkflowRunStatus.running,
            started_at=datetime.now(timezone.utc),
            max_elapsed_time_minutes=None,
        ),
    )

    timeout_seconds = service_module._get_workflow_run_max_elapsed_timeout_seconds(workflow_run)
    assert timeout_seconds is not None
    assert (WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES * 60) - 1 <= timeout_seconds
    assert timeout_seconds <= WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES * 60


def test_workflow_request_rejects_bool_max_elapsed_time() -> None:
    with pytest.warns(DeprecationWarning), pytest.raises(ValidationError):
        WorkflowRequestBody(max_elapsed_time_minutes=True)


def test_workflow_request_rejects_max_elapsed_time_above_platform_cap() -> None:
    with pytest.warns(DeprecationWarning), pytest.raises(ValidationError):
        WorkflowRequestBody(max_elapsed_time_minutes=WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES + 1)


def test_workflow_run_request_accepts_max_elapsed_time() -> None:
    request = WorkflowRunRequest(workflow_id="wpid_test", max_elapsed_time_minutes=300)

    assert request.max_elapsed_time_minutes == 300


def test_workflow_run_request_rejects_invalid_max_elapsed_time() -> None:
    with pytest.raises(ValidationError):
        WorkflowRunRequest(workflow_id="wpid_test", max_elapsed_time_minutes=0)

    with pytest.raises(ValidationError):
        WorkflowRunRequest(workflow_id="wpid_test", max_elapsed_time_minutes=True)

    with pytest.raises(ValidationError):
        WorkflowRunRequest(
            workflow_id="wpid_test",
            max_elapsed_time_minutes=WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES + 1,
        )


def test_public_workflow_run_request_preserves_max_elapsed_time_for_legacy_runner() -> None:
    request = WorkflowRunRequest(workflow_id="wpid_test", max_elapsed_time_minutes=10)

    with pytest.warns(DeprecationWarning):
        legacy_request = _workflow_run_request_to_legacy_request(request)

    assert legacy_request.max_elapsed_time_minutes == 10


def test_workflow_create_yaml_request_rejects_bool_max_elapsed_time() -> None:
    with pytest.raises(ValidationError):
        WorkflowCreateYAMLRequest(
            title="test",
            workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
            max_elapsed_time_minutes=True,
        )


def test_workflow_create_yaml_request_rejects_max_elapsed_time_above_platform_cap() -> None:
    with pytest.raises(ValidationError):
        WorkflowCreateYAMLRequest(
            title="test",
            workflow_definition=WorkflowDefinitionYAML(parameters=[], blocks=[]),
            max_elapsed_time_minutes=WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES + 1,
        )


def test_workflow_domain_models_accept_long_elapsed_timeout_values() -> None:
    now = datetime.now(timezone.utc)
    max_elapsed_time_minutes = 300

    workflow = Workflow(
        workflow_id="wf_1",
        organization_id="org_1",
        title="test",
        workflow_permanent_id="wp_1",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
        max_elapsed_time_minutes=max_elapsed_time_minutes,
        created_at=now,
        modified_at=now,
    )
    workflow_run = WorkflowRun(
        workflow_run_id="wr_1",
        workflow_id="wf_1",
        workflow_permanent_id="wp_1",
        organization_id="org_1",
        status=WorkflowRunStatus.running,
        max_elapsed_time_minutes=max_elapsed_time_minutes,
        created_at=now,
        modified_at=now,
    )

    assert workflow.max_elapsed_time_minutes == max_elapsed_time_minutes
    assert workflow_run.max_elapsed_time_minutes == max_elapsed_time_minutes


@pytest.mark.asyncio
async def test_execute_workflow_returns_after_elapsed_timeout_without_finally(monkeypatch: pytest.MonkeyPatch) -> None:
    started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    created_run = _workflow_run(WorkflowRunStatus.created, started_at=started_at)
    running_run = _workflow_run(WorkflowRunStatus.running, started_at=started_at)
    timed_out_run = _workflow_run(WorkflowRunStatus.timed_out, started_at=started_at)
    timed_out_run.failure_reason = "Workflow run exceeded max elapsed runtime limit of 1 minute."

    workflow = SimpleNamespace(
        workflow_id="wf_1",
        persist_browser_session=False,
        workflow_permanent_id="wp_1",
        title="Timeout workflow",
        organization_id="org_1",
        generate_script_on_terminal=False,
        model=None,
        workflow_definition=SimpleNamespace(
            parameters=[],
            finally_block_label="cleanup",
            blocks=[],
        ),
    )
    organization = SimpleNamespace(organization_id="org_1")

    workflow_context_manager = SimpleNamespace(
        initialize_workflow_run_context=AsyncMock(),
        get_workflow_run_context=lambda _workflow_run_id: SimpleNamespace(browser_session_id=None),
    )
    monkeypatch.setattr(service_module.app, "WORKFLOW_CONTEXT_MANAGER", workflow_context_manager)
    monkeypatch.setattr(service_module.workflow_script_service, "workflow_has_conditionals", lambda _workflow: False)
    monkeypatch.setattr(
        service_module.workflow_script_service,
        "get_workflow_script",
        AsyncMock(return_value=(None, None, False)),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)

    svc = WorkflowService()
    mark_workflow_run_as_timed_out = AsyncMock(return_value=timed_out_run)
    execute_workflow_blocks = AsyncMock()
    generate_script_if_needed = AsyncMock()
    execute_finally_block_if_configured = AsyncMock()
    clean_up_workflow = AsyncMock()

    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(svc, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(svc, "mark_workflow_run_as_running", AsyncMock(return_value=running_run))
    monkeypatch.setattr(svc, "get_workflow_run_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_workflow_output_parameters", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_collect_inherited_workflow_system_prompt", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "auto_create_browser_session_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_browser_profile_is_managed", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "mark_workflow_run_as_timed_out", mark_workflow_run_as_timed_out)
    monkeypatch.setattr(svc, "_execute_workflow_blocks", execute_workflow_blocks)
    monkeypatch.setattr(svc, "generate_script_if_needed", generate_script_if_needed)
    monkeypatch.setattr(svc, "_execute_finally_block_if_configured", execute_finally_block_if_configured)
    monkeypatch.setattr(svc, "clean_up_workflow", clean_up_workflow)

    result = await svc.execute_workflow(
        workflow_run_id="wr_1",
        api_key=None,
        organization=organization,
    )

    assert result is timed_out_run
    mark_workflow_run_as_timed_out.assert_awaited_once()
    assert mark_workflow_run_as_timed_out.await_args is not None
    assert (
        mark_workflow_run_as_timed_out.await_args.kwargs["failure_reason"]
        == "Workflow run exceeded max elapsed runtime limit of 1 minute."
    )
    execute_workflow_blocks.assert_not_awaited()
    generate_script_if_needed.assert_not_awaited()
    execute_finally_block_if_configured.assert_not_awaited()
    clean_up_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_workflow_times_out_slow_pre_block_script_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    started_at = datetime.now(timezone.utc)
    created_run = _workflow_run(WorkflowRunStatus.created, started_at=started_at)
    running_run = _workflow_run(WorkflowRunStatus.running, started_at=started_at)
    timed_out_run = _workflow_run(WorkflowRunStatus.timed_out, started_at=started_at)
    timed_out_run.failure_reason = "Workflow run exceeded max elapsed runtime limit of 1 minute."

    workflow = SimpleNamespace(
        workflow_id="wf_1",
        persist_browser_session=False,
        workflow_permanent_id="wp_1",
        title="Timeout workflow",
        organization_id="org_1",
        generate_script_on_terminal=False,
        model=None,
        workflow_definition=SimpleNamespace(
            parameters=[],
            finally_block_label="cleanup",
            blocks=[],
        ),
    )
    organization = SimpleNamespace(organization_id="org_1")

    workflow_context_manager = SimpleNamespace(
        initialize_workflow_run_context=AsyncMock(),
        get_workflow_run_context=lambda _workflow_run_id: SimpleNamespace(browser_session_id=None),
    )

    async def slow_script_lookup(*_args: object, **_kwargs: object) -> tuple[None, None, bool]:
        await asyncio.sleep(0.05)
        return None, None, False

    monkeypatch.setattr(service_module.app, "WORKFLOW_CONTEXT_MANAGER", workflow_context_manager)
    monkeypatch.setattr(
        service_module.workflow_script_service,
        "get_workflow_script",
        AsyncMock(side_effect=slow_script_lookup),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(service_module, "_get_workflow_run_max_elapsed_timeout_seconds", lambda _workflow_run: 0.01)

    svc = WorkflowService()
    mark_workflow_run_as_timed_out = AsyncMock(return_value=timed_out_run)
    execute_workflow_blocks = AsyncMock()
    clean_up_workflow = AsyncMock()

    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(svc, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(svc, "mark_workflow_run_as_running", AsyncMock(return_value=running_run))
    monkeypatch.setattr(svc, "get_workflow_run_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_workflow_output_parameters", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_collect_inherited_workflow_system_prompt", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "auto_create_browser_session_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_browser_profile_is_managed", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "mark_workflow_run_as_timed_out", mark_workflow_run_as_timed_out)
    monkeypatch.setattr(svc, "_execute_workflow_blocks", execute_workflow_blocks)
    monkeypatch.setattr(svc, "clean_up_workflow", clean_up_workflow)

    result = await svc.execute_workflow(
        workflow_run_id="wr_1",
        api_key=None,
        organization=organization,
    )

    assert result is timed_out_run
    mark_workflow_run_as_timed_out.assert_awaited_once()
    execute_workflow_blocks.assert_not_awaited()
    clean_up_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_workflow_preserves_completed_status_after_post_run_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = datetime.now(timezone.utc)
    created_run = _workflow_run(WorkflowRunStatus.created, started_at=started_at)
    running_run = _workflow_run(WorkflowRunStatus.running, started_at=started_at)
    completed_run = _workflow_run(WorkflowRunStatus.completed, started_at=started_at)
    timed_out_run = _workflow_run(WorkflowRunStatus.timed_out, started_at=started_at)

    workflow = SimpleNamespace(
        workflow_id="wf_1",
        persist_browser_session=False,
        workflow_permanent_id="wp_1",
        title="Timeout workflow",
        organization_id="org_1",
        generate_script_on_terminal=False,
        model=None,
        workflow_definition=SimpleNamespace(
            parameters=[],
            finally_block_label="cleanup",
            blocks=[],
        ),
    )
    organization = SimpleNamespace(organization_id="org_1")

    workflow_context_manager = SimpleNamespace(
        initialize_workflow_run_context=AsyncMock(),
        get_workflow_run_context=lambda _workflow_run_id: SimpleNamespace(browser_session_id=None),
    )
    database = SimpleNamespace(
        workflow_runs=SimpleNamespace(
            get_workflow_run=AsyncMock(return_value=completed_run),
        ),
    )
    timeout_seconds = iter([10.0, 0.01])

    monkeypatch.setattr(service_module.app, "WORKFLOW_CONTEXT_MANAGER", workflow_context_manager)
    monkeypatch.setattr(service_module.app, "DATABASE", database)
    monkeypatch.setattr(service_module.workflow_script_service, "workflow_has_conditionals", lambda _workflow: False)
    monkeypatch.setattr(
        service_module.workflow_script_service,
        "get_workflow_script",
        AsyncMock(return_value=(None, None, False)),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(service_module, "is_adaptive_caching", lambda _workflow, _workflow_run: False)
    monkeypatch.setattr(
        service_module,
        "_get_workflow_run_max_elapsed_timeout_seconds",
        lambda _workflow_run: next(timeout_seconds),
    )

    async def slow_finally(**_kwargs: object) -> None:
        await asyncio.sleep(0.05)

    svc = WorkflowService()
    mark_workflow_run_as_timed_out = AsyncMock(return_value=timed_out_run)
    execute_finally_block_if_configured = AsyncMock(side_effect=slow_finally)
    finalize_workflow_run_status = AsyncMock(return_value=completed_run)
    clean_up_workflow = AsyncMock()

    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(svc, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(svc, "mark_workflow_run_as_running", AsyncMock(return_value=running_run))
    monkeypatch.setattr(svc, "get_workflow_run_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_workflow_output_parameters", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_collect_inherited_workflow_system_prompt", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "auto_create_browser_session_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_browser_profile_is_managed", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "mark_workflow_run_as_timed_out", mark_workflow_run_as_timed_out)
    monkeypatch.setattr(svc, "_execute_workflow_blocks", AsyncMock(return_value=(completed_run, set())))
    monkeypatch.setattr(svc, "generate_script_if_needed", AsyncMock(return_value=running_run))
    monkeypatch.setattr(svc, "should_run_script", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_execute_finally_block_if_configured", execute_finally_block_if_configured)
    monkeypatch.setattr(svc, "_finalize_workflow_run_status", finalize_workflow_run_status)
    monkeypatch.setattr(svc, "clean_up_workflow", clean_up_workflow)

    result = await svc.execute_workflow(
        workflow_run_id="wr_1",
        api_key=None,
        organization=organization,
    )

    assert result is completed_run
    execute_finally_block_if_configured.assert_awaited_once()
    mark_workflow_run_as_timed_out.assert_not_awaited()
    finalize_workflow_run_status.assert_awaited_once()
    clean_up_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_workflow_preserves_timed_out_status_after_non_terminal_post_run_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = datetime.now(timezone.utc)
    created_run = _workflow_run(WorkflowRunStatus.created, started_at=started_at)
    running_run = _workflow_run(WorkflowRunStatus.running, started_at=started_at)
    timed_out_run = _workflow_run(WorkflowRunStatus.timed_out, started_at=started_at)
    timed_out_run.failure_reason = "Workflow run exceeded max elapsed runtime limit of 1 minute."

    workflow = SimpleNamespace(
        workflow_id="wf_1",
        persist_browser_session=False,
        workflow_permanent_id="wp_1",
        title="Timeout workflow",
        organization_id="org_1",
        generate_script_on_terminal=False,
        model=None,
        workflow_definition=SimpleNamespace(
            parameters=[],
            finally_block_label="cleanup",
            blocks=[],
        ),
    )
    organization = SimpleNamespace(organization_id="org_1")

    workflow_context_manager = SimpleNamespace(
        initialize_workflow_run_context=AsyncMock(),
        get_workflow_run_context=lambda _workflow_run_id: SimpleNamespace(browser_session_id=None),
    )
    database = SimpleNamespace(
        workflow_runs=SimpleNamespace(
            get_workflow_run=AsyncMock(return_value=running_run),
        ),
    )
    timeout_seconds = iter([10.0, 0.01])

    monkeypatch.setattr(service_module.app, "WORKFLOW_CONTEXT_MANAGER", workflow_context_manager)
    monkeypatch.setattr(service_module.app, "DATABASE", database)
    monkeypatch.setattr(service_module.workflow_script_service, "workflow_has_conditionals", lambda _workflow: False)
    monkeypatch.setattr(
        service_module.workflow_script_service,
        "get_workflow_script",
        AsyncMock(return_value=(None, None, False)),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(service_module, "is_adaptive_caching", lambda _workflow, _workflow_run: False)
    monkeypatch.setattr(
        service_module,
        "_get_workflow_run_max_elapsed_timeout_seconds",
        lambda _workflow_run: next(timeout_seconds),
    )

    async def slow_finally(**_kwargs: object) -> None:
        await asyncio.sleep(0.05)

    svc = WorkflowService()
    mark_workflow_run_as_timed_out = AsyncMock(return_value=timed_out_run)
    execute_finally_block_if_configured = AsyncMock(side_effect=slow_finally)
    finalize_workflow_run_status = AsyncMock(return_value=timed_out_run)
    clean_up_workflow = AsyncMock()

    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(svc, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(svc, "mark_workflow_run_as_running", AsyncMock(return_value=running_run))
    monkeypatch.setattr(svc, "get_workflow_run_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_workflow_output_parameters", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_collect_inherited_workflow_system_prompt", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "auto_create_browser_session_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_browser_profile_is_managed", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "mark_workflow_run_as_timed_out", mark_workflow_run_as_timed_out)
    monkeypatch.setattr(svc, "_execute_workflow_blocks", AsyncMock(return_value=(running_run, set())))
    monkeypatch.setattr(svc, "generate_script_if_needed", AsyncMock(return_value=running_run))
    monkeypatch.setattr(svc, "should_run_script", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_execute_finally_block_if_configured", execute_finally_block_if_configured)
    monkeypatch.setattr(svc, "_finalize_workflow_run_status", finalize_workflow_run_status)
    monkeypatch.setattr(svc, "clean_up_workflow", clean_up_workflow)

    result = await svc.execute_workflow(
        workflow_run_id="wr_1",
        api_key=None,
        organization=organization,
    )

    assert result is timed_out_run
    mark_workflow_run_as_timed_out.assert_awaited_once()
    execute_finally_block_if_configured.assert_awaited_once()
    finalize_workflow_run_status.assert_awaited_once()
    assert finalize_workflow_run_status.await_args is not None
    assert finalize_workflow_run_status.await_args.kwargs["pre_finally_status"] == WorkflowRunStatus.timed_out
    assert (
        finalize_workflow_run_status.await_args.kwargs["pre_finally_failure_reason"]
        == "Workflow run exceeded max elapsed runtime limit of 1 minute."
    )
    clean_up_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_workflow_marks_timed_out_when_post_run_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = datetime.now(timezone.utc)
    created_run = _workflow_run(WorkflowRunStatus.created, started_at=started_at)
    running_run = _workflow_run(WorkflowRunStatus.running, started_at=started_at)
    timed_out_run = _workflow_run(WorkflowRunStatus.timed_out, started_at=started_at)
    timed_out_run.failure_reason = "Workflow run exceeded max elapsed runtime limit of 1 minute."

    workflow = SimpleNamespace(
        workflow_id="wf_1",
        persist_browser_session=False,
        workflow_permanent_id="wp_1",
        title="Timeout workflow",
        organization_id="org_1",
        generate_script_on_terminal=False,
        model=None,
        workflow_definition=SimpleNamespace(
            parameters=[],
            finally_block_label="cleanup",
            blocks=[],
        ),
    )
    organization = SimpleNamespace(organization_id="org_1")

    workflow_context_manager = SimpleNamespace(
        initialize_workflow_run_context=AsyncMock(),
        get_workflow_run_context=lambda _workflow_run_id: SimpleNamespace(browser_session_id=None),
    )
    database = SimpleNamespace(
        workflow_runs=SimpleNamespace(
            get_workflow_run=AsyncMock(return_value=running_run),
        ),
    )
    timeout_seconds = iter([10.0, 0.0001])

    monkeypatch.setattr(service_module.app, "WORKFLOW_CONTEXT_MANAGER", workflow_context_manager)
    monkeypatch.setattr(service_module.app, "DATABASE", database)
    monkeypatch.setattr(service_module.workflow_script_service, "workflow_has_conditionals", lambda _workflow: False)
    monkeypatch.setattr(
        service_module.workflow_script_service,
        "get_workflow_script",
        AsyncMock(return_value=(None, None, False)),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(service_module, "is_adaptive_caching", lambda _workflow, _workflow_run: False)
    monkeypatch.setattr(
        service_module,
        "_get_workflow_run_max_elapsed_timeout_seconds",
        lambda _workflow_run: next(timeout_seconds),
    )

    svc = WorkflowService()
    mark_workflow_run_as_timed_out = AsyncMock(return_value=timed_out_run)
    generate_script_if_needed = AsyncMock()
    execute_finally_block_if_configured = AsyncMock()
    finalize_workflow_run_status = AsyncMock(return_value=timed_out_run)
    clean_up_workflow = AsyncMock()

    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(svc, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(svc, "mark_workflow_run_as_running", AsyncMock(return_value=running_run))
    monkeypatch.setattr(svc, "get_workflow_run_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_workflow_output_parameters", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_collect_inherited_workflow_system_prompt", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "auto_create_browser_session_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_browser_profile_is_managed", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "mark_workflow_run_as_timed_out", mark_workflow_run_as_timed_out)
    monkeypatch.setattr(svc, "_execute_workflow_blocks", AsyncMock(return_value=(running_run, set())))
    monkeypatch.setattr(svc, "generate_script_if_needed", generate_script_if_needed)
    monkeypatch.setattr(svc, "should_run_script", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_execute_finally_block_if_configured", execute_finally_block_if_configured)
    monkeypatch.setattr(svc, "_finalize_workflow_run_status", finalize_workflow_run_status)
    monkeypatch.setattr(svc, "clean_up_workflow", clean_up_workflow)

    result = await svc.execute_workflow(
        workflow_run_id="wr_1",
        api_key=None,
        organization=organization,
    )

    assert result is timed_out_run
    mark_workflow_run_as_timed_out.assert_awaited_once()
    generate_script_if_needed.assert_not_awaited()
    execute_finally_block_if_configured.assert_not_awaited()
    finalize_workflow_run_status.assert_awaited_once()
    assert finalize_workflow_run_status.await_args is not None
    assert finalize_workflow_run_status.await_args.kwargs["pre_finally_status"] == WorkflowRunStatus.timed_out
    clean_up_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_shield_post_run_elapsed_timeout_waits_for_status_write_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = datetime.now(timezone.utc)
    running_run = _workflow_run(WorkflowRunStatus.running, started_at=started_at)
    timed_out_run = _workflow_run(WorkflowRunStatus.timed_out, started_at=started_at)
    outer_task = asyncio.current_task()
    assert outer_task is not None

    async def refresh_workflow_run(**_kwargs: object) -> SimpleNamespace:
        outer_task.cancel()
        await asyncio.sleep(0)
        return running_run

    database = SimpleNamespace(
        workflow_runs=SimpleNamespace(
            get_workflow_run=AsyncMock(side_effect=refresh_workflow_run),
        ),
    )
    monkeypatch.setattr(service_module.app, "DATABASE", database)

    svc = WorkflowService()
    mark_workflow_run_as_timed_out = AsyncMock(return_value=timed_out_run)
    monkeypatch.setattr(svc, "mark_workflow_run_as_timed_out", mark_workflow_run_as_timed_out)

    result_run, pre_finally_status, pre_finally_failure_reason = await svc._shield_post_run_elapsed_timeout(
        workflow_run_id="wr_1",
        organization_id="org_1",
        workflow_run=cast(WorkflowRun, running_run),
        pre_finally_status=None,
        pre_finally_failure_reason=None,
        timeout_failure_reason="timed out",
    )

    assert result_run is timed_out_run
    assert pre_finally_status == WorkflowRunStatus.timed_out
    assert pre_finally_failure_reason == "timed out"
    mark_workflow_run_as_timed_out.assert_awaited_once_with(
        workflow_run_id="wr_1",
        failure_reason="timed out",
    )


@pytest.mark.asyncio
async def test_shield_post_run_elapsed_timeout_falls_back_when_handler_fails_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = datetime.now(timezone.utc)
    running_run = _workflow_run(WorkflowRunStatus.running, started_at=started_at)
    timed_out_run = _workflow_run(WorkflowRunStatus.timed_out, started_at=started_at)
    outer_task = asyncio.current_task()
    assert outer_task is not None

    async def fail_after_cancellation(**_kwargs: object) -> WorkflowRun:
        outer_task.cancel()
        await asyncio.sleep(0)
        raise RuntimeError("status write failed")

    svc = WorkflowService()
    mark_workflow_run_as_timed_out = AsyncMock(return_value=timed_out_run)
    monkeypatch.setattr(svc, "_handle_post_run_elapsed_timeout", fail_after_cancellation)
    monkeypatch.setattr(svc, "mark_workflow_run_as_timed_out", mark_workflow_run_as_timed_out)

    result_run, pre_finally_status, pre_finally_failure_reason = await svc._shield_post_run_elapsed_timeout(
        workflow_run_id="wr_1",
        organization_id="org_1",
        workflow_run=cast(WorkflowRun, running_run),
        pre_finally_status=None,
        pre_finally_failure_reason=None,
        timeout_failure_reason="timed out",
    )

    assert result_run is timed_out_run
    assert pre_finally_status == WorkflowRunStatus.timed_out
    assert pre_finally_failure_reason == "timed out"
    mark_workflow_run_as_timed_out.assert_awaited_once_with(
        workflow_run_id="wr_1",
        failure_reason="timed out",
    )


@pytest.mark.asyncio
async def test_execute_workflow_refreshes_terminal_status_after_immediate_post_run_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = datetime.now(timezone.utc)
    created_run = _workflow_run(WorkflowRunStatus.created, started_at=started_at)
    running_run = _workflow_run(WorkflowRunStatus.running, started_at=started_at)
    completed_run = _workflow_run(WorkflowRunStatus.completed, started_at=started_at)
    timed_out_run = _workflow_run(WorkflowRunStatus.timed_out, started_at=started_at)

    workflow = SimpleNamespace(
        workflow_id="wf_1",
        persist_browser_session=False,
        workflow_permanent_id="wp_1",
        title="Timeout workflow",
        organization_id="org_1",
        generate_script_on_terminal=False,
        model=None,
        workflow_definition=SimpleNamespace(
            parameters=[],
            finally_block_label="cleanup",
            blocks=[],
        ),
    )
    organization = SimpleNamespace(organization_id="org_1")

    workflow_context_manager = SimpleNamespace(
        initialize_workflow_run_context=AsyncMock(),
        get_workflow_run_context=lambda _workflow_run_id: SimpleNamespace(browser_session_id=None),
    )

    async def refresh_workflow_run(**_kwargs: object) -> SimpleNamespace:
        await asyncio.sleep(0.01)
        return completed_run

    database = SimpleNamespace(
        workflow_runs=SimpleNamespace(
            get_workflow_run=AsyncMock(side_effect=refresh_workflow_run),
        ),
    )
    timeout_seconds = iter([10.0, 0.0])

    monkeypatch.setattr(service_module.app, "WORKFLOW_CONTEXT_MANAGER", workflow_context_manager)
    monkeypatch.setattr(service_module.app, "DATABASE", database)
    monkeypatch.setattr(service_module.workflow_script_service, "workflow_has_conditionals", lambda _workflow: False)
    monkeypatch.setattr(
        service_module.workflow_script_service,
        "get_workflow_script",
        AsyncMock(return_value=(None, None, False)),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(service_module, "is_adaptive_caching", lambda _workflow, _workflow_run: False)
    monkeypatch.setattr(
        service_module,
        "_get_workflow_run_max_elapsed_timeout_seconds",
        lambda _workflow_run: next(timeout_seconds),
    )

    svc = WorkflowService()
    mark_workflow_run_as_timed_out = AsyncMock(return_value=timed_out_run)
    finalize_workflow_run_status = AsyncMock(return_value=completed_run)
    clean_up_workflow = AsyncMock()

    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(svc, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(svc, "mark_workflow_run_as_running", AsyncMock(return_value=running_run))
    monkeypatch.setattr(svc, "get_workflow_run_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_workflow_output_parameters", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_collect_inherited_workflow_system_prompt", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "auto_create_browser_session_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_browser_profile_is_managed", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "mark_workflow_run_as_timed_out", mark_workflow_run_as_timed_out)
    monkeypatch.setattr(svc, "_execute_workflow_blocks", AsyncMock(return_value=(completed_run, set())))
    monkeypatch.setattr(svc, "generate_script_if_needed", AsyncMock())
    monkeypatch.setattr(svc, "should_run_script", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_execute_finally_block_if_configured", AsyncMock())
    monkeypatch.setattr(svc, "_finalize_workflow_run_status", finalize_workflow_run_status)
    monkeypatch.setattr(svc, "clean_up_workflow", clean_up_workflow)

    result = await svc.execute_workflow(
        workflow_run_id="wr_1",
        api_key=None,
        organization=organization,
    )

    assert result is completed_run
    mark_workflow_run_as_timed_out.assert_not_awaited()
    finalize_workflow_run_status.assert_awaited_once()
    clean_up_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_workflow_returns_finalized_status_after_post_run_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = datetime.now(timezone.utc)
    created_run = _workflow_run(WorkflowRunStatus.created, started_at=started_at)
    running_run = _workflow_run(WorkflowRunStatus.running, started_at=started_at)
    failed_run = _workflow_run(WorkflowRunStatus.failed, started_at=started_at)
    timed_out_run = _workflow_run(WorkflowRunStatus.timed_out, started_at=started_at)

    workflow = SimpleNamespace(
        workflow_id="wf_1",
        persist_browser_session=False,
        workflow_permanent_id="wp_1",
        title="Timeout workflow",
        organization_id="org_1",
        generate_script_on_terminal=False,
        model=None,
        workflow_definition=SimpleNamespace(
            parameters=[],
            finally_block_label="cleanup",
            blocks=[],
        ),
    )
    organization = SimpleNamespace(organization_id="org_1")

    workflow_context_manager = SimpleNamespace(
        initialize_workflow_run_context=AsyncMock(),
        get_workflow_run_context=lambda _workflow_run_id: SimpleNamespace(browser_session_id=None),
    )
    database = SimpleNamespace(
        workflow_runs=SimpleNamespace(
            get_workflow_run=AsyncMock(return_value=failed_run),
        ),
    )
    timeout_seconds = iter([10.0, 0.01])

    monkeypatch.setattr(service_module.app, "WORKFLOW_CONTEXT_MANAGER", workflow_context_manager)
    monkeypatch.setattr(service_module.app, "DATABASE", database)
    monkeypatch.setattr(service_module.workflow_script_service, "workflow_has_conditionals", lambda _workflow: False)
    monkeypatch.setattr(
        service_module.workflow_script_service,
        "get_workflow_script",
        AsyncMock(return_value=(None, None, False)),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(service_module, "is_adaptive_caching", lambda _workflow, _workflow_run: False)
    monkeypatch.setattr(
        service_module,
        "_get_workflow_run_max_elapsed_timeout_seconds",
        lambda _workflow_run: next(timeout_seconds),
    )

    async def slow_finally(**_kwargs: object) -> None:
        await asyncio.sleep(0.05)

    svc = WorkflowService()
    mark_workflow_run_as_timed_out = AsyncMock(return_value=timed_out_run)
    update_workflow_run_status = AsyncMock(return_value=running_run)
    execute_finally_block_if_configured = AsyncMock(side_effect=slow_finally)
    finalize_workflow_run_status = AsyncMock(return_value=failed_run)
    clean_up_workflow = AsyncMock()

    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(svc, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(svc, "mark_workflow_run_as_running", AsyncMock(return_value=running_run))
    monkeypatch.setattr(svc, "get_workflow_run_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_workflow_output_parameters", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_collect_inherited_workflow_system_prompt", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "auto_create_browser_session_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_browser_profile_is_managed", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "mark_workflow_run_as_timed_out", mark_workflow_run_as_timed_out)
    monkeypatch.setattr(svc, "_execute_workflow_blocks", AsyncMock(return_value=(failed_run, set())))
    monkeypatch.setattr(svc, "generate_script_if_needed", AsyncMock())
    monkeypatch.setattr(svc, "should_run_script", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_update_workflow_run_status", update_workflow_run_status)
    monkeypatch.setattr(svc, "_execute_finally_block_if_configured", execute_finally_block_if_configured)
    monkeypatch.setattr(svc, "_finalize_workflow_run_status", finalize_workflow_run_status)
    monkeypatch.setattr(svc, "clean_up_workflow", clean_up_workflow)

    result = await svc.execute_workflow(
        workflow_run_id="wr_1",
        api_key=None,
        organization=organization,
    )

    assert result is failed_run
    mark_workflow_run_as_timed_out.assert_not_awaited()
    update_workflow_run_status.assert_awaited_once_with(
        workflow_run_id="wr_1",
        status=WorkflowRunStatus.running,
        failure_reason=None,
    )
    execute_finally_block_if_configured.assert_awaited_once()
    finalize_workflow_run_status.assert_awaited_once()
    clean_up_workflow.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_workflow_runs_finally_for_existing_timed_out_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = datetime.now(timezone.utc)
    created_run = _workflow_run(WorkflowRunStatus.created, started_at=started_at)
    running_run = _workflow_run(WorkflowRunStatus.running, started_at=started_at)
    timed_out_run = _workflow_run(WorkflowRunStatus.timed_out, started_at=started_at)

    workflow = SimpleNamespace(
        workflow_id="wf_1",
        persist_browser_session=False,
        workflow_permanent_id="wp_1",
        title="Timeout workflow",
        organization_id="org_1",
        generate_script_on_terminal=False,
        model=None,
        workflow_definition=SimpleNamespace(
            parameters=[],
            finally_block_label="cleanup",
            blocks=[],
        ),
    )
    organization = SimpleNamespace(organization_id="org_1")

    workflow_context_manager = SimpleNamespace(
        initialize_workflow_run_context=AsyncMock(),
        get_workflow_run_context=lambda _workflow_run_id: SimpleNamespace(browser_session_id=None),
    )
    database = SimpleNamespace(
        workflow_runs=SimpleNamespace(
            get_workflow_run=AsyncMock(return_value=timed_out_run),
        ),
    )

    monkeypatch.setattr(service_module.app, "WORKFLOW_CONTEXT_MANAGER", workflow_context_manager)
    monkeypatch.setattr(service_module.app, "DATABASE", database)
    monkeypatch.setattr(service_module.workflow_script_service, "workflow_has_conditionals", lambda _workflow: False)
    monkeypatch.setattr(
        service_module.workflow_script_service,
        "get_workflow_script",
        AsyncMock(return_value=(None, None, False)),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(service_module, "is_adaptive_caching", lambda _workflow, _workflow_run: False)
    monkeypatch.setattr(service_module, "_get_workflow_run_max_elapsed_timeout_seconds", lambda _workflow_run: 10.0)

    svc = WorkflowService()
    mark_workflow_run_as_timed_out = AsyncMock(return_value=timed_out_run)
    update_workflow_run_status = AsyncMock(return_value=running_run)
    execute_finally_block_if_configured = AsyncMock()
    finalize_workflow_run_status = AsyncMock(return_value=timed_out_run)
    clean_up_workflow = AsyncMock()

    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(svc, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(svc, "mark_workflow_run_as_running", AsyncMock(return_value=running_run))
    monkeypatch.setattr(svc, "get_workflow_run_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_workflow_output_parameters", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_collect_inherited_workflow_system_prompt", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "auto_create_browser_session_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_browser_profile_is_managed", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "mark_workflow_run_as_timed_out", mark_workflow_run_as_timed_out)
    monkeypatch.setattr(svc, "_execute_workflow_blocks", AsyncMock(return_value=(timed_out_run, set())))
    monkeypatch.setattr(svc, "generate_script_if_needed", AsyncMock())
    monkeypatch.setattr(svc, "should_run_script", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_update_workflow_run_status", update_workflow_run_status)
    monkeypatch.setattr(svc, "_execute_finally_block_if_configured", execute_finally_block_if_configured)
    monkeypatch.setattr(svc, "_finalize_workflow_run_status", finalize_workflow_run_status)
    monkeypatch.setattr(svc, "clean_up_workflow", clean_up_workflow)

    result = await svc.execute_workflow(
        workflow_run_id="wr_1",
        api_key=None,
        organization=organization,
    )

    assert result is timed_out_run
    mark_workflow_run_as_timed_out.assert_not_awaited()
    update_workflow_run_status.assert_awaited_once_with(
        workflow_run_id="wr_1",
        status=WorkflowRunStatus.running,
        failure_reason=None,
    )
    execute_finally_block_if_configured.assert_awaited_once()
    finalize_workflow_run_status.assert_awaited_once()
    clean_up_workflow.assert_awaited_once()
