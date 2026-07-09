"""Bounded runtime self-heal escalation for a failed copilot-authored code block.

block.prompt is the operative goal; a confidently-matched step only narrows it, so a rotted
selector heals even when no step covers the failing line (the common case).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.forge import app
from skyvern.forge.agent_functions import CodeBlockEngineFailure, CodeBlockEngineResult
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import BlockResult, BlockStatus, BlockType, CodeBlock, CodeBlockStep
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.self_heal import HealSkipReason
from skyvern.webeye.actions.actions import Action

SECRET_VALUE = "hunter2-super-secret"
DEFAULT_PROMPT = "Log in and download the report"

ExtractedInformation = list[Any] | dict[str, Any] | str | None


def _make_code_block(
    steps: list[CodeBlockStep] | None = None,
    prompt: str | None = DEFAULT_PROMPT,
    code: str = "await page.click('#missing')",
) -> CodeBlock:
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
    return CodeBlock(
        label="code_1",
        code=code,
        prompt=prompt,
        steps=steps,
        output_parameter=output_parameter,
    )


def _make_context(
    *,
    with_secret: bool = False,
    enable_self_healing: bool | None = None,
    created_by: str | None = "copilot",
    edited_by: str | None = None,
) -> WorkflowRunContext:
    context = WorkflowRunContext(
        workflow_title="wf",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
    )
    if with_secret:
        context.secrets["k_secret"] = SECRET_VALUE
        context.include_secrets_in_templates = True
    if enable_self_healing is not None:
        context.workflow = SimpleNamespace(
            enable_self_healing=enable_self_healing,
            workflow_definition=None,
            created_by=created_by,
            edited_by=edited_by,
            workflow_permanent_id="wpid_test",
            organization_id="o_test",
        )
    return context


class _FakeTask:
    def __init__(
        self,
        task_id: str,
        status: TaskStatus,
        extracted_information: ExtractedInformation = None,
    ) -> None:
        self.task_id = task_id
        self.status = status
        self.extracted_information: ExtractedInformation = (
            extracted_information if extracted_information is not None else {"report": "ok"}
        )
        self.failure_reason: str | None = None if status == TaskStatus.completed else "agent gave up"
        self.errors: list[dict[str, Any]] = []
        self.failure_category: list[dict[str, Any]] | None = None
        self.order = 7
        self.retry = 0


def _install_db_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    final_status: TaskStatus,
    organization: object | None = SimpleNamespace(organization_id="o_test", max_steps_per_run=None),
    downloaded_files: list[FileInfo] | None = None,
    extracted_information: ExtractedInformation = None,
    copilot_lineage: bool = False,
) -> dict[str, Any]:
    created_task = _FakeTask("tsk_escalation", TaskStatus.running)
    updated_after_run = _FakeTask("tsk_escalation", final_status, extracted_information=extracted_information)
    state: dict[str, Any] = {
        "create_task_kwargs": None,
        "execute_step_calls": 0,
        "created_actions": [],
        "recovery_block_kwargs": None,
        "recovery_block_updates": [],
        "workflow_run_block_updates": [],
    }

    async def _create_task(**kwargs: object) -> _FakeTask:
        state["create_task_kwargs"] = kwargs
        return created_task

    async def _update_task(*args: object, **kwargs: object) -> _FakeTask:
        return created_task

    async def _create_step(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(step_id="stp_1", order=0, retry_index=0)

    async def _get_task(*args: object, **kwargs: object) -> _FakeTask:
        return updated_after_run

    async def _get_organization(*args: object, **kwargs: object) -> object | None:
        return organization

    async def _get_task_order(*args: object, **kwargs: object) -> tuple[int, int]:
        return 8, 0

    async def _execute_step(*args: object, **kwargs: object) -> tuple[None, None, None]:
        state["execute_step_calls"] += 1
        state["execute_step_kwargs"] = kwargs
        return None, None, None

    async def _get_downloaded_files(*args: object, **kwargs: object) -> list[FileInfo]:
        return list(downloaded_files or [])

    async def _create_action(action: Action) -> Action:
        state["created_actions"].append(action)
        return action

    monkeypatch.setattr(app.DATABASE.tasks, "create_task", AsyncMock(side_effect=_create_task))
    monkeypatch.setattr(app.DATABASE.tasks, "update_task", AsyncMock(side_effect=_update_task))
    monkeypatch.setattr(app.DATABASE.tasks, "update_step", AsyncMock(return_value=None))
    monkeypatch.setattr(app.DATABASE.tasks, "create_step", AsyncMock(side_effect=_create_step))
    monkeypatch.setattr(app.DATABASE.tasks, "get_task", AsyncMock(side_effect=_get_task))
    monkeypatch.setattr(app.DATABASE.organizations, "get_organization", AsyncMock(side_effect=_get_organization))
    monkeypatch.setattr(app.DATABASE.workflows, "is_workflow_copilot_authored", AsyncMock(return_value=copilot_lineage))
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.BaseTaskBlock.get_task_order",
        AsyncMock(side_effect=_get_task_order),
    )
    monkeypatch.setattr(app.agent, "execute_step", AsyncMock(side_effect=_execute_step))
    monkeypatch.setattr(app.DATABASE.workflow_params, "create_action", AsyncMock(side_effect=_create_action))

    async def _create_workflow_run_block(**kwargs: object) -> SimpleNamespace:
        state["recovery_block_kwargs"] = kwargs
        return SimpleNamespace(workflow_run_block_id="wrb_recovery")

    async def _update_workflow_run_block(**kwargs: object) -> None:
        state["workflow_run_block_updates"].append(kwargs)
        if kwargs.get("workflow_run_block_id") == "wrb_recovery":
            state["recovery_block_updates"].append(kwargs)

    monkeypatch.setattr(
        app.DATABASE.observer, "create_workflow_run_block", AsyncMock(side_effect=_create_workflow_run_block)
    )
    monkeypatch.setattr(
        app.DATABASE.observer, "update_workflow_run_block", AsyncMock(side_effect=_update_workflow_run_block)
    )
    monkeypatch.setattr(app.STORAGE, "get_downloaded_files", AsyncMock(side_effect=_get_downloaded_files))
    monkeypatch.setattr(
        app.DATABASE.workflow_runs, "create_or_update_workflow_run_output_parameter", AsyncMock(return_value=None)
    )
    return state


def _recording_page(exception: Exception | None, *, url: object = "http://example.test/home") -> MagicMock:
    page = MagicMock()
    page.last_recorded_exception = MagicMock(return_value=exception)
    page.url = url
    return page


def _browser_state() -> MagicMock:
    browser_state = MagicMock()
    browser_state.navigate_to_url = AsyncMock(return_value=None)
    return browser_state


class FakeRecorder:
    instances: list[FakeRecorder] = []
    _next_last_exception: Exception | None = None

    @classmethod
    def reset(cls, *, last_recorded_exception: Exception | None = None) -> None:
        cls.instances = []
        cls._next_last_exception = last_recorded_exception

    def __init__(self, **kwargs: Any) -> None:
        self.recording_page = MagicMock()
        self.recording_page.last_recorded_exception = MagicMock(return_value=self._next_last_exception)
        self._actions: list[Any] = []
        self.finalized_success: bool | None = None
        self.__class__.instances.append(self)

    async def create_task_and_step(self) -> None:
        return None

    async def link_block(self) -> None:
        return None

    def recorded_actions(self) -> list[Any]:
        return list(self._actions)

    def last_recorded_exception(self) -> Exception | None:
        return self._next_last_exception

    async def persist(self, actions: list[Any]) -> None:
        return None

    async def finalize(self, success: bool) -> None:
        if self.finalized_success is None:
            self.finalized_success = success


async def _heal(
    block: CodeBlock,
    context: WorkflowRunContext,
    exception: Exception,
    recording_page: MagicMock,
    *,
    failing_line: int | None = 1,
    browser_state: MagicMock | None = None,
    page: MagicMock | None = None,
) -> BlockResult | None:
    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        return await block._attempt_self_heal(
            exception=exception,
            failing_line=failing_line,
            recording_page=recording_page,
            workflow_run_context=context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id=None,
            browser_state=browser_state if browser_state is not None else _browser_state(),
            page=page if page is not None else MagicMock(),
        )


def _statuses_for_block(
    state: dict[str, Any],
    workflow_run_block_id: str,
) -> list[BlockStatus]:
    statuses: list[BlockStatus] = []
    for update in state["workflow_run_block_updates"]:
        if update.get("workflow_run_block_id") != workflow_run_block_id:
            continue
        status = update.get("status")
        if isinstance(status, BlockStatus):
            statuses.append(status)
    return statuses


def _patch_execute_chokepoint_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    context: WorkflowRunContext,
    fake_browser_state: object,
    use_codeblock_runner: bool,
) -> None:
    monkeypatch.setattr(CodeBlock, "get_workflow_run_context", MagicMock(return_value=context))
    monkeypatch.setattr(CodeBlock, "get_or_create_browser_state", AsyncMock(return_value=fake_browser_state))
    monkeypatch.setattr(CodeBlock, "_ensure_run_recording_artifact", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "format_potential_template_parameters", MagicMock(return_value=None))
    monkeypatch.setattr(app.AGENT_FUNCTION, "validate_code_block", AsyncMock(return_value=None))
    monkeypatch.setattr(app.AGENT_FUNCTION, "should_use_codeblock_runner", AsyncMock(return_value=use_codeblock_runner))
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", MagicMock(return_value=fake_browser_state))


@pytest.mark.asyncio
async def test_everything_off_is_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(enable_self_healing=False), exc, _recording_page(exc))

    assert result is None
    assert state["execute_step_calls"] == 0


@pytest.mark.asyncio
async def test_workflow_setting_enables_heal_when_env_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(enable_self_healing=True), exc, _recording_page(exc))

    assert result is not None
    assert state["execute_step_calls"] == 1


@pytest.mark.asyncio
async def test_non_copilot_workflow_never_heals_from_the_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")
    context = _make_context(enable_self_healing=True, created_by="user@example.com", edited_by=None)

    result = await _heal(block, context, exc, _recording_page(exc))

    assert result is None
    assert state["execute_step_calls"] == 0


@pytest.mark.asyncio
async def test_user_saved_copilot_workflow_heals_via_lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    """User saves re-stamp created_by/edited_by with the user id; the lineage scan must still
    recognize a copilot-authored workflow."""
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, copilot_lineage=True)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")
    context = _make_context(enable_self_healing=True, created_by="user@example.com", edited_by="user@example.com")

    result = await _heal(block, context, exc, _recording_page(exc))

    assert result is not None
    assert state["execute_step_calls"] == 1


@pytest.mark.asyncio
async def test_lineage_lookup_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lineage scan runs inside the block's exception handler; a DB failure there must
    fail closed (no heal) instead of masking the original block failure with a new raise."""
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    monkeypatch.setattr(
        app.DATABASE.workflows,
        "is_workflow_copilot_authored",
        AsyncMock(side_effect=RuntimeError("db unavailable")),
    )
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")
    context = _make_context(enable_self_healing=True, created_by="user@example.com", edited_by="user@example.com")

    result = await _heal(block, context, exc, _recording_page(exc))

    assert result is None
    assert state["execute_step_calls"] == 0


