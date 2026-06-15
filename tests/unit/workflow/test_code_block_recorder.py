"""Tests for the RecordingPage proxy that records code block playwright calls as actions."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.code_block_recorder import (
    CODE_BLOCK_FILENAME,
    CODE_LINE_OFFSET,
    RecordingPage,
    user_code_line_from_exception,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import BlockStatus
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ActionStatus


class FakeLocator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def locator(self, selector):  # noqa: ANN001, ANN201
        return self

    def get_by_text(self, text, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self

    @property
    def first(self):  # noqa: ANN201
        return self

    async def click(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append("click")

    async def fill(self, value, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append(f"fill:{value}")

    async def type(self, value, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append(f"type:{value}")

    async def select_option(self, value, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append(f"select:{value}")

    async def press(self, key, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append(f"press:{key}")

    def filter(self, **kwargs):  # noqa: ANN003, ANN201
        return self


class FakeKeyboard:
    async def press(self, key, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return None


class FakePage:
    def __init__(self) -> None:
        self.inner = FakeLocator()
        self.keyboard = FakeKeyboard()
        self.url = "about:blank"

    async def goto(self, url, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return None

    async def wait_for_load_state(self, state="load", **kwargs):  # noqa: ANN001, ANN003, ANN201
        return None

    def locator(self, selector):  # noqa: ANN001, ANN201
        return self.inner

    async def click(self, selector, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return None

    async def fill(self, selector, value, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return None

    def get_by_role(self, role, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self.inner

    async def screenshot(self, **kwargs):  # noqa: ANN003, ANN201
        return b"img"


@pytest.mark.asyncio
async def test_records_goto_click_fill_with_types_and_order() -> None:
    page = RecordingPage(FakePage())
    await page.goto("https://example.com")
    await page.locator("#q").fill("hello")
    await page.locator("#go").click()
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [
        ActionType.GOTO_URL,
        ActionType.INPUT_TEXT,
        ActionType.CLICK,
    ]
    assert [a.action_order for a in recorded] == [0, 1, 2]
    assert all(a.status == ActionStatus.completed for a in recorded)
    assert recorded[0].description == "page.goto https://example.com"


@pytest.mark.asyncio
async def test_unmapped_calls_and_attributes_pass_through_unrecorded() -> None:
    fake = FakePage()
    page = RecordingPage(fake)
    await page.wait_for_load_state("networkidle")
    assert page.url == "about:blank"
    assert page.recorded_actions() == []


@pytest.mark.asyncio
async def test_keyboard_press_records_keypress_action() -> None:
    page = RecordingPage(FakePage())
    await page.keyboard.press("Enter")
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.KEYPRESS]
    assert recorded[0].description and "Enter" in recorded[0].description


@pytest.mark.asyncio
async def test_get_by_role_click_is_recorded() -> None:
    page = RecordingPage(FakePage())
    await page.get_by_role("button", name="Go").click()
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.CLICK]
    assert recorded[0].description == "locator.click get_by_role(button)"


@pytest.mark.asyncio
async def test_locator_get_by_chain_is_recorded() -> None:
    page = RecordingPage(FakePage())
    await page.locator("#form").get_by_text("Submit").click()
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.CLICK]
    assert recorded[0].description == "locator.click get_by_text(Submit)"


@pytest.mark.asyncio
async def test_direct_page_actions_are_recorded_with_redaction() -> None:
    page = RecordingPage(FakePage())
    await page.click("#submit")
    await page.fill("#email", "secret@example.com")
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.CLICK, ActionType.INPUT_TEXT]
    # Input values may be credentials; the fill value must never reach the description.
    assert all("secret@example.com" not in (a.description or "") for a in recorded)


@pytest.mark.asyncio
async def test_filter_locator_chain_click_is_recorded() -> None:
    page = RecordingPage(FakePage())
    await page.get_by_role("button", name="Go").filter(has_text="Submit").click()
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.CLICK]


@pytest.mark.asyncio
async def test_failed_call_records_failed_action_and_reraises() -> None:
    class ExplodingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("element detached")

    fake = FakePage()
    fake.inner = ExplodingLocator()
    page = RecordingPage(fake)
    with pytest.raises(RuntimeError):
        await page.locator("#x").click()
    recorded = page.recorded_actions()
    assert recorded[-1].action_type == ActionType.CLICK
    assert recorded[-1].status == ActionStatus.failed
    assert "element detached" in (recorded[-1].response or "")


@pytest.mark.asyncio
async def test_on_action_sink_receives_each_action_and_errors_are_swallowed() -> None:
    seen: list[ActionType] = []

    async def sink(action) -> None:  # noqa: ANN001
        seen.append(action.action_type)
        raise RuntimeError("sink failure must not break recording")

    page = RecordingPage(FakePage(), on_action=sink)
    await page.goto("https://example.com")
    await page.locator("#go").click()
    assert seen == [ActionType.GOTO_URL, ActionType.CLICK]
    assert len(page.recorded_actions()) == 2


def test_user_code_line_from_exception_unwraps_wrapper_offset() -> None:
    code = "raise ValueError('boom')"
    full_code = f"\nasync def wrapper():\n    {code}\n    return None\n"
    namespace: dict = {}
    exec(compile(full_code, CODE_BLOCK_FILENAME, "exec"), {}, namespace)
    with pytest.raises(ValueError) as exc_info:
        asyncio.run(namespace["wrapper"]())
    line = user_code_line_from_exception(exc_info.value)
    assert line == 3 - CODE_LINE_OFFSET  # frame line 3 -> user line 1


@pytest.mark.asyncio
async def test_generated_user_function_exception_maps_to_user_code_line() -> None:
    block = _make_code_block("x = 1")
    user_function = block.generate_async_user_function("x = 1\nraise Exception('boom')", FakePage())
    with pytest.raises(Exception, match="boom") as exc_info:
        await user_function()
    assert user_code_line_from_exception(exc_info.value) == 2


@pytest.mark.asyncio
async def test_input_values_are_elided_from_descriptions() -> None:
    page = RecordingPage(FakePage())
    await page.locator("#pw").fill("hunter2-credential")
    await page.locator("#user").type("alice-credential")
    recorded = page.recorded_actions()
    dumped = json.dumps([a.model_dump(mode="json") for a in recorded])
    assert "hunter2-credential" not in dumped
    assert "alice-credential" not in dumped
    assert recorded[0].description == "locator.fill #pw"
    assert recorded[1].description == "locator.type #user"


def _make_code_block(code: str, goal: str | None = None) -> CodeBlock:
    now = datetime.now(timezone.utc)
    output_parameter = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="code_output",
        description="test output",
        output_parameter_id="op_code",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )
    return CodeBlock(label="code_1", code=code, prompt=goal, output_parameter=output_parameter)


class _FakeTask:
    def __init__(self) -> None:
        self.task_id = "tsk_code"
        self.organization_id = "o_test"


class _FakeStep:
    def __init__(self) -> None:
        self.step_id = "stp_code"
        self.order = 0


class FakeWorkflowRunContext:
    """Minimal context for CodeBlock.execute; masking delegates to the real implementation."""

    values: dict = {}
    workflow_run_outputs: list = []
    include_secrets_in_templates = False
    workflow_title = "Test Workflow"
    workflow_id = "w_test"
    workflow_permanent_id = "wpid_test"
    workflow_run_id = "wr_test"
    browser_session_id = None

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self.secrets = secrets or {}

    def get_block_metadata(self, label):  # noqa: ANN001, ANN201
        return {}

    def build_workflow_run_summary(self) -> str:
        return ""

    def get_value(self, key):  # noqa: ANN001, ANN201
        return self.values.get(key)

    def get_original_secret_value_or_none(self, value):  # noqa: ANN001, ANN201
        return None

    def mask_secrets_in_data(self, data, mask="*****"):  # noqa: ANN001, ANN201
        return WorkflowRunContext.mask_secrets_in_data(self, data, mask)  # type: ignore[arg-type]

    async def register_output_parameter_value_post_execution(self, parameter, value):  # noqa: ANN001, ANN201
        return None


def _patch_execute_environment(
    monkeypatch: pytest.MonkeyPatch,
    page: FakePage,
    context: FakeWorkflowRunContext,
) -> dict[str, AsyncMock]:
    class FakeBrowserState:
        async def get_working_page(self):  # noqa: ANN201
            return page

    async def validate_code_block(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return None

    async def get_browser_state(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return FakeBrowserState()

    async def record_output(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return None

    mocks = {
        "get_workflow_run_block": AsyncMock(return_value=object()),
        "update_workflow_run_block": AsyncMock(return_value=None),
        "create_artifact": AsyncMock(return_value="artifact_1"),
        "create_task_and_step": AsyncMock(return_value=(_FakeTask(), _FakeStep())),
        "create_action": AsyncMock(return_value=None),
        "update_task": AsyncMock(return_value=None),
        "update_step": AsyncMock(return_value=None),
    }
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.app.AGENT_FUNCTION.validate_code_block", validate_code_block
    )
    monkeypatch.setattr(CodeBlock, "get_or_create_browser_state", get_browser_state)
    monkeypatch.setattr(CodeBlock, "get_workflow_run_context", lambda *args: context)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output)
    monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_block", mocks["get_workflow_run_block"])
    monkeypatch.setattr(app.DATABASE.observer, "update_workflow_run_block", mocks["update_workflow_run_block"])
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "create_workflow_run_block_artifact", mocks["create_artifact"])
    monkeypatch.setattr(app.agent, "create_task_and_step_from_code_block", mocks["create_task_and_step"], raising=False)
    monkeypatch.setattr(app.DATABASE.workflow_params, "create_action", mocks["create_action"])
    monkeypatch.setattr(app.DATABASE.tasks, "update_task", mocks["update_task"])
    monkeypatch.setattr(app.DATABASE.tasks, "update_step", mocks["update_step"])
    return mocks


def _created_actions(mocks: dict[str, AsyncMock]) -> list[Action]:
    return [call.args[0] for call in mocks["create_action"].await_args_list]


@pytest.mark.asyncio
async def test_goal_code_block_creates_task_and_links_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """A code block with a goal spins up a task v1 + step and links it to the run block."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("value = 'ok'", goal="log into the portal")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert mocks["create_task_and_step"].await_count == 1
    linked = [
        call.kwargs.get("task_id")
        for call in mocks["update_workflow_run_block"].await_args_list
        if call.kwargs.get("task_id") is not None
    ]
    assert linked == ["tsk_code"]


