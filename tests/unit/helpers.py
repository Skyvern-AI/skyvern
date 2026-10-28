from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Iterator, Sequence
from unittest.mock import AsyncMock, MagicMock

from playwright.async_api import Error as PlaywrightError
from pytest import MonkeyPatch  # type: ignore[import-not-found]

from skyvern.exceptions import MissingBrowserStatePage, get_user_facing_exception_message
from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.sdk.api.llm import api_handler_factory
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.models import LLMRouterConfig, LLMRouterModelConfig
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.taskv3.loop import JUSTIFY_HOLD_MESSAGE
from skyvern.schemas.steps import AgentStepOutput
from skyvern.utils.action_redaction import REDACTED_INPUT_VALUE, REDACTED_OTP_VALUE
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER
from skyvern.webeye.actions.actions import (
    Action,
    ActionStatus,
    ActionType,
    ClickAction,
    CompleteAction,
    InputOrSelectContext,
    InputTextAction,
    VerificationCodeAction,
)
from skyvern.webeye.actions.responses import ActionFailure, ActionResult, ActionSuccess
from tests.unit.scoped_asyncio import ScopedAsyncio


def held_finish_restated(history: Sequence[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """The finish the justification hold answered in the last model turn, made again as a model with a real reason
    would; an empty turn when the last turn had no held finish."""
    calls: dict[str, Any] = {}
    restated: list[tuple[str, dict[str, Any]]] = []
    for message in history:
        if message.get("role") == "assistant":
            calls = {call["id"]: call["function"] for call in message.get("tool_calls") or []}
            restated = []
        elif message.get("role") == "tool" and message.get("content") == JUSTIFY_HOLD_MESSAGE:
            function = calls.get(message.get("tool_call_id"))
            if function is not None and function.get("name") == "finish":
                restated = [("finish", json.loads(function.get("arguments") or "{}"))]
    return restated


class FakeLLMResponse:
    def __init__(
        self,
        model: str,
        content: str | None = '{"actions": []}',
        finish_reason: str | None = None,
        hidden_params: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self._content = content
        self._hidden_params = hidden_params or {}
        self.choices = [
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(
                    content=content,
                ),
            )
        ]
        self.usage = SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            cache_read_input_tokens=0,
        )

    def model_dump_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "model": self.model,
                "choices": [
                    {"message": {"content": self._content}},
                ],
            },
            indent=indent,
        )


class DummyLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.warnings: list[tuple[str, dict[str, Any]]] = []
        self.exceptions: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: dict[str, Any]) -> None:
        self.events.append((event, kwargs))

    def warning(self, event: str = "", **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))

    def exception(self, event: str = "", **kwargs: Any) -> None:
        self.exceptions.append((event, kwargs))

    def debug(self, *args, **kwargs) -> None:  # pragma: no cover
        pass


def fallback_receipts(logs: Sequence[dict[str, Any]]) -> list[tuple[str, str, str | None]]:
    """(fallback_outcome, initial group, final served group) for each log line the fallback metric counts."""
    counted = (api_handler_factory.LLM_CALL_DURATION_MESSAGE, api_handler_factory.LLM_RETRY_CHAIN_EXHAUSTED_MESSAGE)
    return [
        (log["fallback_outcome"], log["initial_model_group"], log.get("served_model_group"))
        for log in logs
        if log["event"] in counted and "fallback_outcome" in log
    ]


@dataclass
class RouterTestContext:
    llm_key: str
    router_config: LLMRouterConfig
    logger: DummyLogger