@pytest.mark.asyncio
async def test_copilot_edited_workflow_heals_from_the_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")
    context = _make_context(enable_self_healing=True, created_by="user@example.com", edited_by="copilot")

    result = await _heal(block, context, exc, _recording_page(exc))

    assert result is not None
    assert state["execute_step_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("env_enabled", [True, False])
async def test_missing_workflow_on_context_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch, env_enabled: bool
) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", env_enabled, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert (result is not None) is env_enabled
    assert state["execute_step_calls"] == (1 if env_enabled else 0)


# --- The spine fix: a rotted page failure heals on block.prompt even with no covering step. ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "steps, failing_line",
    [
        pytest.param(None, 1, id="no_steps"),
        pytest.param([CodeBlockStep(description="d", line_start=50, line_end=60)], 1, id="unmatched_step"),
        pytest.param([CodeBlockStep(description="d", line_start=1, line_end=1)], None, id="null_failing_line"),
        pytest.param([CodeBlockStep(description=None, line_start=1, line_end=1)], 1, id="step_without_description"),
    ],
)
async def test_prompt_only_heal_fires_without_a_matched_step(
    monkeypatch: pytest.MonkeyPatch, steps: list[CodeBlockStep] | None, failing_line: int | None
) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=steps)
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=failing_line)

    assert result is not None and result.success is True
    assert state["execute_step_calls"] == 1
    # No step narrows the goal, so it is the bare block prompt (no MINI_GOAL wrapper).
    assert state["create_task_kwargs"]["navigation_goal"] == DEFAULT_PROMPT