@pytest.mark.asyncio
async def test_create_task_and_step_from_code_block_maps_goal_to_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container task carries the code block goal as its navigation goal so the agent can resume it."""
    create_task = AsyncMock(return_value=_FakeTask())
    update_task = AsyncMock(return_value=_FakeTask())
    create_step = AsyncMock(return_value=_FakeStep())
    monkeypatch.setattr(app.DATABASE.tasks, "get_last_task_for_workflow_run", AsyncMock(return_value=None))
    monkeypatch.setattr(app.DATABASE.tasks, "create_task", create_task)
    monkeypatch.setattr(app.DATABASE.tasks, "update_task", update_task)
    monkeypatch.setattr(app.DATABASE.tasks, "create_step", create_step)

    block = _make_code_block("x = 1", goal="log into the portal")
    task, step = await ForgeAgent().create_task_and_step_from_code_block(
        code_block=block,
        organization_id="o_test",
        workflow_run_id="wr_test",
        task_url="https://example.com/login",
    )

    assert task.task_id == "tsk_code"
    assert step.step_id == "stp_code"
    assert create_task.await_args.kwargs["navigation_goal"] == "log into the portal"
    assert create_task.await_args.kwargs["url"] == "https://example.com/login"
    assert update_task.await_args.kwargs["status"] == TaskStatus.running
    assert create_step.await_args.kwargs["order"] == 0


@pytest.mark.asyncio
async def test_goal_code_block_marks_task_completed_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container task must not dangle in 'running'; success drives it to completed."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("value = 'ok'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    statuses = [call.kwargs.get("status") for call in mocks["update_task"].await_args_list]
    assert TaskStatus.completed in statuses
    step_statuses = [call.kwargs.get("status") for call in mocks["update_step"].await_args_list]
    assert StepStatus.completed in step_statuses


@pytest.mark.asyncio
async def test_goal_code_block_marks_task_failed_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing code block drives its container task to failed, not a stuck 'running'."""

    class ExplodingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("element detached")

    page = FakePage()
    page.inner = ExplodingLocator()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.locator('#x').click()", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is False
    statuses = [call.kwargs.get("status") for call in mocks["update_task"].await_args_list]
    assert TaskStatus.failed in statuses
    step_statuses = [call.kwargs.get("status") for call in mocks["update_step"].await_args_list]
    assert StepStatus.failed in step_statuses


