from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from skyvern.forge.agent import ForgeAgent, SpeculativePlan
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.schemas.runs import RunEngine
from skyvern.schemas.steps import AgentStepOutput
from skyvern.webeye.actions.actions import ClickAction, CompleteAction, ExtractAction
from skyvern.webeye.actions.responses import ActionSuccess
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from tests.unit.helpers import (
    make_browser_state,
    make_organization,
    make_step,
    make_task,
    setup_parallel_verification_mocks,
)


@pytest.mark.asyncio
async def test_parallel_verification_triggers_data_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = ForgeAgent()
    now = datetime.now(UTC)

    organization = make_organization(now)
    task = make_task(now, organization)

    step_output = AgentStepOutput(action_results=[], actions_and_results=[])
    step = make_step(
        now,
        task,
        step_id="step-123",
        status=StepStatus.completed,
        order=0,
        output=step_output,
    )
    next_step = make_step(
        now,
        task,
        step_id="step-next",
        status=StepStatus.created,
        order=1,
        output=None,
    )

    complete_action = CompleteAction(reasoning="done", verified=True)
    extract_action = ExtractAction(
        reasoning="extract final data",
        data_extraction_goal=task.data_extraction_goal,
        data_extraction_schema=task.extracted_information_schema,
    )
    extract_action.organization_id = task.organization_id
    extract_action.workflow_run_id = task.workflow_run_id
    extract_action.task_id = task.task_id
    extract_action.step_id = step.step_id
    extract_action.step_order = step.order
    extract_action.action_order = 1
    monkeypatch.setattr(agent, "create_extract_action", AsyncMock(return_value=extract_action))

    extraction_payload = {"quote": "42%"}
    mocks = setup_parallel_verification_mocks(
        agent,
        step=step,
        task=task,
        monkeypatch=monkeypatch,
        next_step=next_step,
        complete_action=complete_action,
        handle_action_responses=[
            [ActionSuccess()],
            [ActionSuccess(data=extraction_payload)],
        ],
        extract_action=extract_action,
    )

    browser_state, scraped_page, page = make_browser_state()

    completed, last_step, next_created_step = await agent._handle_completed_step_with_parallel_verification(
        organization=organization,
        task=task,
        step=step,
        page=page,
        browser_state=browser_state,
        scraped_page=scraped_page,
        engine=RunEngine.skyvern_v1,
    )

    assert completed is True
    assert last_step == step
    assert next_created_step is None

    assert mocks.handle_action.await_count == 2

    extracted_information = mocks.update_task.await_args.kwargs["extracted_information"]
    assert extracted_information == extraction_payload


@pytest.mark.asyncio
async def test_parallel_verification_skips_extraction_without_navigation_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, navigation_goal=None)

    step_output = AgentStepOutput(action_results=[], actions_and_results=[])
    step = make_step(
        now,
        task,
        step_id="step-123",
        status=StepStatus.completed,
        order=0,
        output=step_output,
    )

    setup_parallel_verification_mocks(
        agent,
        step=step,
        task=task,
        monkeypatch=monkeypatch,
        next_step=step,
        complete_action=CompleteAction(reasoning="done", verified=True),
        handle_action_responses=[[ActionSuccess()]],
    )

    run_data_extraction_mock = AsyncMock()
    monkeypatch.setattr(agent, "_run_data_extraction_after_complete_action", run_data_extraction_mock)

    browser_state, scraped_page, page = make_browser_state()

    await agent._handle_completed_step_with_parallel_verification(
        organization=organization,
        task=task,
        step=step,
        page=page,
        browser_state=browser_state,
        scraped_page=scraped_page,
        engine=RunEngine.skyvern_v1,
    )

    run_data_extraction_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_speculate_next_step_plan_skips_in_script_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """_speculate_next_step_plan returns None without doing any LLM work
    when ctx.script_mode is True."""
    agent = ForgeAgent()
    now = datetime.now(UTC)

    organization = make_organization(now)
    task = make_task(now, organization)
    current_step = make_step(now, task, step_id="step-cur", status=StepStatus.completed, order=0, output=None)
    next_step = make_step(now, task, step_id="step-next", status=StepStatus.created, order=1, output=None)

    sleep_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("skyvern.forge.agent.asyncio.sleep", sleep_mock)

    build_prompt_mock = AsyncMock()
    monkeypatch.setattr(agent, "build_and_record_step_prompt", build_prompt_mock)

    browser_state, _scraped_page, _page = make_browser_state()

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=None,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        tz_info=ZoneInfo("UTC"),
    )
    context.script_mode = True
    with skyvern_context.scoped(context):
        plan = await agent._speculate_next_step_plan(
            organization=organization,
            task=task,
            current_step=current_step,
            next_step=next_step,
            browser_state=browser_state,
            engine=RunEngine.skyvern_v1,
        )

    assert plan is None
    # No prompt build, no sleep — we exited before either.
    build_prompt_mock.assert_not_called()
    sleep_mock.assert_not_called()