@pytest.mark.asyncio
async def test_matched_step_narrows_the_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="click the export button", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=1)

    assert result is not None and result.success is True
    goal = state["create_task_kwargs"]["navigation_goal"]
    assert goal != DEFAULT_PROMPT
    assert "click the export button" in goal
    assert DEFAULT_PROMPT in goal


@pytest.mark.asyncio
async def test_failing_static_goto_sets_escalation_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(code=f'await page.goto("{target_url}")')
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == target_url


@pytest.mark.asyncio
async def test_failing_goto_with_variable_keeps_empty_escalation_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        code="""
target = "https://dead-nav.example.com/login"
await page.goto(target)
""".strip()
    )
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=2)

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == ""


@pytest.mark.asyncio
async def test_failing_goto_with_keyword_url_sets_escalation_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(code=f'await page.goto(url="{target_url}")')
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == target_url


@pytest.mark.asyncio
async def test_failing_goto_with_dynamic_keyword_url_keeps_empty_escalation_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        code="""
target = "https://dead-nav.example.com/login"
await page.goto(url=target)
""".strip()
    )
    exc = RuntimeError("navigation failed")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=2)

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == ""


@pytest.mark.asyncio
async def test_element_rot_failure_never_sets_escalation_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(code="await page.click('#download')")
    exc = RuntimeError("click failed")

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc, url="https://app.example.com/dashboard"),
    )

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == ""


@pytest.mark.asyncio
async def test_error_page_seat_uses_first_static_goto_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(
        code=f"""
await page.goto("{target_url}")
await page.click("#download")
""".strip()
    )
    exc = RuntimeError("click failed after dead nav")

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc, url="chrome-error://chromewebdata/"),
        failing_line=2,
    )

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == target_url


@pytest.mark.asyncio
async def test_error_page_seat_skips_dynamic_goto_to_reach_later_static_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(
        code=f"""
target = build_url()
await page.goto(target)
await page.click("#step1")
await page.goto("{target_url}")
await page.click("#download")
""".strip()
    )
    exc = RuntimeError("click failed after dead nav")

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc, url="chrome-error://chromewebdata/"),
        failing_line=5,
    )

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == target_url


@pytest.mark.asyncio
async def test_error_page_seat_uses_nearest_preceding_goto_not_first_in_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-navigation block must seat recovery on the page the failure actually landed on
    (the nearest preceding goto), not the first static goto anywhere in the block — an earlier
    stale goto and a not-yet-executed later goto are both wrong answers here."""
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    stale_url = "https://stale-first.example.com/old"
    correct_url = "https://correct-recovery.example.com/target"
    never_executed_url = "https://never-executed.example.com/later"
    block = _make_code_block(
        code=f"""
await page.goto("{stale_url}")
await page.click("#step1")
await page.goto("{correct_url}")
await page.click("#step2")
await page.goto("{never_executed_url}")
""".strip()
    )
    exc = RuntimeError("click failed after dead nav")

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc, url="chrome-error://chromewebdata/"),
        failing_line=4,
    )

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["url"] == correct_url


@pytest.mark.asyncio
async def test_dead_nav_seat_navigates_live_page_before_escalation_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """escalation_task.url alone does not reliably trigger navigation (BrowserManager can early-return
    a cached browser state without reading it), so the heal must drive the live browser_state/page
    directly for a dead-nav seat."""
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(code=f'await page.goto("{target_url}")')
    exc = RuntimeError("navigation failed")
    browser_state = _browser_state()
    live_page = MagicMock(name="live_page")

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc),
        browser_state=browser_state,
        page=live_page,
    )

    assert result is not None and result.success is True
    browser_state.navigate_to_url.assert_awaited_once_with(page=live_page, url=target_url)


@pytest.mark.asyncio
async def test_dead_host_goto_recovers_url_from_goal_not_the_rotted_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical dead-nav rot: the block's own goto url is the dead host, and the real
    destination lives in the goal (prompt/steps). The heal must navigate to the goal's URL, not
    re-navigate to the code's dead goto (gauntlet H7 — otherwise it just hits the dead host again)."""
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    real_url = "http://localhost:8900/telco_billing/northwind/"
    block = _make_code_block(
        code='await page.goto("http://localhost:65531/")',
        prompt=f"Open the billing portal at {real_url} and sign in",
    )
    exc = RuntimeError("net::ERR_CONNECTION_REFUSED")
    browser_state = _browser_state()
    live_page = MagicMock(name="live_page")

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc),
        browser_state=browser_state,
        page=live_page,
    )

    assert result is not None and result.success is True
    browser_state.navigate_to_url.assert_awaited_once_with(page=live_page, url=real_url)