@pytest.mark.asyncio
async def test_goal_code_block_finalizes_step_on_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """An asyncio.CancelledError (copilot orphan-cancel) must still finalize task + step, not dangle in 'running'/'created'."""

    class CancellingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise asyncio.CancelledError()

    page = FakePage()
    page.inner = CancellingLocator()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.locator('#x').click()", goal="go")
    with pytest.raises(asyncio.CancelledError):
        await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    task_statuses = [call.kwargs.get("status") for call in mocks["update_task"].await_args_list]
    step_statuses = [call.kwargs.get("status") for call in mocks["update_step"].await_args_list]
    assert TaskStatus.failed in task_statuses
    assert StepStatus.failed in step_statuses


@pytest.mark.asyncio
async def test_goalless_code_block_creates_no_task_or_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a goal there is no task to hang actions on, so none are created or persisted."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.locator('#go').click()\nvalue = 'ok'")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert mocks["create_task_and_step"].await_count == 0
    assert mocks["create_action"].await_count == 0


@pytest.mark.asyncio
async def test_goalless_code_block_skips_screenshots(monkeypatch: pytest.MonkeyPatch) -> None:
    """No task means screenshots would have no action row to anchor to, so don't take orphan ones."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.locator('#go').click()\nvalue = 'ok'")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert mocks["create_artifact"].await_count == 0


@pytest.mark.asyncio
async def test_recorded_calls_persist_as_actions_on_the_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each recorded playwright call becomes a real Action row tied to the task/step."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.goto('https://example.com')\nawait page.locator('#go').click()", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    actions = _created_actions(mocks)
    assert [a.action_type for a in actions] == [ActionType.GOTO_URL, ActionType.CLICK]
    assert all(a.task_id == "tsk_code" and a.step_id == "stp_code" and a.step_order == 0 for a in actions)
    assert [a.action_order for a in actions] == [0, 1]


@pytest.mark.asyncio
async def test_backgrounded_screenshots_are_drained_and_linked_before_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Screenshots upload off the user-await chain, but the drain must finish before
    actions persist so each persisted action carries its screenshot artifact id.
    The upload is made slow so this fails if the pre-persist drain is dropped: the
    action would serialize before the still-running upload sets its id."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    async def slow_upload(**kwargs: object) -> str:
        # Outlasts the microsecond gap between the user function ending and persist's
        # serialization, so only an explicit drain can complete it in time.
        await asyncio.sleep(0.05)
        return "artifact_1"

    mocks["create_artifact"].side_effect = slow_upload

    block = _make_code_block("await page.goto('https://example.com')\nawait page.locator('#go').click()", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    # Both screenshot-eligible actions captured an artifact off the await chain...
    assert mocks["create_artifact"].await_count == 2
    # ...and the drain ran before persistence, so every persisted action links its screenshot.
    actions = _created_actions(mocks)
    assert [a.action_type for a in actions] == [ActionType.GOTO_URL, ActionType.CLICK]
    assert all(a.screenshot_artifact_id == "artifact_1" for a in actions)


@pytest.mark.asyncio
async def test_persist_failure_does_not_fail_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)
    mocks["create_action"].side_effect = RuntimeError("db unavailable")

    block = _make_code_block("await page.locator('#go').click()\nvalue = 'ok'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert result.output_parameter_value is not None
    assert result.output_parameter_value["value"] == "ok"


@pytest.mark.asyncio
async def test_caught_page_failure_then_unrelated_raise_persists_synthetic_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swallowed page failure must not steal attribution from a later unrelated raise."""

    class ExplodingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("element detached")

    page = FakePage()
    page.inner = ExplodingLocator()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    code = "try:\n    await page.locator('#x').click()\nexcept Exception:\n    pass\nraise Exception('later failure')"
    block = _make_code_block(code, goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is False
    actions = _created_actions(mocks)
    assert actions[-2].action_type == ActionType.CLICK
    assert actions[-2].status == ActionStatus.failed
    assert actions[-1].action_type == ActionType.NULL_ACTION
    assert actions[-1].status == ActionStatus.failed
    assert isinstance(actions[-1].output, dict) and actions[-1].output["code_line"] == 5
    assert "later failure" in (actions[-1].response or "")


@pytest.mark.asyncio
async def test_persisted_actions_never_contain_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "s3cr3t-credential-value"

    class ExplodingLocator(FakeLocator):
        async def fill(self, value, **kwargs):  # noqa: ANN001, ANN003, ANN201
            raise RuntimeError(f"cannot fill element with {value}")

    page = FakePage()
    page.inner = ExplodingLocator()
    context = FakeWorkflowRunContext(secrets={"cred": secret})
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block(f"await page.locator('#pw').fill('{secret}')", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.status == BlockStatus.failed
    actions = _created_actions(mocks)
    assert actions
    dumped = json.dumps([a.model_dump(mode="json") for a in actions])
    assert secret not in dumped
