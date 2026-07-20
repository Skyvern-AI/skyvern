"""Tests for TaskRequest input validation (SKY-9857)."""

from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.exceptions import BlockedHost


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