@pytest.mark.asyncio
async def test_speculate_next_step_plan_proceeds_when_not_in_script_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check: without ctx.script_mode, speculation proceeds past the gate
    (builds the prompt, sleeps for the verification head-start, calls the LLM)."""
    agent = ForgeAgent()
    now = datetime.now(UTC)

    organization = make_organization(now)
    task = make_task(now, organization)
    current_step = make_step(now, task, step_id="step-cur", status=StepStatus.completed, order=0, output=None)
    next_step = make_step(now, task, step_id="step-next", status=StepStatus.created, order=1, output=None)

    sleep_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("skyvern.forge.agent.asyncio.sleep", sleep_mock)

    browser_state, scraped_page, page = make_browser_state()
    browser_state.get_working_page = AsyncMock(return_value=page)
    scraped_page.check_pdf_viewer_embed = MagicMock(return_value=False)
    scraped_page.screenshots = [b"img"]

    build_prompt_mock = AsyncMock(return_value=(scraped_page, "prompt", False, "extract-actions"))
    monkeypatch.setattr(agent, "build_and_record_step_prompt", build_prompt_mock)
    monkeypatch.setattr(agent, "register_async_operations", AsyncMock())

    llm_handler_mock = AsyncMock(return_value={"actions": []})
    monkeypatch.setattr(
        "skyvern.forge.agent.LLMAPIHandlerFactory.get_override_llm_api_handler",
        lambda *_args, **_kwargs: llm_handler_mock,
    )
    monkeypatch.setattr(agent.async_operation_pool, "run_operation", MagicMock())

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=None,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        tz_info=ZoneInfo("UTC"),
    )
    # script_mode defaults to False
    with skyvern_context.scoped(context):
        plan = await agent._speculate_next_step_plan(
            organization=organization,
            task=task,
            current_step=current_step,
            next_step=next_step,
            browser_state=browser_state,
            engine=RunEngine.skyvern_v1,
        )

    assert plan is not None
    build_prompt_mock.assert_awaited_once()
    sleep_mock.assert_awaited()  # verification head-start


@pytest.mark.asyncio
async def test_orchestrator_skips_speculative_in_script_mode_complete_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: with ctx.script_mode=True and complete_action=CompleteAction,
    _handle_completed_step_with_parallel_verification must take the discard branch
    without ever calling build_and_record_step_prompt (the first real LLM-tied
    side effect inside _speculate_next_step_plan)."""
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    # No data_extraction_goal — keeps the test focused on the gate and avoids the
    # post-complete extraction path (extraction_cache, etc.) which isn't relevant here.
    task = make_task(now, organization, data_extraction_goal=None)

    step = make_step(
        now,
        task,
        step_id="step-orc-c",
        status=StepStatus.completed,
        order=0,
        output=AgentStepOutput(action_results=[], actions_and_results=[]),
    )
    next_step = make_step(now, task, step_id="step-next-orc-c", status=StepStatus.created, order=1, output=None)

    complete_action = CompleteAction(reasoning="done", verified=True)
    mocks = setup_parallel_verification_mocks(
        agent,
        step=step,
        task=task,
        monkeypatch=monkeypatch,
        next_step=next_step,
        complete_action=complete_action,
        handle_action_responses=[[ActionSuccess()]],
    )
    # Restore the real _speculate_next_step_plan so the in-function gate runs.
    monkeypatch.setattr(agent, "_speculate_next_step_plan", ForgeAgent._speculate_next_step_plan.__get__(agent))
    # Mock build_and_record_step_prompt so we can assert the gate fired before LLM work began.
    build_prompt_mock = AsyncMock()
    monkeypatch.setattr(agent, "build_and_record_step_prompt", build_prompt_mock)

    browser_state, scraped_page, page = make_browser_state()
    browser_state.must_get_working_page = AsyncMock(return_value=page)

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=None,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        tz_info=ZoneInfo("UTC"),
    )
    context.script_mode = True
    with skyvern_context.scoped(context):
        completed, _last_step, returned_next_step = await agent._handle_completed_step_with_parallel_verification(
            organization=organization,
            task=task,
            step=step,
            page=page,
            browser_state=browser_state,
            scraped_page=scraped_page,
            engine=RunEngine.skyvern_v1,
        )

    # Verification ran:
    mocks.check_user_goal_complete.assert_awaited_once()
    # Speculative LLM side-effect never happened (the gate inside _speculate_next_step_plan fired):
    build_prompt_mock.assert_not_called()
    # complete-action branch: task marked completed, returns (True, last_step, None):
    assert completed is True
    assert returned_next_step is None