@contextmanager
def router_test_context(
    monkeypatch: MonkeyPatch,
    *,
    llm_key: str,
    primary_group: str,
    fallback_group: str,
    routing_strategy: str = "simple-shuffle",
) -> Iterator[RouterTestContext]:
    router_config = LLMRouterConfig(
        model_name="test-router",
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
        model_list=[
            LLMRouterModelConfig(model_name=primary_group, litellm_params={"model": primary_group}),
            LLMRouterModelConfig(model_name=fallback_group, litellm_params={"model": fallback_group}),
        ],
        redis_host="localhost",
        redis_port=6379,
        redis_password="",
        main_model_group=primary_group,
        fallback_model_group=fallback_group,
        routing_strategy=routing_strategy,
        num_retries=0,
        disable_cooldowns=True,
        temperature=None,
    )

    LLMConfigRegistry._configs.pop(llm_key, None)  # type: ignore[attr-defined]
    LLMConfigRegistry.register_config(llm_key, router_config)

    logger = DummyLogger()
    monkeypatch.setattr(api_handler_factory, "LOG", logger)

    async def fake_llm_messages_builder(prompt: str, screenshots, add_assistant_prefix: bool) -> list[dict[str, str]]:
        return [{"role": "user", "content": prompt}]

    monkeypatch.setattr(api_handler_factory, "llm_messages_builder", fake_llm_messages_builder)
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda completion_response: 0.0)

    try:
        yield RouterTestContext(llm_key=llm_key, router_config=router_config, logger=logger)
    finally:
        LLMConfigRegistry._configs.pop(llm_key, None)  # type: ignore[attr-defined]


def unsolved_captcha_relabel_categories() -> tuple[list[dict], list[dict]]:
    """A browser loss's merge-base categories, and the same failure relabeled after an unsolved captcha."""
    browser_loss = MissingBrowserStatePage()
    reason = get_user_facing_exception_message(browser_loss)
    before = classify_from_failure_reason(reason, exception=browser_loss, fallback_to_unknown=True)
    after = classify_from_failure_reason(
        reason, exception=browser_loss, fallback_to_unknown=True, unsolved_captcha_exception="CaptchaNotSolvedInTime"
    )
    assert before is not None and after is not None and after != before
    return before, after


def make_organization(now: datetime) -> Organization:
    return Organization(
        organization_id="org-123",
        organization_name="Org",
        webhook_callback_url=None,
        max_steps_per_run=None,
        max_retries_per_step=None,
        domain=None,
        bw_organization_id=None,
        bw_collection_ids=None,
        created_at=now,
        modified_at=now,
    )


def make_task(now: datetime, organization: Organization, **overrides: Any) -> Task:
    base: dict[str, Any] = {
        "title": "Task",
        "url": "https://example.com",
        "webhook_callback_url": None,
        "webhook_failure_reason": None,
        "totp_verification_url": None,
        "totp_identifier": None,
        "navigation_goal": "Find the quote",
        "data_extraction_goal": "Extract the quote",
        "navigation_payload": None,
        "error_code_mapping": None,
        "proxy_location": None,
        "extracted_information_schema": None,
        "extra_http_headers": None,
        "complete_criterion": None,
        "terminate_criterion": None,
        "task_type": TaskType.general,
        "application": None,
        "include_action_history_in_verification": False,
        "max_screenshot_scrolls": None,
        "browser_address": None,
        "download_timeout": None,
        "created_at": now,
        "modified_at": now,
        "task_id": "task-123",
        "status": TaskStatus.running,
        "extracted_information": None,
        "failure_reason": None,
        "organization_id": organization.organization_id,
        "workflow_run_id": None,
        "workflow_permanent_id": None,
        "browser_session_id": None,
        "order": 0,
        "retry": 0,
        "max_steps_per_run": None,
        "errors": [],
        "model": None,
        "queued_at": now,
        "started_at": now,
        "finished_at": None,
    }
    base.update(overrides)
    return Task(**base)


def make_step(
    now: datetime,
    task: Task,
    *,
    step_id: str,
    status: StepStatus,
    order: int,
    output,
    is_last: bool = False,
    retry_index: int = 0,
    organization_id: str | None = None,
    **overrides: Any,
) -> Step:
    base: dict[str, Any] = {
        "created_at": now,
        "modified_at": now,
        "task_id": task.task_id,
        "step_id": step_id,
        "status": status,
        "output": output,
        "order": order,
        "is_last": is_last,
        "retry_index": retry_index,
        "organization_id": organization_id or task.organization_id,
    }
    base.update(overrides)
    return Step(**base)


@dataclass
class ParallelVerificationMocks:
    create_step: AsyncMock
    get_task_steps: AsyncMock
    sleep: AsyncMock
    check_user_goal_complete: AsyncMock
    handle_action: AsyncMock
    create_extract_action: AsyncMock | None
    speculate_next_step_plan: AsyncMock
    persist_speculative_metadata: AsyncMock
    cancel_speculative_step: AsyncMock
    record_artifacts_after_action: AsyncMock
    update_step: AsyncMock
    update_task: AsyncMock


