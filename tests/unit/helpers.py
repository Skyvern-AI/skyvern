from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Iterator, Sequence
from unittest.mock import AsyncMock, MagicMock

from pytest import MonkeyPatch  # type: ignore[import-not-found]

from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.api.llm import api_handler_factory
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.models import LLMRouterConfig, LLMRouterModelConfig
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus


class FakeLLMResponse:
    def __init__(self, model: str) -> None:
        self.model = model
        self._content = '{"actions": []}'
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(
                    content=self._content,
                )
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

    def info(self, event: str, **kwargs: dict[str, Any]) -> None:
        self.events.append((event, kwargs))

    def warning(self, *args, **kwargs) -> None:  # pragma: no cover
        pass

    def exception(self, *args, **kwargs) -> None:  # pragma: no cover
        pass

    def debug(self, *args, **kwargs) -> None:  # pragma: no cover
        pass


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
    monkeypatch.setattr(app.DATABASE, "create_step", create_step_mock)

    get_task_steps_mock = AsyncMock(return_value=[step])
    monkeypatch.setattr(app.DATABASE, "get_task_steps", get_task_steps_mock)

    sleep_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("skyvern.forge.agent.asyncio.sleep", sleep_mock)

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


def make_browser_state() -> tuple[MagicMock, MagicMock, MagicMock]:
    return MagicMock(), MagicMock(), MagicMock()