@pytest.mark.asyncio
async def test_orchestrator_skips_speculative_in_script_mode_not_achieved_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: with ctx.script_mode=True and complete_action=None,
    _handle_completed_step_with_parallel_verification must take the
    sequential-continuation branch without ever calling build_and_record_step_prompt."""
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)

    step = make_step(
        now,
        task,
        step_id="step-orc-na",
        status=StepStatus.completed,
        order=0,
        output=AgentStepOutput(action_results=[], actions_and_results=[]),
    )
    next_step = make_step(now, task, step_id="step-next-orc-na", status=StepStatus.created, order=1, output=None)

    mocks = setup_parallel_verification_mocks(
        agent,
        step=step,
        task=task,
        monkeypatch=monkeypatch,
        next_step=next_step,
        complete_action=None,
        handle_action_responses=[],
    )
    # Restore the real _speculate_next_step_plan so the gate inside it runs.
    monkeypatch.setattr(agent, "_speculate_next_step_plan", ForgeAgent._speculate_next_step_plan.__get__(agent))
    # Mock build_and_record_step_prompt so we can assert it was never called.
    build_prompt_mock = AsyncMock()
    monkeypatch.setattr(agent, "build_and_record_step_prompt", build_prompt_mock)
    monkeypatch.setattr(
        "skyvern.forge.agent.ForgeAgent._check_workflow_run_step_budget",
        AsyncMock(return_value=None),
    )

    browser_state, scraped_page, page = make_browser_state()

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=None,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        tz_info=ZoneInfo("UTC"),
    )
    context.script_mode = True
    with skyvern_context.scoped(context):
        completed, _last_step, returned_next_step = await agent._handle_completed_step_with_parallel_verification(
            organization=organization,
            task=task,
            step=step,
            page=page,
            browser_state=browser_state,
            scraped_page=scraped_page,
            engine=RunEngine.skyvern_v1,
        )

    mocks.check_user_goal_complete.assert_awaited_once()
    build_prompt_mock.assert_not_called()
    # Sequential-continuation branch returns (None, None, next_step):
    assert completed is None
    assert returned_next_step == next_step


@pytest.mark.asyncio
async def test_speculate_next_step_plan_skips_for_cua_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CUA engine bypass short-circuits before the script_mode gate, even when
    script_mode is explicitly False (i.e. the nominal non-script_mode agent path)."""
    from skyvern.schemas.runs import CUA_ENGINES

    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    current_step = make_step(now, task, step_id="step-cur", status=StepStatus.completed, order=0, output=None)
    next_step = make_step(now, task, step_id="step-next", status=StepStatus.created, order=1, output=None)

    sleep_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("skyvern.forge.agent.asyncio.sleep", sleep_mock)
    build_prompt_mock = AsyncMock()
    monkeypatch.setattr(agent, "build_and_record_step_prompt", build_prompt_mock)

    browser_state, _scraped_page, _page = make_browser_state()

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=None,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        tz_info=ZoneInfo("UTC"),
    )
    # script_mode False — proves the CUA early-return runs first, regardless of the script_mode gate.
    cua_engine = next(iter(CUA_ENGINES))
    with skyvern_context.scoped(context):
        plan = await agent._speculate_next_step_plan(
            organization=organization,
            task=task,
            current_step=current_step,
            next_step=next_step,
            browser_state=browser_state,
            engine=cua_engine,
        )

    assert plan is None
    build_prompt_mock.assert_not_called()
    sleep_mock.assert_not_called()