def setup_parallel_verification_mocks(
    agent: ForgeAgent,
    *,
    step: Step,
    task: Task,
    monkeypatch: MonkeyPatch,
    next_step: Step | None,
    complete_action,
    handle_action_responses: Sequence[Any],
    extract_action: Any | None = None,
) -> ParallelVerificationMocks:
    create_step_mock = AsyncMock(return_value=next_step)
    monkeypatch.setattr(app.DATABASE.tasks, "create_step", create_step_mock)

    get_task_steps_mock = AsyncMock(return_value=[step])
    monkeypatch.setattr(app.DATABASE.tasks, "get_task_steps", get_task_steps_mock)

    sleep_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("skyvern.forge.agent.asyncio", ScopedAsyncio(sleep=sleep_mock))

    check_user_goal_complete_mock = AsyncMock(return_value=complete_action)
    monkeypatch.setattr(agent, "check_user_goal_complete", check_user_goal_complete_mock)

    handle_action_mock = AsyncMock(side_effect=handle_action_responses)
    monkeypatch.setattr("skyvern.forge.agent.ActionHandler.handle_action", handle_action_mock)

    speculate_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(agent, "_speculate_next_step_plan", speculate_mock)

    persist_mock = AsyncMock()
    monkeypatch.setattr(agent, "_persist_speculative_metadata_for_discarded_plan", persist_mock)

    cancel_mock = AsyncMock()
    monkeypatch.setattr(agent, "_cancel_speculative_step", cancel_mock)

    record_artifacts_mock = AsyncMock()
    monkeypatch.setattr(agent, "record_artifacts_after_action", record_artifacts_mock)

    update_step_mock = AsyncMock(return_value=step)
    monkeypatch.setattr(agent, "update_step", update_step_mock)

    update_task_mock = AsyncMock(return_value=task)
    monkeypatch.setattr(agent, "update_task", update_task_mock)

    if extract_action is not None:
        create_extract_action_mock = AsyncMock(return_value=extract_action)
        monkeypatch.setattr(agent, "create_extract_action", create_extract_action_mock)
    else:
        create_extract_action_mock = None

    return ParallelVerificationMocks(
        create_step=create_step_mock,
        get_task_steps=get_task_steps_mock,
        sleep=sleep_mock,
        check_user_goal_complete=check_user_goal_complete_mock,
        handle_action=handle_action_mock,
        create_extract_action=create_extract_action_mock,
        speculate_next_step_plan=speculate_mock,
        persist_speculative_metadata=persist_mock,
        cancel_speculative_step=cancel_mock,
        record_artifacts_after_action=record_artifacts_mock,
        update_step=update_step_mock,
        update_task=update_task_mock,
    )


# The repository's update_step adds these deltas to the step's own running totals, as the SQL update does.
_STEP_COUNTER_COLUMNS = {
    "incremental_cost": "step_cost",
    "incremental_input_tokens": "input_token_count",
    "incremental_output_tokens": "output_token_count",
    "incremental_reasoning_tokens": "reasoning_token_count",
    "incremental_cached_tokens": "cached_token_count",
}