@pytest.mark.asyncio
async def test_element_rot_seat_never_navigates_live_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Element-rot heals must preserve same-session SPA state (gauntlet H8 invariant); the direct
    navigate call added for dead-nav seats must not fire when no recovery URL was derived."""
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(code="await page.click('#download')")
    exc = RuntimeError("click failed")
    browser_state = _browser_state()

    result = await _heal(
        block,
        _make_context(),
        exc,
        _recording_page(exc, url="https://app.example.com/dashboard"),
        browser_state=browser_state,
    )

    assert result is not None and result.success is True
    browser_state.navigate_to_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_derived_escalation_url_is_not_added_to_goal_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    target_url = "https://dead-nav.example.com/login"
    block = _make_code_block(code=f'await page.goto("{target_url}")')
    exc = RuntimeError("navigation failed")

    state_with_url = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    result_with_url = await _heal(block, _make_context(), exc, _recording_page(exc))
    goal_with_url = state_with_url["create_task_kwargs"]["navigation_goal"]

    assert result_with_url is not None and result_with_url.success is True
    assert target_url not in goal_with_url

    element_rot_block = _make_code_block(code="await page.click('#missing')")
    state_no_url = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    result_no_url = await _heal(
        element_rot_block,
        _make_context(),
        exc,
        _recording_page(exc, url="https://app.example.com/dashboard"),
    )

    assert result_no_url is not None and result_no_url.success is True
    assert state_no_url["create_task_kwargs"]["url"] == ""
    assert goal_with_url == state_no_url["create_task_kwargs"]["navigation_goal"]


@pytest.mark.asyncio
async def test_goto_inside_comment_or_string_never_sets_escalation_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    for code in (
        'await page.click("#download")  # retry via .goto("https://evil.example.com")',
        "await page.click('.goto(\"https://evil.example.com\")')",
    ):
        block = _make_code_block(code=code)
        exc = RuntimeError("click failed")
        state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)

        result = await _heal(block, _make_context(), exc, _recording_page(exc))

        assert result is not None and result.success is True
        assert state["create_task_kwargs"]["url"] == ""


@pytest.mark.asyncio
async def test_unmapped_playwright_error_is_healed(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unmapped Playwright call's exception is never registered as last_recorded_exception, but a
    # Playwright page error is still genuine page drift the type-classifier must catch (CORR-10).
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = PlaywrightTimeoutError("locator.click: Timeout 30000ms exceeded")

    result = await _heal(block, _make_context(), exc, _recording_page(None))

    assert result is not None and result.success is True
    assert state["execute_step_calls"] == 1


@pytest.mark.asyncio
async def test_deliberate_raise_is_not_healed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    raised = ValueError("business logic refused")

    result = await _heal(block, _make_context(), raised, _recording_page(None))

    assert result is None
    assert state["execute_step_calls"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, organization",
    [
        pytest.param({"prompt": None}, SimpleNamespace(organization_id="o_test"), id="no_prompt"),
        pytest.param({}, None, id="no_organization"),
    ],
)
async def test_no_op_guards_skip_escalation(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], organization: object | None
) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, organization=organization)
    block = _make_code_block(steps=[CodeBlockStep(description="d", line_start=1, line_end=1)], **kwargs)
    exc = RuntimeError("rotted selector")

    assert await _heal(block, _make_context(), exc, _recording_page(exc)) is None
    assert state["execute_step_calls"] == 0


@pytest.mark.asyncio
async def test_completed_heal_maps_to_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    block = _make_code_block(steps=[CodeBlockStep(description="download the report", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None
    assert result.success is True
    assert result.status == BlockStatus.completed
    assert state["execute_step_calls"] == 1
    record.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_heal_records_task_output_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    _install_db_fakes(
        monkeypatch,
        final_status=TaskStatus.completed,
        extracted_information={"order_total": "42.50", "currency": "USD"},
    )
    block = _make_code_block(steps=[CodeBlockStep(description="read the order total", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None
    output = result.output_parameter_value
    assert output["extracted_information"] == {"order_total": "42.50", "currency": "USD"}
    assert output["task_id"] == "tsk_escalation"
    assert output["status"] == TaskStatus.completed
    recorded = record.await_args.args[2]
    assert recorded["extracted_information"] == {"order_total": "42.50", "currency": "USD"}


@pytest.mark.asyncio
async def test_completed_heal_carries_downloaded_files_into_task_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    downloaded = [FileInfo(url="https://files.test/report.pdf", checksum="abc123", artifact_id="art_1")]
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed, downloaded_files=downloaded)
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    block = _make_code_block(steps=[CodeBlockStep(description="download the report", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    output = result.output_parameter_value
    assert output["downloaded_file_urls"] == ["https://files.test/report.pdf"]
    assert output["downloaded_file_artifact_ids"] == ["art_1"]
    assert output["downloaded_files"][0]["url"] == "https://files.test/report.pdf"
    recorded_output = record.await_args.args[2]
    assert recorded_output["downloaded_file_urls"] == ["https://files.test/report.pdf"]


@pytest.mark.parametrize(
    "final_status, expected_status",
    [
        (TaskStatus.terminated, BlockStatus.terminated),
        (TaskStatus.timed_out, BlockStatus.timed_out),
        (TaskStatus.canceled, BlockStatus.canceled),
        (TaskStatus.failed, BlockStatus.failed),
    ],
)
@pytest.mark.asyncio
async def test_non_completed_heal_maps_status_without_collapsing(
    monkeypatch: pytest.MonkeyPatch, final_status: TaskStatus, expected_status: BlockStatus
) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=final_status)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None
    assert result.success is False
    assert result.status == expected_status
    assert state["execute_step_calls"] == 1


@pytest.mark.asyncio
async def test_escalation_runs_its_own_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert state["execute_step_kwargs"]["task"].task_id == "tsk_escalation"
    assert state["created_actions"] == []


@pytest.mark.asyncio
async def test_secret_value_never_leaks_into_goal_or_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    block = _make_code_block(
        steps=[CodeBlockStep(description=f"submit token {SECRET_VALUE}", line_start=1, line_end=1)],
        prompt=f"Sign in with {SECRET_VALUE} and download",
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(with_secret=True), exc, _recording_page(exc))

    assert result is not None
    goal = state["create_task_kwargs"]["navigation_goal"]
    assert SECRET_VALUE not in goal
    assert "*****" in goal
    for value in state["create_task_kwargs"].values():
        assert SECRET_VALUE not in str(value)


@pytest.mark.asyncio
async def test_completed_heal_masks_secret_in_recorded_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    record = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record)
    _install_db_fakes(
        monkeypatch,
        final_status=TaskStatus.completed,
        extracted_information={"token": SECRET_VALUE},
    )
    block = _make_code_block(steps=[CodeBlockStep(description="read the token", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(with_secret=True), exc, _recording_page(exc))

    assert result is not None
    recorded = record.await_args.args[2]
    assert SECRET_VALUE not in str(recorded)
    assert SECRET_VALUE not in str(result.output_parameter_value)


@pytest.mark.asyncio
async def test_max_steps_and_model_and_running_status_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr("skyvern.config.settings.MAX_STEPS_PER_RUN", 7, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    update_calls: list[dict[str, object]] = []
    original_update = app.DATABASE.tasks.update_task

    async def _track_update(*args: object, **kwargs: object) -> object:
        update_calls.append(kwargs)
        return await original_update(*args, **kwargs)

    monkeypatch.setattr(app.DATABASE.tasks, "update_task", AsyncMock(side_effect=_track_update))
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    block.model = {"model_name": "gpt-5.5"}
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None
    assert state["create_task_kwargs"]["max_steps_per_run"] == 7
    assert state["create_task_kwargs"]["model"] == {"model_name": "gpt-5.5"}
    assert state["create_task_kwargs"]["order"] == 8
    assert any(call.get("status") == TaskStatus.running for call in update_calls)
    assert state["execute_step_kwargs"]["task_block"] is None


@pytest.mark.asyncio
async def test_heal_internal_exception_degrades_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    monkeypatch.setattr(app.DATABASE.tasks, "create_task", AsyncMock(side_effect=RuntimeError("db down")))
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is None


@pytest.mark.asyncio
async def test_escalation_task_finalized_when_execute_step_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    update_calls: list[dict[str, object]] = []
    original_update = app.DATABASE.tasks.update_task

    async def _track_update(*args: object, **kwargs: object) -> object:
        update_calls.append(kwargs)
        return await original_update(*args, **kwargs)

    monkeypatch.setattr(app.DATABASE.tasks, "update_task", AsyncMock(side_effect=_track_update))
    monkeypatch.setattr(app.agent, "execute_step", AsyncMock(side_effect=RuntimeError("agent boom")))
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is None
    # The escalation task was set running, then finalized to failed on cleanup — never left stranded.
    assert any(call.get("status") == TaskStatus.failed for call in update_calls)


@pytest.mark.asyncio
async def test_escalation_task_finalized_when_not_final(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    _install_db_fakes(monkeypatch, final_status=TaskStatus.running)
    update_calls: list[dict[str, object]] = []
    original_update = app.DATABASE.tasks.update_task

    async def _track_update(*args: object, **kwargs: object) -> object:
        update_calls.append(kwargs)
        return await original_update(*args, **kwargs)

    monkeypatch.setattr(app.DATABASE.tasks, "update_task", AsyncMock(side_effect=_track_update))
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is False
    assert result.status == BlockStatus.failed
    assert any(call.get("status") == TaskStatus.failed for call in update_calls)


@pytest.mark.asyncio
async def test_lone_line_start_step_is_matched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    block = _make_code_block(steps=[CodeBlockStep(description="open the menu", line_start=3, line_end=None)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=9)

    assert result is not None
    assert state["execute_step_calls"] == 1
    assert "open the menu" in state["create_task_kwargs"]["navigation_goal"]


def test_match_step_picks_largest_preceding_start() -> None:
    block = _make_code_block(
        steps=[
            CodeBlockStep(description="first", line_start=1, line_end=3),
            CodeBlockStep(description="second", line_start=5, line_end=None),
            CodeBlockStep(description="third", line_start=8, line_end=12),
        ]
    )
    assert block._match_step_for_failing_line(2).description == "first"
    assert block._match_step_for_failing_line(6).description == "second"
    assert block._match_step_for_failing_line(10).description == "third"
    assert block._match_step_for_failing_line(4) is None


@pytest.mark.asyncio
async def test_heal_max_steps_capped_by_org(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr("skyvern.config.settings.MAX_STEPS_PER_RUN", 25, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    # Org caps runs at 4 steps; the heal must not exceed it even though the global default is 25.
    state = _install_db_fakes(
        monkeypatch,
        final_status=TaskStatus.completed,
        organization=SimpleNamespace(organization_id="o_test", max_steps_per_run=4),
    )
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["max_steps_per_run"] == 4


@pytest.mark.asyncio
async def test_cancellation_finalizes_escalation_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    update_calls: list[dict[str, object]] = []
    original_update = app.DATABASE.tasks.update_task

    async def _track_update(*args: object, **kwargs: object) -> object:
        update_calls.append(kwargs)
        return await original_update(*args, **kwargs)

    monkeypatch.setattr(app.DATABASE.tasks, "update_task", AsyncMock(side_effect=_track_update))
    monkeypatch.setattr(app.agent, "execute_step", AsyncMock(side_effect=asyncio.CancelledError()))
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    with pytest.raises(asyncio.CancelledError):
        await _heal(block, _make_context(), exc, _recording_page(exc))

    # CancelledError (BaseException) must not strand the escalation task: finalized failed, then re-raised.
    assert any(call.get("status") == TaskStatus.failed for call in update_calls)


@pytest.mark.asyncio
async def test_recovery_child_block_created_and_finalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    # blocker #2: the heal takes over the live half-mutated page — the escalation task carries no url.
    assert state["create_task_kwargs"]["url"] == ""
    # blocker #1: a child block parented to the code block surfaces the recovery on the run timeline.
    rb = state["recovery_block_kwargs"]
    assert rb["parent_workflow_run_block_id"] == "wrb_test"
    assert rb["task_id"] == "tsk_escalation"
    assert rb["block_type"] == BlockType.TASK
    assert rb["label"]
    # the recovery block is finalized to the heal outcome, not left dangling in `running`.
    assert any(u.get("status") == BlockStatus.completed for u in state["recovery_block_updates"])


@pytest.mark.asyncio
async def test_recovery_block_finalized_to_non_completed_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.terminated)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is False
    assert any(u.get("status") == BlockStatus.terminated for u in state["recovery_block_updates"])


@pytest.mark.asyncio
async def test_mid_block_failure_composes_remaining_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        steps=[
            CodeBlockStep(description="open the portal", line_start=1, line_end=1),
            CodeBlockStep(description="click the invoices tab", line_start=2, line_end=2),
            CodeBlockStep(description="download the latest invoice", line_start=3, line_end=3),
        ]
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=2)

    assert result is not None and result.success is True
    goal = state["create_task_kwargs"]["navigation_goal"]
    assert "click the invoices tab" in goal
    assert "Then: download the latest invoice" in goal
    # steps before the failure already ran as code — they must not be re-demanded.
    assert "open the portal" not in goal
    assert DEFAULT_PROMPT in goal


@pytest.mark.asyncio
async def test_last_step_failure_keeps_single_step_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        steps=[
            CodeBlockStep(description="open the portal", line_start=1, line_end=1),
            CodeBlockStep(description="download the latest invoice", line_start=2, line_end=2),
        ]
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=2)

    assert result is not None and result.success is True
    goal = state["create_task_kwargs"]["navigation_goal"]
    assert "download the latest invoice" in goal
    assert "Then:" not in goal


@pytest.mark.asyncio
async def test_remaining_steps_without_descriptions_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        steps=[
            CodeBlockStep(description="click the invoices tab", line_start=1, line_end=1),
            CodeBlockStep(description=None, line_start=2, line_end=2),
        ]
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc), failing_line=1)

    assert result is not None and result.success is True
    goal = state["create_task_kwargs"]["navigation_goal"]
    assert "click the invoices tab" in goal
    assert "Then:" not in goal


@pytest.mark.asyncio
async def test_remaining_step_descriptions_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(
        steps=[
            CodeBlockStep(description="open the portal", line_start=1, line_end=1),
            CodeBlockStep(description=f"submit token {SECRET_VALUE}", line_start=2, line_end=2),
        ]
    )
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(with_secret=True), exc, _recording_page(exc), failing_line=1)

    assert result is not None and result.success is True
    goal = state["create_task_kwargs"]["navigation_goal"]
    assert "open the portal" in goal
    assert "Then:" in goal
    assert SECRET_VALUE not in goal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "steps",
    [
        pytest.param([CodeBlockStep(description="download", line_start=1, line_end=1)], id="matched_step"),
        pytest.param(None, id="bare_prompt"),
    ],
)
async def test_escalation_task_verifies_with_action_history(
    monkeypatch: pytest.MonkeyPatch, steps: list[CodeBlockStep] | None
) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=steps)
    exc = RuntimeError("rotted selector")

    result = await _heal(block, _make_context(), exc, _recording_page(exc))

    assert result is not None and result.success is True
    assert state["create_task_kwargs"]["include_action_history_in_verification"] is True


@pytest.mark.parametrize(
    ("error_code", "healable", "skip_reason"),
    [
        ("unsupported_page_operation", True, None),
        ("timeout", False, HealSkipReason.timeout_class),
        ("insecure_code_detected", False, HealSkipReason.insecure_code),
        ("browser_disconnected", False, HealSkipReason.unclassifiable),
        ("user_code_error", False, HealSkipReason.unclassifiable),
        ("busy", False, HealSkipReason.unclassifiable),
    ],
)
def test_secure_runner_degraded_classification_from_error_code(
    error_code: str, healable: bool, skip_reason: HealSkipReason | None
) -> None:
    block = _make_code_block()
    classification = block._classify_secure_runner_failure(
        CodeBlockEngineFailure(
            error_code=error_code,
            safe_message=None,
            failure_reason=None,
            exception_class=None,
            failing_line=None,
            healability_hint=None,
        )
    )
    assert classification.healable is healable
    assert classification.skip_reason == skip_reason


def test_secure_runner_classification_uses_playwright_class_when_hint_is_unknown() -> None:
    block = _make_code_block()
    classification = block._classify_secure_runner_failure(
        CodeBlockEngineFailure(
            error_code="busy",
            safe_message=None,
            failure_reason=None,
            exception_class="playwright._impl._errors.TimeoutError",
            failing_line=3,
            healability_hint=None,
        )
    )
    assert classification.healable is True
    assert classification.skip_reason is None


def test_secure_runner_classification_treats_browser_disconnected_as_non_healable() -> None:
    block = _make_code_block()
    classification = block._classify_secure_runner_failure(
        CodeBlockEngineFailure(
            error_code="browser_disconnected",
            safe_message=None,
            failure_reason=None,
            exception_class="codeblock.page_operation_broker.BrowserDisconnectedError",
            failing_line=3,
            healability_hint=None,
        )
    )
    assert classification.healable is False
    assert classification.skip_reason == HealSkipReason.unclassifiable


@pytest.mark.asyncio
async def test_self_heal_passes_pre_resolved_browser_state_by_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", True, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    browser_state = object()
    exc = RuntimeError("rotted selector")

    with skyvern_context.scoped(SkyvernContext(organization_id="o_test", workflow_run_id="wr_test")):
        result = await block._attempt_self_heal(
            exception=exc,
            failing_line=1,
            recording_page=_recording_page(exc),
            workflow_run_context=_make_context(enable_self_healing=True),
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id=None,
            browser_state=browser_state,
        )

    assert result is not None
    assert state["execute_step_kwargs"]["pre_resolved_browser_state"] is browser_state


@pytest.mark.asyncio
async def test_legacy_heal_success_skips_failed_write_and_ends_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    context = _make_context(enable_self_healing=True)
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=fake_page))
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=False,
    )
    raised = RuntimeError("rotted selector")
    monkeypatch.setattr(CodeBlock, "execute_user_function_with_timeout", AsyncMock(side_effect=raised))
    monkeypatch.setattr(app.AGENT_FUNCTION, "execute_code_block_override", AsyncMock(return_value=None))
    FakeRecorder.reset(last_recorded_exception=raised)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)

    async def _heal_success(**kwargs: Any) -> BlockResult:
        return await block.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value={"task_id": "tsk_escalation", "status": "completed"},
            status=BlockStatus.completed,
            workflow_run_block_id=kwargs["workflow_run_block_id"],
            organization_id=kwargs["organization_id"],
        )

    heal_mock = AsyncMock(side_effect=_heal_success)
    monkeypatch.setattr(block, "_attempt_self_heal", heal_mock)
    record_output_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output_mock)

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    block_statuses = _statuses_for_block(state, "wrb_test")
    assert result.success is True
    assert heal_mock.await_count == 1
    assert BlockStatus.failed not in block_statuses
    assert block_statuses[-1] == BlockStatus.completed
    assert record_output_mock.await_count == 1
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is True


@pytest.mark.asyncio
async def test_heal_output_write_failure_does_not_finalize_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    context = _make_context(enable_self_healing=True)
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=fake_page))
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=False,
    )
    raised = RuntimeError("rotted selector")
    monkeypatch.setattr(CodeBlock, "execute_user_function_with_timeout", AsyncMock(side_effect=raised))
    monkeypatch.setattr(app.AGENT_FUNCTION, "execute_code_block_override", AsyncMock(return_value=None))
    FakeRecorder.reset(last_recorded_exception=raised)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)

    async def _heal_success(**kwargs: Any) -> BlockResult:
        return await block.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value={"task_id": "tsk_escalation", "status": "completed"},
            status=BlockStatus.completed,
            workflow_run_block_id=kwargs["workflow_run_block_id"],
            organization_id=kwargs["organization_id"],
        )

    monkeypatch.setattr(block, "_attempt_self_heal", AsyncMock(side_effect=_heal_success))
    write_error = RuntimeError("output parameter write failed")
    record_output_mock = AsyncMock(side_effect=write_error)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output_mock)

    with pytest.raises(RuntimeError, match="output parameter write failed"):
        await block.execute(
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="o_test",
            browser_session_id="pbs_test",
        )

    assert record_output_mock.await_count == 1
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is not True


@pytest.mark.asyncio
async def test_secure_heal_success_skips_failed_write_and_ends_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    context = _make_context(enable_self_healing=True)
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=fake_page))
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=True,
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(
            return_value=CodeBlockEngineResult(
                block_result=None,
                failure=CodeBlockEngineFailure(
                    error_code="user_code_error",
                    safe_message=None,
                    failure_reason="CodeBlock failed while running user code.",
                    exception_class="playwright._impl._errors.TimeoutError",
                    failing_line=2,
                    healability_hint=True,
                ),
            )
        ),
    )
    FakeRecorder.reset(last_recorded_exception=None)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)

    async def _heal_success(**kwargs: Any) -> BlockResult:
        return await block.build_block_result(
            success=True,
            failure_reason=None,
            output_parameter_value={"task_id": "tsk_escalation", "status": "completed"},
            status=BlockStatus.completed,
            workflow_run_block_id=kwargs["workflow_run_block_id"],
            organization_id=kwargs["organization_id"],
        )

    heal_mock = AsyncMock(side_effect=_heal_success)
    monkeypatch.setattr(block, "_attempt_self_heal", heal_mock)
    record_output_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output_mock)

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    block_statuses = _statuses_for_block(state, "wrb_test")
    assert result.success is True
    assert heal_mock.await_count == 1
    assert heal_mock.await_args.kwargs["page"] is fake_page
    assert heal_mock.await_args.kwargs["browser_state"] is fake_browser_state
    assert BlockStatus.failed not in block_statuses
    assert block_statuses[-1] == BlockStatus.completed
    assert record_output_mock.await_count == 1
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is True


@pytest.mark.asyncio
async def test_legacy_heal_declined_writes_failed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    context = _make_context(enable_self_healing=True)
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=fake_page))
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=False,
    )
    raised = RuntimeError("rotted selector")
    monkeypatch.setattr(CodeBlock, "execute_user_function_with_timeout", AsyncMock(side_effect=raised))
    monkeypatch.setattr(app.AGENT_FUNCTION, "execute_code_block_override", AsyncMock(return_value=None))
    FakeRecorder.reset(last_recorded_exception=raised)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)
    monkeypatch.setattr(block, "_attempt_self_heal", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    block_statuses = _statuses_for_block(state, "wrb_test")
    assert result.success is False
    assert block_statuses.count(BlockStatus.failed) == 1
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is False


@pytest.mark.asyncio
async def test_secure_heal_declined_writes_failed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block(steps=[CodeBlockStep(description="download", line_start=1, line_end=1)])
    context = _make_context(enable_self_healing=True)
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=fake_page))
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=True,
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(
            return_value=CodeBlockEngineResult(
                block_result=None,
                failure=CodeBlockEngineFailure(
                    error_code="user_code_error",
                    safe_message=None,
                    failure_reason="CodeBlock failed while running user code.",
                    exception_class="playwright._impl._errors.TimeoutError",
                    failing_line=2,
                    healability_hint=True,
                ),
            )
        ),
    )
    FakeRecorder.reset(last_recorded_exception=None)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)
    monkeypatch.setattr(block, "_attempt_self_heal", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    block_statuses = _statuses_for_block(state, "wrb_test")
    assert result.success is False
    assert block_statuses.count(BlockStatus.failed) == 1
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is False


@pytest.mark.asyncio
async def test_secure_unhealable_failure_without_result_builds_failed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block()
    context = _make_context(enable_self_healing=False)
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=fake_page))
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=True,
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(
            return_value=CodeBlockEngineResult(
                block_result=None,
                failure=CodeBlockEngineFailure(
                    error_code="timeout",
                    safe_message="CodeBlock execution timed out.",
                    failure_reason="CodeBlock execution timed out.",
                    exception_class=None,
                    failing_line=None,
                    healability_hint=False,
                ),
            )
        ),
    )
    FakeRecorder.reset(last_recorded_exception=None)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)
    heal_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(block, "_attempt_self_heal", heal_mock)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    block_statuses = _statuses_for_block(state, "wrb_test")
    assert result.success is False
    assert heal_mock.await_count == 0
    assert block_statuses.count(BlockStatus.failed) == 1
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is False


@pytest.mark.asyncio
async def test_secure_runner_missing_block_result_returns_generic_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block()
    context = _make_context(enable_self_healing=True)
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=fake_page))
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=True,
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(return_value=CodeBlockEngineResult(block_result=None, failure=None)),
    )
    FakeRecorder.reset(last_recorded_exception=None)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", AsyncMock(return_value=None))

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    block_statuses = _statuses_for_block(state, "wrb_test")
    assert result.success is False
    assert result.failure_reason == "Secure code block runner returned no result"
    assert block_statuses.count(BlockStatus.failed) == 1
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is False


@pytest.mark.asyncio
async def test_secure_infra_failure_without_metadata_finalizes_failed_and_records_no_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.config.settings.ENABLE_CODE_BLOCK_SELF_HEALING", False, raising=False)
    state = _install_db_fakes(monkeypatch, final_status=TaskStatus.completed)
    block = _make_code_block()
    context = _make_context(enable_self_healing=True)
    fake_page = MagicMock()
    fake_browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=fake_page))
    _patch_execute_chokepoint_environment(
        monkeypatch,
        context=context,
        fake_browser_state=fake_browser_state,
        use_codeblock_runner=True,
    )
    failed_result = await block.build_block_result(
        success=False,
        failure_reason="infra failure",
        output_parameter_value={"raw": "not-recorded"},
        status=BlockStatus.failed,
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "execute_code_block_override",
        AsyncMock(return_value=CodeBlockEngineResult(block_result=failed_result, failure=None)),
    )
    FakeRecorder.reset(last_recorded_exception=None)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.CodeBlockActionRecording", FakeRecorder)
    heal_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(block, "_attempt_self_heal", heal_mock)
    record_output_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output_mock)

    result = await block.execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id="pbs_test",
    )

    block_statuses = _statuses_for_block(state, "wrb_test")
    assert result.success is False
    assert len(FakeRecorder.instances) == 1
    assert FakeRecorder.instances[0].finalized_success is False
    assert record_output_mock.await_count == 0
    assert BlockStatus.completed not in block_statuses
    heal_mock.assert_not_awaited()