def test_task_validate_update_requires_extracted_information() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        data_extraction_goal="Need data",
    )

    with pytest.raises(ValueError):
        task.validate_update(TaskStatus.completed, extracted_information=None)


@pytest.mark.asyncio
async def test_agent_step_skips_user_goal_check_when_feature_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, navigation_goal="Reach confirmation page", workflow_run_id="workflow-1")
    step = make_step(
        now,
        task,
        step_id="step-disable",
        status=StepStatus.created,
        order=0,
        output=None,
    )

    browser_state, _, page = make_browser_state()
    browser_state.must_get_working_page = AsyncMock(return_value=page)
    browser_state.get_working_page = AsyncMock(return_value=page)

    async def _dummy_cleanup(*_args, **_kwargs) -> list[dict]:
        return []

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=_dummy_cleanup,
        _scrape_exclude=None,
    )
    scraped_page.screenshots = [b"image"]

    agent.build_and_record_step_prompt = AsyncMock(return_value=(scraped_page, "prompt", False, "extract-actions"))
    json_response: dict[str, object] = {"actions": [{"action_type": "CLICK", "element_id": "node-1"}]}
    agent.handle_potential_OTP_actions = AsyncMock(return_value=(json_response, []))

    click_action = ClickAction(
        element_id="node-1",
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        task_id=task.task_id,
        step_id=step.step_id,
        step_order=step.order,
        action_order=0,
    )
    monkeypatch.setattr("skyvern.forge.agent.parse_actions", lambda *_, **__: [click_action])

    action_handler_mock = AsyncMock(return_value=[ActionSuccess()])
    monkeypatch.setattr("skyvern.forge.agent.ActionHandler.handle_action", action_handler_mock)
    agent.record_artifacts_after_action = AsyncMock()
    agent._is_multi_field_totp_sequence = MagicMock(return_value=False)
    agent.check_user_goal_complete = AsyncMock()

    llm_handler_mock = AsyncMock(return_value=json_response)
    monkeypatch.setattr(
        "skyvern.forge.agent.LLMAPIHandlerFactory.get_override_llm_api_handler",
        lambda *_args, **_kwargs: llm_handler_mock,
    )
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.prepare_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.post_action_execution", AsyncMock())
    monkeypatch.setattr("skyvern.forge.agent.asyncio.sleep", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.random.uniform", lambda *_args, **_kwargs: 0)

    async def fake_update_step(
        step: Step,
        status: StepStatus | None = None,
        output=None,
        is_last: bool | None = None,
        retry_index: int | None = None,
        **_kwargs,
    ) -> Step:
        if status is not None:
            step.status = status
        if output is not None:
            step.output = output
        if is_last is not None:
            step.is_last = is_last
        if retry_index is not None:
            step.retry_index = retry_index
        return step

    agent.update_step = AsyncMock(side_effect=fake_update_step)

    async def feature_flag_side_effect(flag_name: str, *_args, **_kwargs) -> bool:
        if flag_name == "DISABLE_USER_GOAL_CHECK":
            return True
        return False

    monkeypatch.setattr(
        "skyvern.forge.agent.app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached",
        AsyncMock(side_effect=feature_flag_side_effect),
    )

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=None,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        tz_info=ZoneInfo("UTC"),
    )
    skyvern_context.set(context)
    try:
        completed_step, detailed_output = await agent.agent_step(
            task=task,
            step=step,
            browser_state=browser_state,
            organization=organization,
        )
    finally:
        skyvern_context.reset()

    assert completed_step.status == StepStatus.completed
    assert detailed_output.actions_and_results is not None
    assert action_handler_mock.await_count == 1
    agent.record_artifacts_after_action.assert_awaited()
    agent.check_user_goal_complete.assert_not_called()