@dataclass
class InMemoryTaskStepRepo:
    """Backs app.DATABASE.tasks for one task and step, so the real ForgeAgent update methods read back their writes."""

    task: Task | None = None
    step: Step | None = None

    async def create_task(self, *, url: str, organization_id: str | None, **fields: Any) -> Task:
        fields["max_screenshot_scrolls"] = fields.pop("max_screenshot_scrolling_times", None)
        now = datetime.now(UTC)
        self.task = Task(
            task_id=f"tsk_{secrets.token_hex(6)}",
            url=url,
            organization_id=organization_id,
            status=TaskStatus.created,
            created_at=now,
            modified_at=now,
            **{name: value for name, value in fields.items() if value is not None},
        )
        return self.task

    async def create_step(self, task_id: str, *, order: int, retry_index: int, organization_id: str | None) -> Step:
        now = datetime.now(UTC)
        self.step = Step(
            task_id=task_id,
            step_id=f"stp_{secrets.token_hex(6)}",
            status=StepStatus.created,
            order=order,
            retry_index=retry_index,
            is_last=False,
            organization_id=organization_id,
            created_at=now,
            modified_at=now,
        )
        return self.step

    async def get_task(self, task_id: str, organization_id: str | None = None) -> Task | None:
        return self.task if self.task is not None and self.task.task_id == task_id else None

    async def update_task(self, task_id: str, organization_id: str | None = None, **updates: Any) -> Task:
        updated, _ = await self.update_task_and_claim_finish(task_id, organization_id=organization_id, **updates)
        return updated

    async def update_task_and_claim_finish(
        self,
        task_id: str,
        status: TaskStatus | None = None,
        organization_id: str | None = None,
        errors: list[dict[str, Any]] | None = None,
        *,
        keep_final_status: bool = False,
        **updates: Any,
    ) -> tuple[Task, bool]:
        task = self.task
        assert task is not None and task.task_id == task_id, f"no task {task_id}"
        if keep_final_status and task.status.is_final():
            return task, False
        finish_claimed = False
        if status is not None:
            task.status = status
            if status == TaskStatus.running and task.started_at is None:
                task.started_at = datetime.now(UTC)
            if status.is_final() and task.finished_at is None:
                task.finished_at = datetime.now(UTC)
                finish_claimed = True
        if errors is not None:
            task.errors = [*(task.errors or []), *errors]
        for key, value in updates.items():
            if value is not None:
                setattr(task, key, value)
        return task, finish_claimed

    async def update_task_and_claim_finish_for_cancel(
        self,
        task_id: str,
        organization_id: str | None,
        audit_actor: Any = None,
        failure_reason: str | None = None,
        errors: list[dict[str, Any]] | None = None,
        extracted_information: dict[str, Any] | list | str | None = None,
        webhook_failure_reason: str | None = None,
    ) -> tuple[Task, bool, bool]:
        task = self.task
        assert task is not None and task.task_id == task_id, f"no task {task_id}"
        if task.status.is_final():
            return task, False, False

        finish_claimed = task.finished_at is None
        task.status = TaskStatus.canceled
        if finish_claimed:
            task.finished_at = datetime.now(UTC)
        if failure_reason is not None:
            task.failure_reason = failure_reason
        if errors is not None:
            task.errors = [*(task.errors or []), *errors]
        if extracted_information is not None:
            task.extracted_information = extracted_information
        if webhook_failure_reason is not None:
            task.webhook_failure_reason = webhook_failure_reason
        return task, finish_claimed, True

    async def update_step(self, task_id: str, step_id: str, organization_id: str | None = None, **updates: Any) -> Step:
        assert self.step is not None and self.step.step_id == step_id, f"no step {step_id}"
        for key, value in updates.items():
            if key in _STEP_COUNTER_COLUMNS:
                column = _STEP_COUNTER_COLUMNS[key]
                if value is not None:
                    setattr(self.step, column, (getattr(self.step, column) or 0) + value)
            else:
                setattr(self.step, key, value)
        return self.step

    async def get_first_step(self, task_id: str, organization_id: str | None = None) -> None:
        return None

    async def get_task_steps(self, task_id: str, organization_id: str | None = None) -> list[Step]:
        return [self.step] if self.step is not None and self.step.task_id == task_id else []

    async def get_latest_step(self, task_id: str, organization_id: str | None = None) -> Step | None:
        steps = await self.get_task_steps(task_id, organization_id)
        return steps[-1] if steps else None

    async def get_run(self, run_id: str, organization_id: str | None = None) -> None:
        return None

    def install(self, monkeypatch: MonkeyPatch) -> None:
        for name in (
            "create_task",
            "create_step",
            "get_task",
            "update_task",
            "update_task_and_claim_finish",
            "update_task_and_claim_finish_for_cancel",
            "update_step",
            "get_first_step",
            "get_task_steps",
            "get_latest_step",
            "get_run",
        ):
            monkeypatch.setattr(app.DATABASE.tasks, name, getattr(self, name))
        monkeypatch.setattr(
            app.DATABASE.observer, "get_workflow_run_block_engine_by_task_id", AsyncMock(return_value=None)
        )


def make_browser_state() -> tuple[MagicMock, MagicMock, MagicMock]:
    return MagicMock(), MagicMock(), MagicMock()


