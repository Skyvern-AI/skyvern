"""The post-block completion check is a second multimodal call; gating it (flag-on) skips it after
navigate blocks, which cannot satisfy an information subgoal. Default-off preserves current behavior.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.config import Settings
from skyvern.forge.sdk.prompting import PromptEngine
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.services import planner_levers, task_v2_service
from skyvern.services.task_v2_service import _should_run_post_block_completion_check


class _TaskV2WiringObserved(Exception):
    pass


async def _run_task_v2_wiring_case(
    monkeypatch: pytest.MonkeyPatch,
    iterations: list[int],
    *,
    shared_flag_enabled: bool,
    planner_response: dict[str, Any] | None = None,
    max_iterations: int | None = None,
) -> tuple[list[dict[str, Any]], AsyncMock]:
    completion_case = len(iterations) == 1
    organization = SimpleNamespace(organization_id="org_test", max_steps_per_run=None)
    task_v2 = MagicMock(observer_cruise_id="t", status=TaskV2Status.queued, prompt="goal")
    task_v2.url, task_v2.workflow_run_id = "https://example.test", "wr_test"
    queued_run = MagicMock(workflow_run_id="wr_test", workflow_id="wf_test", status=WorkflowRunStatus.queued)
    running_run = MagicMock(workflow_run_id="wr_test", workflow_id="wf_test", status=WorkflowRunStatus.running)
    workflow = MagicMock(workflow_id="wf_test", workflow_permanent_id="wp_test", title="Test")
    block_result = MagicMock(failure_reason=None, output_parameter_value={}, success=completion_case)
    block = SimpleNamespace(execute_safe=AsyncMock(return_value=block_result))
    scraped_page = SimpleNamespace(screenshots=[])
    browser_state = MagicMock(engine_selection=None)
    browser_state.get_working_page = AsyncMock(return_value=MagicMock())
    browser_state.validate_browser_context = AsyncMock(return_value=True)
    browser_state.scrape_website = AsyncMock(return_value=scraped_page)
    test_app = MagicMock()
    test_app.scrape_exclude = []
    test_app.WORKFLOW_SERVICE.get_workflow_run = AsyncMock(side_effect=[queued_run, *[running_run for _ in iterations]])
    test_app.WORKFLOW_SERVICE.get_workflow = AsyncMock(return_value=workflow)
    test_app.WORKFLOW_SERVICE.mark_workflow_run_as_running = AsyncMock()
    test_app.WORKFLOW_SERVICE.create_workflow_from_request = AsyncMock(return_value=workflow)
    test_app.BROWSER_MANAGER.get_or_create_for_workflow_run = AsyncMock(return_value=browser_state)
    test_app.AGENT_FUNCTION.has_code_block_access = AsyncMock(return_value=False)
    test_app.AGENT_FUNCTION.validate_task_execution = AsyncMock()
    test_app.AGENT_FUNCTION.cleanup_element_tree_factory = MagicMock(return_value=MagicMock())
    test_app.DATABASE.observer.update_task_v2 = AsyncMock(return_value=task_v2)
    test_app.DATABASE.observer.create_thought = AsyncMock(return_value=SimpleNamespace(observer_thought_id="thought"))
    test_app.DATABASE.observer.update_thought = AsyncMock()
    test_app.DATABASE.tasks.get_tasks_by_workflow_run_id = AsyncMock(
        side_effect=_TaskV2WiringObserved if completion_case else None,
        return_value=[],
    )
    test_app.DATABASE.tasks.get_total_unique_step_order_count_by_task_ids = AsyncMock(return_value=0)
    provider = AsyncMock(return_value=shared_flag_enabled)
    planner_inputs: list[dict[str, Any]] = []
    planner_response = planner_response or dict(
        user_goal_achieved=False, should_terminate=False, plan="continue", task_type="navigate"
    )
    llm_handler = AsyncMock(return_value=planner_response)

    def capture_planner_inputs(*_args: object, **kwargs: Any) -> str:
        planner_inputs.append(kwargs)
        if not completion_case and len(planner_inputs) == len(iterations):
            raise _TaskV2WiringObserved
        return "prompt"

    context = SimpleNamespace(run_id=None, root_workflow_run_id=None, tz_info=timezone.utc)
    monkeypatch.setattr(task_v2_service, "app", test_app)
    monkeypatch.setattr(planner_levers.app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)
    monkeypatch.setattr(planner_levers.settings, "TASK_V2_CONVERGE_PCT", 0)
    monkeypatch.setattr(planner_levers.settings, "TASK_V2_CARRY_SUBGOALS", False)
    monkeypatch.setattr(planner_levers.settings, "TASK_V2_SKIP_COMPLETION_CHECK_AFTER_NAVIGATE", False)
    monkeypatch.setattr(task_v2_service.skyvern_context, "ensure_context", lambda: context)
    monkeypatch.setattr(task_v2_service, "initialize_task_v2_metadata", AsyncMock(return_value=task_v2))
    monkeypatch.setattr(task_v2_service, "_set_up_workflow_context", AsyncMock())
    monkeypatch.setattr(task_v2_service, "_resolve_max_iterations", lambda _override: max_iterations or len(iterations))
    monkeypatch.setattr(task_v2_service, "range", lambda _max_iterations: iterations, raising=False)
    monkeypatch.setattr(
        task_v2_service,
        "_is_planner_mini_goal_improvements_enabled",
        AsyncMock(return_value=completion_case),
    )
    monkeypatch.setattr(task_v2_service, "build_open_tabs_context", AsyncMock(return_value=None))
    monkeypatch.setattr(task_v2_service, "load_prompt_with_elements", capture_planner_inputs)
    monkeypatch.setattr(task_v2_service, "_get_task_v2_llm_api_handler", lambda _task_v2: llm_handler)
    monkeypatch.setattr(task_v2_service.SkyvernFrame, "get_url", AsyncMock(return_value=task_v2.url))
    monkeypatch.setattr(task_v2_service, "_generate_navigation_task", AsyncMock(return_value=(block, [], [])))
    monkeypatch.setattr(task_v2_service, "WorkflowDefinitionYAML", MagicMock())
    monkeypatch.setattr(task_v2_service, "WorkflowCreateYAMLRequest", MagicMock())
    monkeypatch.setattr(task_v2_service, "runtime_proxy_location", lambda _location: None)
    monkeypatch.setattr(task_v2_service, "_get_extracted_data_from_block_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(task_v2_service, "handle_block_result", AsyncMock(return_value=running_run))

    with pytest.raises(_TaskV2WiringObserved):
        await task_v2_service.run_task_v2_helper(
            organization, task_v2, max_iterations_override=max_iterations or len(iterations)
        )

    return planner_inputs, llm_handler


def test_skip_after_navigate_ships_off() -> None:
    assert Settings.model_fields["TASK_V2_SKIP_COMPLETION_CHECK_AFTER_NAVIGATE"].default is False


def test_planner_mini_goal_improvements_levers_ship_off() -> None:
    assert Settings.model_fields["PLANNER_MINI_GOAL_IMPROVEMENTS"].default is False
    assert Settings.model_fields["RESET_BROWSER_TABS_BETWEEN_LOOP_ITERATIONS"].default is False


def test_task_v2_all_levers_off_render_matches_control() -> None:
    rendered = PromptEngine(model="skyvern").load_prompt(
        "task_v2",
        planner_mini_goal_improvements=False,
        prior_required_subgoals=None,
        iterations_remaining=None,
        compute_enabled=False,
        open_tabs_context=None,
    )

    assert (
        hashlib.sha256(rendered.encode()).hexdigest(),
        len(rendered.splitlines()),
        sum(not line.strip() for line in rendered.splitlines()),
    ) == (
        "d5d5343e2f87b34baed6d794b70220368686593f609eff888cd895e62c3fd7bb",
        108,
        25,
    )


def test_wrap_up_dropped_required_part_keeps_goal_unmet() -> None:
    rendered = PromptEngine(model="skyvern").load_prompt(
        "task_v2",
        planner_mini_goal_improvements=True,
        prior_required_subgoals=None,
        iterations_remaining=1,
        compute_enabled=False,
        open_tabs_context=None,
    )

    assert "leave it satisfied=false and keep user_goal_achieved=false" in rendered
    assert "every achievable required part" not in rendered


def test_prior_required_subgoals_instruction_points_to_fenced_data() -> None:
    rendered = PromptEngine(model="skyvern").load_prompt(
        "task_v2",
        planner_mini_goal_improvements=True,
        prior_required_subgoals=[
            {
                "subgoal": "Submit the form",
                "satisfied": True,
                "evidence": "Success message is visible",
            }
        ],
        iterations_remaining=None,
        compute_enabled=False,
        open_tabs_context=None,
    )

    data_index = rendered.index("Required subgoals carried forward from the previous planning step:")
    boundary_end_index = rendered.index("END_UNTRUSTED_WEB_PAGE_DATA")
    instruction = (
        "Refine the required_subgoals leg-checklist provided in the untrusted webpage-data block above "
        "instead of re-deriving it from scratch."
    )

    assert data_index < boundary_end_index < rendered.index(instruction)
    assert "only re-mark a part satisfied=false if new evidence shows it regressed." in rendered
    assert "only re-mark a part satisfied=false if new evidence shows it regressed:" not in rendered


@pytest.mark.asyncio
async def test_first_iteration_goto_url_has_no_completion_criterion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = SimpleNamespace(organization_id="org_test", max_steps_per_run=None)
    task_v2 = SimpleNamespace(
        observer_cruise_id="task_v2_test",
        status=TaskV2Status.queued,
        prompt="visit the page",
        url="https://example.test",
        workflow_run_id="workflow_run_test",
        max_screenshot_scrolls=None,
        proxy_location=None,
    )
    queued_run = SimpleNamespace(
        workflow_run_id="workflow_run_test",
        workflow_id="workflow_test",
        status=WorkflowRunStatus.queued,
        browser_profile_id=None,
    )
    running_run = SimpleNamespace(
        workflow_run_id="workflow_run_test",
        workflow_id="workflow_test",
        status=WorkflowRunStatus.running,
        browser_profile_id=None,
    )
    workflow = SimpleNamespace(
        workflow_id="workflow_test",
        workflow_permanent_id="workflow_permanent_test",
        title="Test workflow",
        description=None,
        status="published",
    )
    block_result = SimpleNamespace(
        status="failed",
        failure_reason=None,
        output_parameter_value=None,
        success=False,
    )
    block = SimpleNamespace(execute_safe=AsyncMock(return_value=block_result))
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=None))
    workflow_service = SimpleNamespace(
        get_workflow_run=AsyncMock(side_effect=[queued_run, running_run]),
        get_workflow=AsyncMock(return_value=workflow),
        mark_workflow_run_as_running=AsyncMock(),
        create_workflow_from_request=AsyncMock(return_value=workflow),
    )
    database = SimpleNamespace(
        observer=SimpleNamespace(update_task_v2=AsyncMock(return_value=task_v2)),
        tasks=SimpleNamespace(
            get_tasks_by_workflow_run_id=AsyncMock(return_value=[]),
            get_total_unique_step_order_count_by_task_ids=AsyncMock(return_value=0),
        ),
    )
    agent_function = SimpleNamespace(
        has_code_block_access=AsyncMock(return_value=False),
        validate_task_execution=AsyncMock(),
    )
    goto_task = AsyncMock(return_value=(block, [], []))
    context = SimpleNamespace(run_id=None, root_workflow_run_id=None, tz_info=timezone.utc)

    monkeypatch.setattr(task_v2_service.app, "WORKFLOW_SERVICE", workflow_service)
    monkeypatch.setattr(task_v2_service.app, "DATABASE", database)
    monkeypatch.setattr(task_v2_service.app, "AGENT_FUNCTION", agent_function)
    monkeypatch.setattr(
        task_v2_service.app,
        "BROWSER_MANAGER",
        SimpleNamespace(get_or_create_for_workflow_run=AsyncMock(return_value=browser_state)),
    )
    monkeypatch.setattr(task_v2_service.skyvern_context, "ensure_context", lambda: context)
    monkeypatch.setattr(
        task_v2_service,
        "initialize_task_v2_metadata",
        AsyncMock(return_value=task_v2),
    )
    monkeypatch.setattr(task_v2_service, "_set_up_workflow_context", AsyncMock())
    monkeypatch.setattr(task_v2_service, "_resolve_max_iterations", lambda _override: 1)
    monkeypatch.setattr(task_v2_service, "_generate_goto_url_task", goto_task)
    monkeypatch.setattr(task_v2_service, "WorkflowDefinitionYAML", MagicMock())
    monkeypatch.setattr(task_v2_service, "WorkflowCreateYAMLRequest", MagicMock())
    monkeypatch.setattr(task_v2_service, "runtime_proxy_location", lambda _location: None)
    monkeypatch.setattr(task_v2_service, "_get_extracted_data_from_block_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(task_v2_service, "handle_block_result", AsyncMock(return_value=running_run))
    monkeypatch.setattr(task_v2_service, "_should_run_post_block_completion_check", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(task_v2_service, "classify_from_failure_reason", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        task_v2_service,
        "_best_effort_failure_deliverable",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(task_v2_service, "mark_task_v2_as_failed", AsyncMock(return_value=task_v2))

    result = await task_v2_service.run_task_v2_helper(
        organization,
        task_v2,
        max_steps_override=1,
        max_iterations_override=1,
    )

    assert result == (workflow, running_run, task_v2)
    goto_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_task_type_failure_finalizes_with_organization_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    organization = SimpleNamespace(organization_id="org_test", max_steps_per_run=None)
    task_v2 = SimpleNamespace(
        observer_cruise_id="task_v2_test",
        status=TaskV2Status.queued,
        prompt="finish the task",
        url="https://example.test",
        workflow_run_id="workflow_run_test",
        max_screenshot_scrolls=None,
        proxy_location=None,
        workflow_system_prompt=None,
        started_at=now,
        created_at=now,
    )
    queued_run = SimpleNamespace(
        workflow_run_id="workflow_run_test",
        workflow_id="workflow_test",
        status=WorkflowRunStatus.queued,
        browser_profile_id=None,
    )
    running_run = SimpleNamespace(
        workflow_run_id="workflow_run_test",
        workflow_id="workflow_test",
        status=WorkflowRunStatus.running,
        browser_profile_id=None,
    )
    workflow = SimpleNamespace(
        workflow_id="workflow_test",
        workflow_permanent_id="workflow_permanent_test",
        title="Test workflow",
        description=None,
        status="published",
    )
    page = MagicMock()
    scraped_page = SimpleNamespace(screenshots=[])
    browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=page),
        validate_browser_context=AsyncMock(return_value=True),
        scrape_website=AsyncMock(return_value=scraped_page),
    )
    workflow_service = SimpleNamespace(
        get_workflow_run=AsyncMock(side_effect=[queued_run, running_run]),
        get_workflow=AsyncMock(return_value=workflow),
        mark_workflow_run_as_running=AsyncMock(),
        mark_workflow_run_as_failed=AsyncMock(),
    )

    async def update_task_v2(*_args: object, **kwargs: object) -> SimpleNamespace:
        if status := kwargs.get("status"):
            task_v2.status = status
        return task_v2

    observer = SimpleNamespace(
        update_task_v2=AsyncMock(side_effect=update_task_v2),
        create_thought=AsyncMock(return_value=SimpleNamespace(observer_thought_id="thought_test")),
        update_thought=AsyncMock(),
    )
    agent_function = SimpleNamespace(
        has_code_block_access=AsyncMock(return_value=False),
        validate_task_execution=AsyncMock(),
        cleanup_element_tree_factory=MagicMock(return_value=MagicMock()),
    )
    context = SimpleNamespace(run_id=None, root_workflow_run_id=None, tz_info=timezone.utc)
    llm_handler = AsyncMock(
        return_value={
            "user_goal_achieved": False,
            "should_terminate": False,
            "plan": "perform the next required action",
            "task_type": "",
        }
    )

    monkeypatch.setattr(task_v2_service.app, "WORKFLOW_SERVICE", workflow_service)
    monkeypatch.setattr(task_v2_service.app, "DATABASE", SimpleNamespace(observer=observer))
    monkeypatch.setattr(task_v2_service.app, "AGENT_FUNCTION", agent_function)
    monkeypatch.setattr(
        task_v2_service.app,
        "BROWSER_MANAGER",
        SimpleNamespace(get_or_create_for_workflow_run=AsyncMock(return_value=browser_state)),
    )
    monkeypatch.setattr(task_v2_service.skyvern_context, "ensure_context", lambda: context)
    monkeypatch.setattr(task_v2_service, "initialize_task_v2_metadata", AsyncMock(return_value=task_v2))
    monkeypatch.setattr(task_v2_service, "_set_up_workflow_context", AsyncMock())
    monkeypatch.setattr(task_v2_service, "_resolve_max_iterations", lambda _override: 1)
    monkeypatch.setattr(task_v2_service, "_is_planner_mini_goal_improvements_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(task_v2_service, "build_open_tabs_context", AsyncMock(return_value=None))
    monkeypatch.setattr(task_v2_service, "load_prompt_with_elements", MagicMock(return_value="prompt"))
    monkeypatch.setattr(task_v2_service, "_get_task_v2_llm_api_handler", lambda _task_v2: llm_handler)
    monkeypatch.setattr(task_v2_service.SkyvernFrame, "get_url", AsyncMock(return_value=task_v2.url))
    monkeypatch.setattr(task_v2_service, "send_task_v2_webhook", AsyncMock())

    result = await task_v2_service.run_task_v2_helper(
        organization,
        task_v2,
        max_iterations_override=1,
    )

    assert result == (workflow, running_run, task_v2)
    assert task_v2.status == TaskV2Status.failed
    failed_update = next(
        call for call in observer.update_task_v2.await_args_list if call.kwargs.get("status") == TaskV2Status.failed
    )
    assert failed_update.kwargs["organization_id"] == organization.organization_id
    workflow_service.mark_workflow_run_as_failed.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_enabled,settings_default,expected",
    [(True, False, True), (False, False, False), (False, True, True)],
)
async def test_planner_mini_goal_improvements_resolver_uses_provider_then_settings_fallback(
    provider_enabled: bool,
    settings_default: bool,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = AsyncMock(return_value=provider_enabled)
    monkeypatch.setattr(task_v2_service.app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", resolver)
    monkeypatch.setattr(task_v2_service.settings, "PLANNER_MINI_GOAL_IMPROVEMENTS", settings_default)

    assert await task_v2_service._is_planner_mini_goal_improvements_enabled("org_test") is expected
    resolver.assert_awaited_once_with(
        "PLANNER_MINI_GOAL_IMPROVEMENTS",
        "org_test",
        properties={"organization_id": "org_test"},
    )


@pytest.mark.asyncio
async def test_planner_mini_goal_improvements_resolver_falls_back_when_provider_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        task_v2_service.app.EXPERIMENTATION_PROVIDER,
        "is_feature_enabled_cached",
        AsyncMock(side_effect=RuntimeError("provider unavailable")),
    )
    monkeypatch.setattr(task_v2_service.settings, "PLANNER_MINI_GOAL_IMPROVEMENTS", True)

    assert await task_v2_service._is_planner_mini_goal_improvements_enabled("org_test") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("lever_enabled", [True, False])
async def test_completion_gate_resolver_controls_post_navigate_check(
    lever_enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, llm_handler = await _run_task_v2_wiring_case(
        monkeypatch,
        [0],
        shared_flag_enabled=lever_enabled,
        planner_response={
            "user_goal_achieved": False,
            "should_terminate": False,
            "plan": "continue",
            "task_type": "navigate",
            "complete_criterion": "the navigation succeeded",
        },
    )

    prompt_names = [call.kwargs["prompt_name"] for call in llm_handler.await_args_list]
    assert ("task_v2_check_completion" in prompt_names) is not lever_enabled


@pytest.mark.parametrize("task_type", ["navigate", "extract", "loop"])
def test_default_off_runs_check_for_every_block_type(task_type: str) -> None:
    assert (
        _should_run_post_block_completion_check(
            True,
            task_type,
            navigate_completion_check_enabled=True,
            skip_completion_check_after_navigate=False,
        )
        is True
    )


@pytest.mark.parametrize(
    "task_type,navigate_completion_check_enabled,expected",
    [
        ("navigate", True, False),
        ("navigate", False, True),
        ("extract", True, True),
        ("loop", True, True),
    ],
)
def test_flag_on_skips_only_navigate_with_embedded_check(
    task_type: str,
    navigate_completion_check_enabled: bool,
    expected: bool,
) -> None:
    assert (
        _should_run_post_block_completion_check(
            True,
            task_type,
            navigate_completion_check_enabled=navigate_completion_check_enabled,
            skip_completion_check_after_navigate=True,
        )
        is expected
    )


@pytest.mark.parametrize("block_success", [False, None])
def test_no_check_when_block_did_not_succeed(block_success: bool | None) -> None:
    assert (
        _should_run_post_block_completion_check(
            block_success,
            "extract",
            navigate_completion_check_enabled=False,
            skip_completion_check_after_navigate=True,
        )
        is False
    )