@pytest.mark.asyncio
async def test_agent_step_persists_artifacts_when_using_speculative_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, navigation_goal=None)
    step = make_step(
        now,
        task,
        step_id="step-speculative",
        status=StepStatus.created,
        order=0,
        output=None,
    )

    browser_state, _, page = make_browser_state()
    browser_state.must_get_working_page = AsyncMock(return_value=page)
    browser_state.get_working_page = AsyncMock(return_value=page)

    async def _dummy_cleanup(*_args, **_kwargs) -> list[dict]:
        return []

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[{"tagName": "div", "children": []}],
        element_tree_trimmed=[{"tagName": "div", "children": []}],
        _browser_state=browser_state,
        _clean_up_func=_dummy_cleanup,
        _scrape_exclude=None,
    )
    scraped_page.html = "<html></html>"
    scraped_page.id_to_css_dict = {"node-1": "#node"}
    scraped_page.id_to_frame_dict = {"node-1": "frame-1"}
    scraped_page.screenshots = [b"image"]

    speculative_plan = SpeculativePlan(
        scraped_page=scraped_page,
        extract_action_prompt="unused",
        use_caching=False,
        llm_json_response=None,
        llm_metadata=None,
        prompt_name="extract-actions",
    )

    extract_action = ExtractAction(
        reasoning="collect data",
        data_extraction_goal=task.data_extraction_goal,
        data_extraction_schema=task.extracted_information_schema,
    )
    extract_action.organization_id = task.organization_id
    extract_action.workflow_run_id = task.workflow_run_id
    extract_action.task_id = task.task_id
    extract_action.step_id = step.step_id
    extract_action.step_order = step.order
    extract_action.action_order = 0

    agent.create_extract_action = AsyncMock(return_value=extract_action)
    agent.record_artifacts_after_action = AsyncMock()
    agent._persist_scrape_artifacts = AsyncMock()
    agent._is_multi_field_totp_sequence = MagicMock(return_value=False)

    action_handler_mock = AsyncMock(return_value=[ActionSuccess()])
    monkeypatch.setattr("skyvern.forge.agent.ActionHandler.handle_action", action_handler_mock)
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.prepare_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.post_action_execution", AsyncMock())
    monkeypatch.setattr("skyvern.forge.agent.asyncio.sleep", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.random.uniform", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.workflow_params.create_action", AsyncMock())
    monkeypatch.setattr(
        "skyvern.forge.agent.app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached",
        AsyncMock(return_value=False),
    )

    async def fake_update_step(
        step: Step,
        status: StepStatus | None = None,
        output=None,
        is_last: bool | None = None,
        retry_index: int | None = None,
        **_kwargs,
    ) -> Step:
        if status is not None:
            step.status = status
        if output is not None:
            step.output = output
        if is_last is not None:
            step.is_last = is_last
        if retry_index is not None:
            step.retry_index = retry_index
        return step

    agent.update_step = AsyncMock(side_effect=fake_update_step)

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=None,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        tz_info=ZoneInfo("UTC"),
    )
    context.speculative_plans[step.step_id] = speculative_plan
    skyvern_context.set(context)
    try:
        completed_step, detailed_output = await agent.agent_step(
            task=task,
            step=step,
            browser_state=browser_state,
            organization=organization,
        )
    finally:
        skyvern_context.reset()

    assert completed_step.status == StepStatus.completed
    assert detailed_output.actions is not None
    agent._persist_scrape_artifacts.assert_awaited_once()


def _make_scrape_test_fixtures(now, monkeypatch):
    """Shared setup for _persist_scrape_artifacts tests."""
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(
        now,
        task,
        step_id="step-artifacts",
        status=StepStatus.created,
        order=0,
        output=None,
    )
    browser_state, _, _ = make_browser_state()

    async def _dummy_cleanup(*_args, **_kwargs) -> list[dict]:
        return []

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[{"tagName": "div"}],
        element_tree_trimmed=[{"tagName": "div"}],
        _browser_state=browser_state,
        _clean_up_func=_dummy_cleanup,
        _scrape_exclude=None,
    )
    scraped_page.html = "<html></html>"
    scraped_page.id_to_css_dict = {"node-1": "#node"}
    scraped_page.id_to_frame_dict = {"node-1": "frame-1"}
    scraped_page.element_tree = [{"tagName": "div"}]
    scraped_page.element_tree_trimmed = [{"tagName": "div"}]

    economy_tree_mock = MagicMock(return_value="<economy>")
    full_tree_mock = MagicMock(return_value="<full>")
    monkeypatch.setattr(ScrapedPage, "build_economy_elements_tree", lambda self, *a, **kw: economy_tree_mock())
    monkeypatch.setattr(ScrapedPage, "build_element_tree", lambda self, *a, **kw: full_tree_mock())

    return task, step, scraped_page, economy_tree_mock, full_tree_mock


