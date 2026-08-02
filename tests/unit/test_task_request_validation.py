"""Tests for TaskRequest input validation (SKY-9857)."""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.exceptions import BlockedHost
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.task_v2 import TaskV2
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.settings_manager import SettingsManager


def _task_with_model(model: dict[str, str] | None = None) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        task_id="tsk_llm_default",
        organization_id="o_test",
        status=TaskStatus.running,
        created_at=now,
        modified_at=now,
        url="https://example.com",
        model=model,
    )


def test_task_llm_key_ignores_ambient_org_default() -> None:
    with skyvern_context.scoped(SkyvernContext(org_default_llm_key="CUSTOM_LLM_oat_smart")):
        assert _task_with_model().llm_key is None


def test_task_explicit_model_wins_over_org_default(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = MagicMock()
    settings.get_model_name_to_llm_key.return_value = {"selected-model": {"llm_key": "EXPLICIT_LLM_KEY"}}
    monkeypatch.setattr(SettingsManager, "get_settings", MagicMock(return_value=settings))
    with skyvern_context.scoped(SkyvernContext(org_default_llm_key="CUSTOM_LLM_oat_smart")):
        assert _task_with_model({"model_name": "selected-model"}).llm_key == "EXPLICIT_LLM_KEY"


def test_task_llm_key_is_safe_without_context() -> None:
    skyvern_context.reset()
    assert _task_with_model().llm_key is None


def test_task_v2_llm_key_ignores_ambient_org_default() -> None:
    with skyvern_context.scoped(SkyvernContext(org_default_llm_key="CUSTOM_LLM_oat_smart")):
        assert TaskV2.model_construct(organization_id="o_test", model=None).llm_key is None


def test_task_models_do_not_resolve_dns_during_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.schemas.tasks import TaskRequest
    from skyvern.schemas.runs import TaskRunRequest

    monkeypatch.setattr(
        "skyvern.utils.url_validators.socket.getaddrinfo",
        MagicMock(side_effect=AssertionError("task model validation must not resolve DNS")),
    )

    TaskRunRequest(prompt="stored task", url="https://task.example.test")
    TaskRequest(url="https://task.example.test")


@pytest.mark.parametrize("task_version", ["v1", "v2"])
@pytest.mark.asyncio
async def test_task_write_rejects_hostname_resolving_to_blocked_ip(
    monkeypatch: pytest.MonkeyPatch, task_version: str
) -> None:
    from skyvern.forge.sdk.schemas.tasks import TaskRequest
    from skyvern.services import task_v1_service, task_v2_service

    monkeypatch.setattr(
        "skyvern.utils.url_validators.socket.getaddrinfo",
        lambda host, port, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.42", 0))],
    )
    write = AsyncMock()

    with pytest.raises(BlockedHost):
        if task_version == "v1":
            monkeypatch.setattr(task_v1_service.app.agent, "create_task", write)
            await task_v1_service.run_task(
                TaskRequest(url="https://task.example.test"), SimpleNamespace(organization_id="o_test")
            )
        else:
            monkeypatch.setattr(task_v2_service.app.DATABASE.observer, "create_task_v2", write)
            await task_v2_service.initialize_task_v2(
                organization=SimpleNamespace(organization_id="o_test"),
                user_prompt="test",
                user_url="https://task.example.test",
            )

    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_v1_empty_url_with_browser_session_skips_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.schemas.tasks import TaskRequest
    from skyvern.services import task_v1_service

    create_task = AsyncMock(side_effect=RuntimeError("reached task write"))
    monkeypatch.setattr(task_v1_service.app.agent, "create_task", create_task)
    with pytest.raises(RuntimeError, match="reached task write"):
        await task_v1_service.run_task(
            TaskRequest(url="", browser_session_id="pbs_test"), SimpleNamespace(organization_id="o_test")
        )
    create_task.assert_awaited_once()


def test_data_extraction_goal_none_passes() -> None:
    from skyvern.forge.sdk.schemas.tasks import TaskRequest

    req = TaskRequest(url="https://example.com", data_extraction_goal=None)
    assert req.data_extraction_goal is None


def test_data_extraction_goal_short_passes() -> None:
    from skyvern.forge.sdk.schemas.tasks import TaskRequest

    req = TaskRequest(url="https://example.com", data_extraction_goal="Extract the total price")
    assert req.data_extraction_goal == "Extract the total price"


def test_data_extraction_goal_over_limit_raises() -> None:
    from skyvern.exceptions import SkyvernHTTPException
    from skyvern.utils.prompt_truncation import EXTRACTION_GOAL_MAX_TOKENS

    # Build a goal that is clearly over the token limit.
    # 200_000 repetitions of "extract " * ~7 chars → ~1.4M chars well above 600k fast-exit.
    oversized_goal = "extract " * 200_000

    from skyvern.forge.sdk.schemas.tasks import TaskRequest

    with pytest.raises(SkyvernHTTPException) as exc_info:
        TaskRequest(url="https://example.com", data_extraction_goal=oversized_goal)

    assert f"{EXTRACTION_GOAL_MAX_TOKENS:,}" in exc_info.value.message


def test_extraction_goal_max_tokens_constant() -> None:
    from skyvern.utils.prompt_truncation import EXTRACTION_GOAL_MAX_TOKENS

    assert EXTRACTION_GOAL_MAX_TOKENS == 150_000


def test_task_run_request_rejects_start_fresh_with_session() -> None:
    import pydantic

    from skyvern.schemas.runs import TaskRunRequest

    with pytest.raises(pydantic.ValidationError, match="cannot be combined with browser_session_id"):
        TaskRunRequest(prompt="t", browser_session_id="pbs_1", start_fresh_browser=True)


def test_task_run_request_allows_session_or_start_fresh_alone() -> None:
    from skyvern.schemas.runs import TaskRunRequest

    TaskRunRequest(prompt="t", browser_session_id="pbs_1")
    TaskRunRequest(prompt="t", start_fresh_browser=True)


def test_workflow_run_request_rejects_start_fresh_with_session() -> None:
    import pydantic

    from skyvern.schemas.runs import WorkflowRunRequest

    with pytest.raises(pydantic.ValidationError, match="cannot be combined with browser_session_id"):
        WorkflowRunRequest(agent_id="wpid_1", browser_session_id="pbs_1", start_fresh_browser=True)


def test_workflow_run_request_allows_session_or_start_fresh_alone() -> None:
    from skyvern.schemas.runs import WorkflowRunRequest

    WorkflowRunRequest(agent_id="wpid_1", browser_session_id="pbs_1")
    WorkflowRunRequest(agent_id="wpid_1", start_fresh_browser=True)


def test_task_run_request_rejects_start_fresh_with_address() -> None:
    import pydantic

    from skyvern.schemas.runs import TaskRunRequest

    # A browser_address connects to a live remote browser with its existing cookies — that reuse
    # violates the fresh contract, so the combination must be rejected at the request boundary.
    with pytest.raises(pydantic.ValidationError, match="cannot be combined with browser_address"):
        TaskRunRequest(prompt="t", browser_address="http://1.2.3.4:9222", start_fresh_browser=True)


def test_task_run_request_allows_address_or_start_fresh_alone() -> None:
    from skyvern.schemas.runs import TaskRunRequest

    TaskRunRequest(prompt="t", browser_address="http://1.2.3.4:9222")
    TaskRunRequest(prompt="t", start_fresh_browser=True)


def test_workflow_run_request_rejects_start_fresh_with_address() -> None:
    import pydantic

    from skyvern.schemas.runs import WorkflowRunRequest

    with pytest.raises(pydantic.ValidationError, match="cannot be combined with browser_address"):
        WorkflowRunRequest(agent_id="wpid_1", browser_address="http://1.2.3.4:9222", start_fresh_browser=True)


def test_workflow_run_request_allows_address_or_start_fresh_alone() -> None:
    from skyvern.schemas.runs import WorkflowRunRequest

    WorkflowRunRequest(agent_id="wpid_1", browser_address="http://1.2.3.4:9222")
    WorkflowRunRequest(agent_id="wpid_1", start_fresh_browser=True)


def test_workflow_request_body_rejects_start_fresh_with_address() -> None:
    import pydantic

    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody

    with pytest.raises(pydantic.ValidationError, match="cannot be combined with browser_address"):
        WorkflowRequestBody(browser_address="http://1.2.3.4:9222", start_fresh_browser=True)


def test_login_request_rejects_start_fresh_with_session() -> None:
    import pydantic

    from skyvern.schemas.credential_type import CredentialType
    from skyvern.schemas.run_blocks import LoginRequest

    with pytest.raises(pydantic.ValidationError, match="cannot be combined with browser_session_id"):
        LoginRequest(credential_type=CredentialType.skyvern, start_fresh_browser=True, browser_session_id="pbs_1")


def test_login_request_allows_session_or_start_fresh_alone() -> None:
    from skyvern.schemas.credential_type import CredentialType
    from skyvern.schemas.run_blocks import LoginRequest

    LoginRequest(credential_type=CredentialType.skyvern, browser_session_id="pbs_1")
    LoginRequest(credential_type=CredentialType.skyvern, start_fresh_browser=True)


def test_block_run_request_rejects_start_fresh_with_address() -> None:
    import pydantic

    from skyvern.schemas.credential_type import CredentialType
    from skyvern.schemas.run_blocks import LoginRequest

    # Without this the block routes only fail deep in execution (500 + an orphaned workflow) instead
    # of a clean 422 at the request boundary, the way the task/workflow run models already reject it.
    with pytest.raises(pydantic.ValidationError, match="cannot be combined with browser_address"):
        LoginRequest(
            credential_type=CredentialType.skyvern,
            browser_address="http://1.2.3.4:9222",
            start_fresh_browser=True,
        )


def test_workflow_request_body_rejects_start_fresh_with_session() -> None:
    import pydantic

    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody

    with pytest.raises(pydantic.ValidationError, match="cannot be combined with browser_session_id"):
        WorkflowRequestBody(start_fresh_browser=True, browser_session_id="pbs_1")


def test_workflow_request_body_allows_session_or_start_fresh_alone() -> None:
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody

    WorkflowRequestBody(browser_session_id="pbs_1")
    WorkflowRequestBody(start_fresh_browser=True)


def test_task_run_request_rejects_start_fresh_with_profile() -> None:
    import pydantic

    from skyvern.schemas.runs import TaskRunRequest

    with pytest.raises(pydantic.ValidationError, match="cannot be combined with browser_profile_id"):
        TaskRunRequest(prompt="t", browser_profile_id="bp_1", start_fresh_browser=True)


def test_task_run_request_allows_profile_or_start_fresh_alone() -> None:
    from skyvern.schemas.runs import TaskRunRequest

    TaskRunRequest(prompt="t", browser_profile_id="bp_1")
    TaskRunRequest(prompt="t", start_fresh_browser=True)


def test_workflow_run_request_rejects_start_fresh_with_profile() -> None:
    import pydantic

    from skyvern.schemas.runs import WorkflowRunRequest

    with pytest.raises(pydantic.ValidationError, match="cannot be combined with browser_profile_id"):
        WorkflowRunRequest(agent_id="wpid_1", browser_profile_id="bp_1", start_fresh_browser=True)


def test_workflow_run_request_allows_profile_or_start_fresh_alone() -> None:
    from skyvern.schemas.runs import WorkflowRunRequest

    WorkflowRunRequest(agent_id="wpid_1", browser_profile_id="bp_1")
    WorkflowRunRequest(agent_id="wpid_1", start_fresh_browser=True)


def test_login_request_rejects_start_fresh_with_profile() -> None:
    import pydantic

    from skyvern.schemas.credential_type import CredentialType
    from skyvern.schemas.run_blocks import LoginRequest

    with pytest.raises(pydantic.ValidationError, match="cannot be combined with browser_profile_id"):
        LoginRequest(credential_type=CredentialType.skyvern, start_fresh_browser=True, browser_profile_id="bp_1")


def test_login_request_allows_profile_or_start_fresh_alone() -> None:
    from skyvern.schemas.credential_type import CredentialType
    from skyvern.schemas.run_blocks import LoginRequest

    LoginRequest(credential_type=CredentialType.skyvern, browser_profile_id="bp_1")
    LoginRequest(credential_type=CredentialType.skyvern, start_fresh_browser=True)


def test_workflow_request_body_rejects_start_fresh_with_profile() -> None:
    import pydantic

    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody

    with pytest.raises(pydantic.ValidationError, match="cannot be combined with browser_profile_id"):
        WorkflowRequestBody(start_fresh_browser=True, browser_profile_id="bp_1")


def test_workflow_request_body_allows_profile_or_start_fresh_alone() -> None:
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody

    WorkflowRequestBody(browser_profile_id="bp_1")
    WorkflowRequestBody(start_fresh_browser=True)