def make_action_row(**overrides: Any) -> SimpleNamespace:
    """Build a minimal duck-typed stand-in for ActionModel."""
    base: dict[str, Any] = {
        "action_id": "act_test",
        "action_type": ActionType.MOVE,
        "status": ActionStatus.completed,
        "source_action_id": None,
        "organization_id": "o_test",
        "workflow_run_id": "wr_test",
        "task_id": "tsk_test",
        "step_id": "stp_test",
        "step_order": 0,
        "action_order": 0,
        "confidence_float": None,
        "reasoning": None,
        "intention": None,
        "response": None,
        "element_id": None,
        "skyvern_element_hash": None,
        "skyvern_element_data": None,
        "screenshot_artifact_id": None,
        "started_at": None,
        "finished_at": None,
        "created_at": datetime(2026, 5, 6, 0, 0, 0),
        "modified_at": datetime(2026, 5, 6, 0, 0, 0),
        "action_json": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


ONE_TIME_CODE = "314159"
WITHHELD_LABEL = "Input value withheld."
CLICK_REASONING = f"Typed {ONE_TIME_CODE} into the code field; clicking Verify."
_IDENTITY_FIELDS = (
    "action_id",
    "action_type",
    "status",
    "step_id",
    "action_order",
    "created_at",
    "started_at",
    "finished_at",
)


def _fetched_code_action(**overrides: Any) -> VerificationCodeAction:
    # Shaped as parse_actions writes it, with the code in reasoning and intention.
    reasoning = f"Received verification code: {ONE_TIME_CODE}"
    return VerificationCodeAction(
        verification_code=ONE_TIME_CODE,
        reasoning=reasoning,
        intention=reasoning,
        status=ActionStatus.completed,
        **overrides,
    )


def _marked_code_input(status: ActionStatus = ActionStatus.completed, **overrides: Any) -> InputTextAction:
    return InputTextAction(
        element_id="otp",
        text=ONE_TIME_CODE,
        response=ONE_TIME_CODE,
        reasoning=f"The page asks for the code {ONE_TIME_CODE}.",
        intention=f"Enter {ONE_TIME_CODE}",
        input_or_select_context=InputOrSelectContext(
            field=f"One-time code {ONE_TIME_CODE}", intention=f"Enter {ONE_TIME_CODE}"
        ),
        totp_identifier="inbox@example.test",
        status=status,
        **overrides,
    )


def _code_click_row(**overrides: Any) -> SimpleNamespace:
    return make_action_row(
        action_id="act_click",
        action_type=ActionType.CLICK,
        element_id="verify",
        action_json={"element_id": "verify", "reasoning": CLICK_REASONING, "intention": CLICK_REASONING},
        **{"action_order": 5, **overrides},
    )


def make_fetched_code_action_rows() -> list[SimpleNamespace]:
    """A fetched code, then a failed click that quotes it before any input row exists."""
    fetched = make_action_row(
        action_id="act_code_fetch",
        action_type=ActionType.VERIFICATION_CODE,
        action_json=_fetched_code_action().model_dump(mode="json"),
    )
    return [fetched, _code_click_row(status=ActionStatus.failed, action_order=1)]


def make_one_time_code_action_rows() -> list[SimpleNamespace]:
    """Marked, untyped and saved-only code inputs, a fetched code, an input, a result, a click quoting the code."""
    entered_at = datetime(2026, 9, 18, 12, 0, 5)
    marked = make_action_row(
        action_id="act_otp",
        action_type=ActionType.INPUT_TEXT,
        element_id="otp",
        response=REDACTED_OTP_VALUE,
        started_at=entered_at,
        finished_at=entered_at,
        action_json=_marked_code_input().model_dump(mode="json"),
    )
    untyped = make_action_row(
        action_id="act_otp_untyped",
        action_type=ActionType.INPUT_TEXT,
        element_id="otp_retry",
        status=ActionStatus.failed,
        action_order=1,
        action_json={"totp_code_required": True, "response": ONE_TIME_CODE, "reasoning": f"Retyped {ONE_TIME_CODE}."},
    )
    fetched = make_action_row(
        action_id="act_code_fetch",
        action_type=ActionType.VERIFICATION_CODE,
        action_order=2,
        started_at=entered_at,
        finished_at=entered_at,
        action_json=_fetched_code_action().model_dump(mode="json"),
    )
    typed = make_action_row(
        action_id="act_typed",
        action_type=ActionType.INPUT_TEXT,
        element_id="street",
        action_json={"element_id": "street", "text": "Meridian Ave"},
    )
    result = make_action_row(
        action_id="act_complete",
        action_type=ActionType.COMPLETE,
        action_json={"output": {"failure_rate": "4.2%"}},
    )
    saved_only = make_action_row(
        action_id="act_otp_saved_only",
        action_type=ActionType.INPUT_TEXT,
        element_id="otp_again",
        action_order=6,
        action_json={"element_id": "otp_again", "text": ONE_TIME_CODE, "totp_code_required": True},
    )
    return [marked, untyped, fetched, typed, result, _code_click_row(), saved_only]


def _identity(action: dict[str, Any]) -> dict[str, Any]:
    return {key: action.get(key) for key in _IDENTITY_FIELDS}


def assert_one_time_code_withheld(body: str, served: list[dict[str, Any]], sources: Sequence[Action]) -> None:
    assert ONE_TIME_CODE not in body
    by_id = {action["action_id"]: action for action in served}
    masked_ids = ("act_otp", "act_otp_untyped", "act_code_fetch")
    for source in sources:
        if source.action_id in masked_ids:
            assert _identity(by_id[source.action_id]) == _identity(json.loads(source.model_dump_json()))
    assert by_id["act_otp"]["text"] == REDACTED_OTP_VALUE
    assert by_id["act_otp_untyped"]["response"] == REDACTED_INPUT_VALUE
    assert {by_id[action_id]["reasoning"] for action_id in masked_ids} == {WITHHELD_LABEL}
    assert by_id["act_typed"]["text"] == "Meridian Ave"
    assert by_id["act_complete"]["output"] == {"failure_rate": "4.2%"}
    assert_code_click_scrubbed(by_id["act_click"])


def assert_code_click_scrubbed(click: dict[str, Any]) -> None:
    for field in ("reasoning", "intention"):
        assert click[field] == CLICK_REASONING.replace(ONE_TIME_CODE, REDACTED_SECRET_PLACEHOLDER)


# Captured from a headless Chromium Locator.fill on a disabled input: the call log quotes the typed value.
_FILL_ERROR = (
    'Locator.fill: Timeout 5000ms exceeded.\nCall log:\nwaiting for locator("#otp")\n'
    f'  -   locator resolved to <input id="otp" disabled/>\n  -   fill("{ONE_TIME_CODE}")\n'
    "  - attempting fill action\n  -   waiting for element to be visible, enabled and editable\n"
)
_CLICK_ERROR = 'Locator.click: Timeout 5000ms exceeded.\nCall log:\nwaiting for locator("#submit")\n'
_TYPE_ERROR = f'elementHandle.type: Target closed\nCall log:\n  -   elementHandle.type("{ONE_TIME_CODE}")\n'


def _step(step_id: str, order: int, output: dict[str, Any]) -> Step:
    created_at = datetime(2026, 9, 18, 12, 0, 0)
    return Step.model_validate(
        {
            "task_id": "tsk_test",
            "step_id": step_id,
            "created_at": created_at,
            "modified_at": created_at,
            "status": StepStatus.failed,
            "output": output,
            "order": order,
            "is_last": order == 1,
            "organization_id": "o_test",
        }
    )


def make_one_time_code_steps() -> list[Step]:
    """Steps as get_task_steps reads back what update_step wrote, in the current and the legacy output format."""
    entered_at = datetime(2026, 9, 18, 12, 0, 5)
    pairs: list[tuple[Action, list[ActionResult]]] = [
        (
            _fetched_code_action(action_id="act_code_fetch", step_id="stp_test", action_order=0),
            [ActionSuccess(data={"totp_group_filled": True})],
        ),
        (
            _marked_code_input(
                action_id="act_otp",
                step_id="stp_test",
                action_order=1,
                started_at=entered_at,
                finished_at=entered_at,
                status=ActionStatus.failed,
            ),
            [ActionFailure(PlaywrightError(_FILL_ERROR))],
        ),
        (
            ClickAction(action_id="act_submit", element_id="submit", step_id="stp_test", action_order=2),
            [ActionFailure(PlaywrightError(_CLICK_ERROR))],
        ),
        (
            CompleteAction(
                action_id="act_complete", step_id="stp_test", action_order=3, output={"failure_rate": "4.2%"}
            ),
            [ActionSuccess(data={"failure_rate": "4.2%"})],
        ),
        (
            InputTextAction(
                action_id="act_typed", element_id="street", text="Meridian Ave", step_id="stp_test", action_order=4
            ),
            [ActionSuccess()],
        ),
        (
            ClickAction(
                action_id="act_click",
                element_id="verify",
                step_id="stp_test",
                action_order=5,
                reasoning=CLICK_REASONING,
                intention=CLICK_REASONING,
            ),
            [ActionFailure(PlaywrightError(_CLICK_ERROR))],
        ),
    ]
    # Only the saved rows of these inputs carry a marker, so the step copy holds the plain code.
    pairs += [
        (
            InputTextAction(
                action_id=action_id,
                element_id="otp_again",
                text=ONE_TIME_CODE,
                reasoning=f"Retyping {ONE_TIME_CODE}.",
                step_id="stp_test",
                action_order=order,
            ),
            [ActionFailure(PlaywrightError(_FILL_ERROR))],
        )
        for action_id, order in (("act_otp_saved_only", 6), ("act_otp_untyped", 7))
    ]
    current = AgentStepOutput(
        actions_and_results=pairs, action_results=[result for _, results in pairs for result in results]
    )
    legacy = AgentStepOutput(
        actions_and_results=[],
        action_results=[
            ActionFailure(PlaywrightError(_FILL_ERROR)),
            ActionFailure(PlaywrightError(_TYPE_ERROR)),
            ActionSuccess(data={"failure_rate": "4.2%"}),
        ],
    )
    return [
        _step("stp_test", 0, current.model_dump(exclude_none=True)),
        _step("stp_legacy", 1, legacy.model_dump(exclude_none=True)),
    ]


def assert_step_code_withheld(body: str, sources: Sequence[Step]) -> None:
    assert ONE_TIME_CODE not in body
    current_source, legacy_source = sources
    assert current_source.output is not None and current_source.output.actions_and_results is not None
    assert ONE_TIME_CODE in (current_source.output.actions_and_results[1][1][0].exception_message or "")
    source_pairs = current_source.output.actions_and_results
    current, legacy = (step["output"] for step in json.loads(body))
    served = [action for action, _ in current["actions_and_results"]]
    served_results = [results for _, results in current["actions_and_results"]]
    assert [_identity(action) for action in served] == [
        _identity(json.loads(action.model_dump_json())) for action, _ in source_pairs
    ]
    assert served[0]["reasoning"] == served[1]["reasoning"] == WITHHELD_LABEL
    assert served[1]["text"] == REDACTED_OTP_VALUE
    assert "data" not in served_results[0][0]
    assert (served_results[1][0]["success"], served_results[1][0]["exception_type"]) == (False, "Error")
    assert "exception_message" not in served_results[1][0]
    assert served_results[2][0]["exception_message"] == source_pairs[2][1][0].exception_message
    assert "Locator.click" in served_results[2][0]["exception_message"]
    assert served[3]["output"] == {"failure_rate": "4.2%"}
    assert served[4]["text"] == "Meridian Ave"
    assert_code_click_scrubbed(served[5])
    assert "Locator.click" in served_results[5][0]["exception_message"]
    for action, results in zip(served[6:], served_results[6:], strict=True):
        assert (action["text"], action["reasoning"]) == (REDACTED_INPUT_VALUE, WITHHELD_LABEL)
        assert "exception_message" not in results[0]
    flat = current["action_results"]
    assert "data" not in flat[0] and "exception_message" not in flat[1]
    assert flat[2]["exception_message"] == served_results[2][0]["exception_message"]
    assert flat[3]["data"] == served_results[3][0]["data"] == {"failure_rate": "4.2%"}
    assert [result.get("exception_type") for result in legacy["action_results"]] == ["Error", "Error", None]
    assert legacy["action_results"][2]["data"] == {"failure_rate": "4.2%"}


def make_session_factory_yielding(rows: list[Any]) -> Any:
    """A Session factory whose scalars() returns `rows`, so a repository method runs for real.

    Patch it onto a repository class as `Session` to exercise the real retrieval code over
    duck-typed ORM rows (see make_action_row) without a database.
    """

    class _Result:
        @staticmethod
        def all() -> list[Any]:
            return rows

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def scalars(self, _query: Any) -> _Result:
            return _Result()

    return staticmethod(lambda *_args: _Session())