@pytest.mark.asyncio
async def test_persist_scrape_artifacts_bundling_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With USE_ARTIFACT_BUNDLING on, all 6 scrape fields go into a single archive call."""
    agent = ForgeAgent()
    now = datetime.now(UTC)
    task, step, scraped_page, economy_tree_mock, full_tree_mock = _make_scrape_test_fixtures(now, monkeypatch)

    accumulate_mock = MagicMock()
    monkeypatch.setattr("skyvern.forge.agent.app.ARTIFACT_MANAGER.accumulate_scrape_to_archive", accumulate_mock)

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=None,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        tz_info=ZoneInfo("UTC"),
    )
    context.enable_speed_optimizations = True
    context.use_artifact_bundling = True

    await agent._persist_scrape_artifacts(task=task, step=step, scraped_page=scraped_page, context=context)

    accumulate_mock.assert_called_once()
    call_kwargs = accumulate_mock.call_args.kwargs
    assert call_kwargs["html"] == b"<html></html>"
    assert "node-1" in call_kwargs["id_css_map"].decode()
    assert "node-1" in call_kwargs["id_frame_map"].decode()
    assert call_kwargs["element_tree_in_prompt"] == b"<economy>"
    economy_tree_mock.assert_called_once()
    full_tree_mock.assert_not_called()


@pytest.mark.asyncio
async def test_persist_scrape_artifacts_bundling_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With USE_ARTIFACT_BUNDLING off (default), 6 individual create_artifact calls are made."""
    agent = ForgeAgent()
    now = datetime.now(UTC)
    task, step, scraped_page, economy_tree_mock, full_tree_mock = _make_scrape_test_fixtures(now, monkeypatch)

    create_artifact_mock = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.ARTIFACT_MANAGER.create_artifact", create_artifact_mock)

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=None,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        tz_info=ZoneInfo("UTC"),
    )
    context.enable_speed_optimizations = True
    context.use_artifact_bundling = False  # default — individual uploads

    await agent._persist_scrape_artifacts(task=task, step=step, scraped_page=scraped_page, context=context)

    assert create_artifact_mock.await_count == 6
    artifact_types = [call.kwargs["artifact_type"] for call in create_artifact_mock.await_args_list]
    assert ArtifactType.HTML_SCRAPE in artifact_types
    assert ArtifactType.VISIBLE_ELEMENTS_ID_CSS_MAP in artifact_types
    assert ArtifactType.VISIBLE_ELEMENTS_ID_FRAME_MAP in artifact_types
    assert ArtifactType.VISIBLE_ELEMENTS_TREE in artifact_types
    assert ArtifactType.VISIBLE_ELEMENTS_TREE_TRIMMED in artifact_types
    assert ArtifactType.VISIBLE_ELEMENTS_TREE_IN_PROMPT in artifact_types
    economy_tree_mock.assert_called_once()
    full_tree_mock.assert_not_called()


@pytest.mark.asyncio
async def test_persist_scrape_artifacts_bundling_disabled_logs_and_reraises_failures(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """All scrape artifact uploads run to completion; failures are logged and the first is re-raised."""
    agent = ForgeAgent()
    now = datetime.now(UTC)
    task, step, scraped_page, economy_tree_mock, full_tree_mock = _make_scrape_test_fixtures(now, monkeypatch)

    expected_error = RuntimeError("artifact upload failed")

    async def _create_artifact(*, step: Step, artifact_type: ArtifactType, data: bytes) -> str:
        if artifact_type == ArtifactType.VISIBLE_ELEMENTS_ID_CSS_MAP:
            raise expected_error
        return f"artifact-{artifact_type.value}"

    create_artifact_mock = AsyncMock(side_effect=_create_artifact)
    monkeypatch.setattr("skyvern.forge.agent.app.ARTIFACT_MANAGER.create_artifact", create_artifact_mock)

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=None,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        tz_info=ZoneInfo("UTC"),
    )
    context.enable_speed_optimizations = True
    context.use_artifact_bundling = False

    with pytest.raises(RuntimeError, match="artifact upload failed"):
        await agent._persist_scrape_artifacts(task=task, step=step, scraped_page=scraped_page, context=context)

    assert create_artifact_mock.await_count == 6
    assert any(
        "Failed to persist scrape artifact" in record.message and "visible_elements_id_css_map" in record.message
        for record in caplog.records
    )
    economy_tree_mock.assert_called_once()
    full_tree_mock.assert_not_called()
