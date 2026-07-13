"""Tests for the RecordingPage proxy that records code block playwright calls as actions."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.core.script_generations.skyvern_page import SkyvernPage
from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.copilot.code_block_steps import _METHOD_ACTION_TYPES
from skyvern.forge.sdk.db.models import ActionModel
from skyvern.forge.sdk.db.utils import hydrate_action
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.code_block_recorder import (
    _HIGH_LEVEL_ACTION_MAP,
    _LOCATOR_ACTION_MAP,
    _PAGE_ACTION_MAP,
    CODE_BLOCK_FILENAME,
    CODE_LINE_OFFSET,
    RecordingKeyboard,
    RecordingLocator,
    RecordingPage,
    _Recorder,
    json_safe_recorder_output,
    user_code_line_from_exception,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import BlockStatus
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ActionStatus, ClickAction, GotoUrlAction, InputTextAction
from skyvern.webeye.browser_artifacts import BrowserArtifacts


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

    async def evaluate(self, expression, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        return None

    async def complete(self, prompt=None, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return None

    async def scroll(self, **kwargs):  # noqa: ANN003, ANN201
        return None


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
    assert isinstance(recorded[0], GotoUrlAction)
    assert recorded[0].url == "https://example.com"
    assert isinstance(recorded[1], InputTextAction)
    assert recorded[1].element_id == "#q"
    assert recorded[1].text == ""
    assert isinstance(recorded[2], ClickAction)
    assert recorded[2].element_id == "#go"


@pytest.mark.asyncio
async def test_page_evaluate_records_execute_js_action() -> None:
    page = RecordingPage(FakePage())
    await page.evaluate("() => document.title")
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.EXECUTE_JS]
    assert recorded[0].description == "page.evaluate () => document.title"
    assert recorded[0].status == ActionStatus.completed


@pytest.mark.asyncio
async def test_extract_does_not_resolve_on_a_raw_playwright_page() -> None:
    """Code blocks run on a raw Playwright page and must never reach the LLM extraction path,
    so page.extract neither resolves nor records a step."""
    page = RecordingPage(FakePage())
    await page.goto("https://example.com/")
    with pytest.raises(AttributeError):
        await page.extract(prompt="Extract the URLs of the top 20 posts")
    assert [a.action_type for a in page.recorded_actions()] == [ActionType.GOTO_URL]


def test_extract_is_absent_from_the_code_block_vocabulary() -> None:
    """Nothing may author or preview a page.extract call in a code block."""
    assert "extract" not in _HIGH_LEVEL_ACTION_MAP
    assert "extract" not in _METHOD_ACTION_TYPES


@pytest.mark.asyncio
async def test_other_high_level_skyvern_page_calls_are_recorded() -> None:
    """High-level SkyvernPage methods without a prompt still record their action type."""
    page = RecordingPage(FakePage())
    await page.scroll()
    await page.complete()
    recorded = page.recorded_actions()
    assert [a.action_type for a in recorded] == [ActionType.SCROLL, ActionType.COMPLETE]


def test_recorder_maps_cover_every_action_wrapped_skyvern_page_method() -> None:
    """The recorder and editor-deriver maps are hand-maintained mirrors of SkyvernPage's
    @action_wrap set. A high-level method added there but absent here would execute
    unrecorded -- the exact SKY-11463 regression -- so assert every @action_wrap method
    is mapped (to the same action_type) on both surfaces, or is an explicit no-op exclusion."""
    live = {}
    for name in dir(SkyvernPage):
        action_type = getattr(getattr(SkyvernPage, name), "__skyvern_action_type__", None)
        if action_type is not None:
            live[name] = action_type
    # Guard against a vacuous pass if introspection ever stops finding the decorated surface.
    assert {"extract", "click", "complete", "scroll"} <= live.keys()

    recorder = {**_PAGE_ACTION_MAP, **_LOCATOR_ACTION_MAP, **_HIGH_LEVEL_ACTION_MAP}
    excluded = {
        "null_action",  # NULL_ACTION is a no-op probe, never a timeline step
        "extract",  # code blocks run raw Playwright; page.extract must not reach the LLM path
    }

    for name, action_type in live.items():
        if name in excluded:
            continue
        assert recorder.get(name) == action_type, (
            f"SkyvernPage.{name} is @action_wrap({action_type}) but RecordingPage maps it to "
            f"{recorder.get(name)!r}; add it to code_block_recorder or it executes unrecorded"
        )
        assert _METHOD_ACTION_TYPES.get(name) == action_type.value, (
            f"SkyvernPage.{name} ({action_type}) is missing/mismatched in "
            f"code_block_steps._METHOD_ACTION_TYPES; the editor step preview will drift from the timeline"
        )


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
    workflow = None

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
        def __init__(self) -> None:
            self.browser_artifacts = BrowserArtifacts()

        async def get_working_page(self):  # noqa: ANN201
            return page

    async def validate_code_block(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return None

    browser_state = FakeBrowserState()

    async def get_browser_state(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return browser_state

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
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", lambda *args, **kwargs: browser_state)
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
async def test_create_task_and_step_from_code_block_fails_partial_task_on_step_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If step creation fails after the container task is running, fail the task before the recorder degrades."""
    create_task = AsyncMock(return_value=_FakeTask())
    update_task = AsyncMock(return_value=_FakeTask())
    create_step = AsyncMock(side_effect=RuntimeError("step unavailable"))
    monkeypatch.setattr(app.DATABASE.tasks, "get_last_task_for_workflow_run", AsyncMock(return_value=None))
    monkeypatch.setattr(app.DATABASE.tasks, "create_task", create_task)
    monkeypatch.setattr(app.DATABASE.tasks, "update_task", update_task)
    monkeypatch.setattr(app.DATABASE.tasks, "create_step", create_step)

    block = _make_code_block("x = 1", goal="log into the portal")

    with pytest.raises(RuntimeError, match="step unavailable"):
        await ForgeAgent().create_task_and_step_from_code_block(
            code_block=block,
            organization_id="o_test",
            workflow_run_id="wr_test",
            task_url="https://example.com/login",
        )

    assert [call.kwargs["status"] for call in update_task.await_args_list] == [
        TaskStatus.running,
        TaskStatus.failed,
    ]


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
async def test_self_heal_success_finalizes_seat_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healed code block finalizes its SEAT task to completed — never a completed block over a failed seat."""

    class ExplodingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("rotted selector")

    page = FakePage()
    page.inner = ExplodingLocator()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)
    # Stub the heal to a success result; this tests execute()'s seat-finalization wiring, not the heal itself.
    monkeypatch.setattr(
        CodeBlock,
        "_attempt_self_heal",
        AsyncMock(return_value=SimpleNamespace(success=True, output_parameter_value=None)),
    )

    block = _make_code_block("await page.locator('#x').click()", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    statuses = [call.kwargs.get("status") for call in mocks["update_task"].await_args_list]
    assert TaskStatus.completed in statuses
    assert TaskStatus.failed not in statuses


@pytest.mark.asyncio
async def test_self_heal_decline_finalizes_seat_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the heal declines (None), the block fails closed and the seat task is finalized failed."""

    class ExplodingLocator(FakeLocator):
        async def click(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("rotted selector")

    page = FakePage()
    page.inner = ExplodingLocator()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)
    monkeypatch.setattr(CodeBlock, "_attempt_self_heal", AsyncMock(return_value=None))

    block = _make_code_block("await page.locator('#x').click()", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is False
    statuses = [call.kwargs.get("status") for call in mocks["update_task"].await_args_list]
    assert TaskStatus.failed in statuses


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
    context = FakeWorkflowRunContext(secrets={"pw": "secret-password"})
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block(
        "await page.goto('https://example.com')\n"
        "await page.locator('#pw').fill('secret-password')\n"
        "await page.locator('#go').click()",
        goal="go",
    )
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    actions = _created_actions(mocks)
    assert [a.action_type for a in actions] == [ActionType.GOTO_URL, ActionType.INPUT_TEXT, ActionType.CLICK]
    assert all(a.task_id == "tsk_code" and a.step_id == "stp_code" and a.step_order == 0 for a in actions)
    assert [a.action_order for a in actions] == [0, 1, 2]
    assert isinstance(actions[0], GotoUrlAction)
    assert actions[0].url == "https://example.com"
    assert isinstance(actions[1], InputTextAction)
    assert actions[1].element_id == "#pw"
    assert actions[1].text == ""
    assert isinstance(actions[2], ClickAction)
    assert actions[2].element_id == "#go"
    dumped = json.dumps([a.model_dump(mode="json") for a in actions])
    assert "secret-password" not in dumped
    hydrated = [
        hydrate_action(
            ActionModel(
                action_type=action.action_type,
                status=action.status,
                action_json=action.model_dump(mode="json"),
            )
        )
        for action in actions
    ]
    assert [type(action) for action in hydrated] == [GotoUrlAction, InputTextAction, ClickAction]


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
async def test_page_evaluate_action_captures_and_links_screenshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """page.evaluate (EXECUTE_JS) is a recorded, timeline-visible action and must get a screenshot
    like clicks and navigations do, so the run detail panel can render it instead of "No screenshot"."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("await page.evaluate('() => document.title')", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    actions = _created_actions(mocks)
    assert [a.action_type for a in actions] == [ActionType.EXECUTE_JS]
    assert mocks["create_artifact"].await_count == 1
    assert actions[0].screenshot_artifact_id == "artifact_1"


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
async def test_create_task_failure_does_not_fail_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recording is best-effort: a DB hiccup creating the container task must not fail the block, and
    with no task the recorder degrades to in-memory only (no orphaned actions or screenshots)."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)
    mocks["create_task_and_step"].side_effect = RuntimeError("db unavailable")

    block = _make_code_block("await page.locator('#go').click()\nvalue = 'ok'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert result.output_parameter_value is not None
    assert result.output_parameter_value["value"] == "ok"
    assert mocks["create_action"].await_count == 0
    assert mocks["create_artifact"].await_count == 0


@pytest.mark.asyncio
async def test_link_block_failure_fails_task_and_disables_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task created but not linked to the run block must not stay running or receive orphan actions."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    mocks = _patch_execute_environment(monkeypatch, page, context)

    async def fail_link_only(**kwargs: object) -> None:
        if kwargs.get("task_id") is not None:
            raise RuntimeError("db unavailable")

    mocks["update_workflow_run_block"].side_effect = fail_link_only

    block = _make_code_block("await page.locator('#go').click()\nvalue = 'ok'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert mocks["create_action"].await_count == 0
    assert mocks["create_artifact"].await_count == 0
    assert [call.kwargs.get("status") for call in mocks["update_task"].await_args_list] == [TaskStatus.failed]
    assert [call.kwargs.get("status") for call in mocks["update_step"].await_args_list] == [StepStatus.failed]


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


def test_json_safe_recorder_output_normalizes_leaked_locator_wrappers() -> None:
    """SKY-12272: a leaked recorder proxy in a code block's output must collapse to a JSON-safe
    marker, never a raw proxy that raises TypeError at the registration boundary."""
    recorder = _Recorder(None)
    locator = RecordingLocator(FakeLocator(), recorder, "#invoice-link")
    keyboard = RecordingKeyboard(SimpleNamespace(), recorder)

    result = {
        "link": locator,
        "name": "Invoice_2026.pdf",
        "rows": [locator, {"nested": locator}],
        "kb": keyboard,
    }

    safe = json_safe_recorder_output(result)

    # The whole point: serializes with NO default= fallback. A raw wrapper raises TypeError here.
    json.dumps(safe)
    assert safe["name"] == "Invoice_2026.pdf"  # sibling field never starved
    assert safe["link"] == "<RecordingLocator>"  # leaked locator -> type marker, not its selector
    assert safe["rows"][0] == "<RecordingLocator>"  # nested inside a list
    assert safe["rows"][1]["nested"] == "<RecordingLocator>"  # nested inside a dict
    assert safe["kb"] == "<RecordingKeyboard>"  # non-locator proxy -> its own marker


def test_json_safe_recorder_output_normalizes_leaked_locator_used_as_key() -> None:
    """json.dumps rejects a non-primitive mapping key outright (default= is never consulted for
    keys), so a leaked proxy key must be normalized too, not just values."""
    locator = RecordingLocator(FakeLocator(), _Recorder(None), "#doc")
    safe = json_safe_recorder_output({locator: "delivered"})
    json.dumps(safe)  # a raw locator key raises TypeError: keys must be str/int/float/bool/None
    assert safe == {"<RecordingLocator>": "delivered"}


def test_json_safe_recorder_output_never_leaks_a_secret_bearing_selector() -> None:
    """A resolved credential can end up in a locator selector; mask_secrets_in_data scrubs dict
    values, not keys, so the marker must not carry the selector at all — as a value or a key."""
    secret = "s3cr3t-token"
    recorder = _Recorder(None)
    as_value = RecordingLocator(FakeLocator(), recorder, f"text={secret}")
    as_key = RecordingLocator(FakeLocator(), recorder, f"#{secret}")

    safe = json_safe_recorder_output({"field": as_value, as_key: "delivered"})

    assert secret not in json.dumps(safe)


def test_json_safe_recorder_output_passes_through_plain_data() -> None:
    payload = {"a": 1, "b": ["x", {"c": True, "d": None}], "e": 3.5}
    assert json_safe_recorder_output(payload) == payload


@pytest.mark.asyncio
async def test_code_block_output_registers_leaked_locator_as_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    """SKY-12272 end-to-end: a code block that leaves a locator in a local variable registers a
    JSON-safe output (selector string), and sibling fields survive rather than dropping the payload."""
    page = FakePage()
    context = FakeWorkflowRunContext()
    _patch_execute_environment(monkeypatch, page, context)

    block = _make_code_block("link = page.locator('#invoice-link')\nname = 'Invoice_2026.pdf'", goal="go")
    result = await block.execute(workflow_run_id="wr_test", workflow_run_block_id="wrb_test", organization_id="o_test")

    assert result.success is True
    assert result.output_parameter_value is not None
    json.dumps(result.output_parameter_value)  # registration payload is JSON-safe
    assert result.output_parameter_value["name"] == "Invoice_2026.pdf"  # sibling preserved
    assert result.output_parameter_value["link"] == "<RecordingLocator>"  # locator normalized, not a raw proxy
